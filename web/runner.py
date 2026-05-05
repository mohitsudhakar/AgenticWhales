"""Background analysis runner: drives TradingAgentsGraph and streams events."""

from __future__ import annotations

import asyncio
import threading
import time
import traceback
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents import portfolio
from tradingagents.market_snapshot import fetch_snapshot_block

from cli.stats_handler import StatsCallbackHandler

from . import storage


# ---- Mirrors cli/main.py MessageBuffer mappings ----

ANALYST_ORDER = ["market", "social", "news", "fundamentals"]

ANALYST_AGENT_NAMES = {
    "market": "Market Analyst",
    "social": "Social Analyst",
    "news": "News Analyst",
    "fundamentals": "Fundamentals Analyst",
}

ANALYST_REPORT_MAP = {
    "market": "market_report",
    "social": "sentiment_report",
    "news": "news_report",
    "fundamentals": "fundamentals_report",
}

FIXED_TEAMS: List[Tuple[str, List[str]]] = [
    ("Research Team", ["Bull Researcher", "Bear Researcher", "Research Manager"]),
    ("Trading Team", ["Trader"]),
    ("Risk Management", ["Aggressive Analyst", "Neutral Analyst", "Conservative Analyst"]),
    ("Portfolio Management", ["Portfolio Manager"]),
]

# Reverse map: each agent → the team it belongs to. Built once at import so the
# runner can look up team membership in O(1) on every status transition.
AGENT_TO_TEAM: Dict[str, str] = {}
for _team_name, _agents in (
    [("Analyst Team", list(ANALYST_AGENT_NAMES.values()))] + list(FIXED_TEAMS)
):
    for _agent in _agents:
        AGENT_TO_TEAM[_agent] = _team_name


# Maps each canonical report section to the agent that "owns" it for the UI.
SECTION_AGENT = {
    "market_report": "Market Analyst",
    "sentiment_report": "Social Analyst",
    "news_report": "News Analyst",
    "fundamentals_report": "Fundamentals Analyst",
    "bull_history": "Bull Researcher",
    "bear_history": "Bear Researcher",
    "investment_plan": "Research Manager",
    "trader_investment_plan": "Trader",
    "aggressive_history": "Aggressive Analyst",
    "conservative_history": "Conservative Analyst",
    "neutral_history": "Neutral Analyst",
    "final_trade_decision": "Portfolio Manager",
}


class _Cancelled(Exception):
    """Internal sentinel — raised from the run thread when the user cancels.
    Caught by _run_safe to mark the session as 'cancelled' instead of
    'failed'. Not part of the public API."""


class SessionRunner:
    """Owns one analysis run, its in-memory state, and websocket fan-out."""

    def __init__(self, session: Dict[str, Any], loop: asyncio.AbstractEventLoop):
        self.session = session
        self.loop = loop
        self.subscribers: List[asyncio.Queue] = []
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        # Cooperative cancellation. Python threads can't be force-stopped, so
        # the worker checks this flag between graph.stream() chunks. Worst-
        # case latency = one LLM call duration after the user clicks Stop.
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        """Request that the run abort at the next chunk boundary."""
        self._cancelled.set()

    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    # ---- subscription API ----

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self.subscribers:
            self.subscribers.remove(q)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return _deep_copy(self.session)

    # ---- worker-thread mutators ----

    def _broadcast(self, event: Dict[str, Any]) -> None:
        for q in list(self.subscribers):
            self.loop.call_soon_threadsafe(q.put_nowait, event)

    def _set_status(self, agent: str, status: str) -> None:
        with self._lock:
            if self.session["agent_status"].get(agent) == status:
                return
            if agent not in self.session["agent_status"]:
                return
            self.session["agent_status"][agent] = status
        self._broadcast({"type": "agent_status", "agent": agent, "status": status})
        self._maybe_update_team_timing(agent, status)
        storage.save(self.session)

    def _maybe_update_team_timing(self, agent: str, status: str) -> None:
        """Record team start/end timestamps as agents transition through statuses.

        A team starts the first time *any* of its agents goes ``in_progress`` and
        ends when *every* of its agents present in ``agent_status`` is
        ``completed``. The session-level ``team_timings`` dict accumulates the
        result and is broadcast as a ``team_timing`` event for UI rendering.
        """
        team = AGENT_TO_TEAM.get(agent)
        if not team:
            return

        with self._lock:
            timings = self.session.setdefault("team_timings", {})
            entry = timings.setdefault(team, {"started_at": None, "completed_at": None, "duration_s": None})

            updated = False
            if status == "in_progress" and entry["started_at"] is None:
                entry["started_at"] = time.time()
                updated = True

            if status == "completed":
                # Have we now finished every team member that is part of this session?
                team_members = [
                    a for a, t in AGENT_TO_TEAM.items()
                    if t == team and a in self.session["agent_status"]
                ]
                all_done = team_members and all(
                    self.session["agent_status"].get(a) == "completed"
                    for a in team_members
                )
                if all_done and entry["completed_at"] is None:
                    entry["completed_at"] = time.time()
                    if entry["started_at"] is not None:
                        entry["duration_s"] = round(entry["completed_at"] - entry["started_at"], 2)
                    updated = True

            if not updated:
                return
            payload = {"team": team, "timing": dict(entry)}

        self._broadcast({"type": "team_timing", **payload})

    def _set_report(self, section: str, content: str) -> None:
        with self._lock:
            self.session["report_sections"][section] = content
        self._broadcast(
            {
                "type": "report",
                "section": section,
                "agent": SECTION_AGENT.get(section),
                "content": content,
            }
        )
        storage.save(self.session)

    def _append_message(self, msg: Dict[str, Any]) -> None:
        with self._lock:
            self.session["messages"].append(msg)
            if len(self.session["messages"]) > 500:
                self.session["messages"] = self.session["messages"][-500:]
        self._broadcast({"type": "message", "message": msg})

    def _set_session(self, **fields: Any) -> None:
        with self._lock:
            self.session.update(fields)
        self._broadcast({"type": "session", "session": self.snapshot()})
        storage.save(self.session)

    def _set_stats(self, stats: Dict[str, Any]) -> None:
        with self._lock:
            prev = self.session.get("stats") or {}
            if prev == stats:
                return
            self.session["stats"] = dict(stats)
        self._broadcast({"type": "stats", "stats": dict(stats)})
        storage.save(self.session)

    # ---- entrypoint ----

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_safe, daemon=True)
        self._thread.start()

    def _run_safe(self) -> None:
        try:
            self._run()
        except _Cancelled:
            self._set_session(status="cancelled", completed_at=time.time())
        except Exception as exc:
            # If the user clicked Stop and the underlying graph happened to
            # raise (e.g. an LLM API call mid-cancel), surface as cancelled
            # rather than failed — the user's intent was to abort.
            if self._cancelled.is_set():
                self._set_session(status="cancelled", completed_at=time.time())
                return
            self._set_session(
                status="failed",
                error=str(exc),
                error_traceback=traceback.format_exc(),
                completed_at=time.time(),
            )

    def _run(self) -> None:
        sel = self.session["config"]
        config = DEFAULT_CONFIG.copy()
        config["llm_provider"] = sel["llm_provider"]
        config["backend_url"] = sel.get("backend_url")
        config["quick_think_llm"] = sel["quick_think_llm"]
        config["deep_think_llm"] = sel["deep_think_llm"]
        config["max_debate_rounds"] = sel.get("research_depth", 1)
        config["max_risk_discuss_rounds"] = sel.get("research_depth", 1)
        config["google_thinking_level"] = sel.get("google_thinking_level")
        config["openai_reasoning_effort"] = sel.get("openai_reasoning_effort")
        config["anthropic_effort"] = sel.get("anthropic_effort")
        config["output_language"] = sel.get("output_language", "English")

        analysts: List[str] = sel["analysts"]
        stats_handler = StatsCallbackHandler()
        graph = TradingAgentsGraph(
            analysts, config=config, debug=False, callbacks=[stats_handler]
        )

        self._set_session(status="running", started_at=time.time())
        if analysts:
            self._set_status(ANALYST_AGENT_NAMES[analysts[0]], "in_progress")

        position_block = portfolio.format_for_prompt(self.session["ticker"])
        snapshot_block = fetch_snapshot_block(
            self.session["ticker"], self.session["analysis_date"]
        )
        init_state = graph.propagator.create_initial_state(
            self.session["ticker"],
            self.session["analysis_date"],
            current_position=position_block,
            market_snapshot=snapshot_block,
        )
        args = graph.propagator.get_graph_args(callbacks=[stats_handler])

        seen_msg_ids: set[str] = set()
        last = {
            "bull": "",
            "bear": "",
            "judge": "",
            "agg": "",
            "con": "",
            "neu": "",
            "risk_judge": "",
        }

        for chunk in graph.graph.stream(init_state, **args):
            if self._cancelled.is_set():
                raise _Cancelled()
            for message in chunk.get("messages", []):
                mid = getattr(message, "id", None)
                if mid is not None:
                    if mid in seen_msg_ids:
                        continue
                    seen_msg_ids.add(mid)
                msg_type, content = _classify_message(message)
                if content:
                    self._append_message(
                        {
                            "ts": datetime.utcnow().isoformat(),
                            "type": msg_type,
                            "content": content[:5000],
                        }
                    )
                tool_calls = getattr(message, "tool_calls", None) or []
                for tc in tool_calls:
                    name = tc["name"] if isinstance(tc, dict) else getattr(tc, "name", "tool")
                    targs = tc["args"] if isinstance(tc, dict) else getattr(tc, "args", {})
                    self._append_message(
                        {
                            "ts": datetime.utcnow().isoformat(),
                            "type": "tool_call",
                            "content": f"{name}({_compact_args(targs)})",
                        }
                    )

            self._update_analyst_statuses(chunk, analysts)

            if chunk.get("investment_debate_state"):
                d = chunk["investment_debate_state"]
                bh = (d.get("bull_history") or "").strip()
                rh = (d.get("bear_history") or "").strip()
                jd = (d.get("judge_decision") or "").strip()
                if bh and bh != last["bull"]:
                    last["bull"] = bh
                    self._set_report("bull_history", bh)
                    self._set_status("Bull Researcher", "in_progress")
                if rh and rh != last["bear"]:
                    last["bear"] = rh
                    self._set_report("bear_history", rh)
                    self._set_status("Bull Researcher", "completed")
                    self._set_status("Bear Researcher", "in_progress")
                if jd and jd != last["judge"]:
                    last["judge"] = jd
                    self._set_report("investment_plan", jd)
                    self._set_status("Bull Researcher", "completed")
                    self._set_status("Bear Researcher", "completed")
                    self._set_status("Research Manager", "completed")
                    self._set_status("Trader", "in_progress")

            if chunk.get("trader_investment_plan"):
                self._set_report("trader_investment_plan", chunk["trader_investment_plan"])
                self._set_status("Trader", "completed")
                self._set_status("Aggressive Analyst", "in_progress")

            if chunk.get("risk_debate_state"):
                r = chunk["risk_debate_state"]
                ah = (r.get("aggressive_history") or "").strip()
                ch = (r.get("conservative_history") or "").strip()
                nh = (r.get("neutral_history") or "").strip()
                jd = (r.get("judge_decision") or "").strip()
                if ah and ah != last["agg"]:
                    last["agg"] = ah
                    self._set_report("aggressive_history", ah)
                    self._set_status("Aggressive Analyst", "in_progress")
                if ch and ch != last["con"]:
                    last["con"] = ch
                    self._set_report("conservative_history", ch)
                    self._set_status("Aggressive Analyst", "completed")
                    self._set_status("Conservative Analyst", "in_progress")
                if nh and nh != last["neu"]:
                    last["neu"] = nh
                    self._set_report("neutral_history", nh)
                    self._set_status("Conservative Analyst", "completed")
                    self._set_status("Neutral Analyst", "in_progress")
                if jd and jd != last["risk_judge"]:
                    last["risk_judge"] = jd
                    for a in ("Aggressive Analyst", "Conservative Analyst", "Neutral Analyst"):
                        self._set_status(a, "completed")
                    self._set_status("Portfolio Manager", "in_progress")

            if chunk.get("final_trade_decision"):
                self._set_report("final_trade_decision", chunk["final_trade_decision"])

            # Snapshot accumulated token / call counts after each chunk so the UI
            # streams progress instead of waiting for completion.
            self._set_stats(stats_handler.get_stats())

        if self._cancelled.is_set():
            raise _Cancelled()
        for name in list(self.session["agent_status"].keys()):
            self._set_status(name, "completed")
        self._set_stats(stats_handler.get_stats())
        self._set_session(status="completed", completed_at=time.time())

    def _update_analyst_statuses(self, chunk: Dict[str, Any], analysts: List[str]) -> None:
        found_active = False
        for a in ANALYST_ORDER:
            if a not in analysts:
                continue
            agent = ANALYST_AGENT_NAMES[a]
            section = ANALYST_REPORT_MAP[a]
            if chunk.get(section):
                self._set_report(section, chunk[section])
            has_report = bool(self.session["report_sections"].get(section))
            if has_report:
                self._set_status(agent, "completed")
            elif not found_active:
                self._set_status(agent, "in_progress")
                found_active = True
            else:
                self._set_status(agent, "pending")


# ---- helpers ----


def _deep_copy(d: Any) -> Any:
    import copy

    return copy.deepcopy(d)


def _classify_message(message: Any) -> Tuple[str, Optional[str]]:
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    text = _stringify(getattr(message, "content", None))
    if isinstance(message, ToolMessage):
        return ("tool", text)
    if isinstance(message, HumanMessage):
        return ("user", text)
    if isinstance(message, AIMessage):
        return ("agent", text)
    return ("system", text)


def _stringify(content: Any) -> Optional[str]:
    if content is None:
        return None
    if isinstance(content, str):
        s = content.strip()
        return s or None
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                t = (item.get("text") or "").strip()
                if t:
                    parts.append(t)
            elif isinstance(item, str):
                t = item.strip()
                if t:
                    parts.append(t)
        return "\n".join(parts) or None
    return str(content).strip() or None


def _compact_args(args: Any, limit: int = 120) -> str:
    if not args:
        return ""
    try:
        if isinstance(args, dict):
            s = ", ".join(f"{k}={v}" for k, v in args.items())
        else:
            s = str(args)
    except Exception:
        s = repr(args)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def build_session(form: Dict[str, Any]) -> Dict[str, Any]:
    """Create a fresh session record from validated form data."""
    analysts = form.get("analysts") or list(ANALYST_ORDER)
    analysts = [a for a in ANALYST_ORDER if a in analysts]

    agent_status: Dict[str, str] = {}
    for a in analysts:
        agent_status[ANALYST_AGENT_NAMES[a]] = "pending"
    for _, names in FIXED_TEAMS:
        for n in names:
            agent_status[n] = "pending"

    return {
        "id": uuid.uuid4().hex,
        "ticker": form["ticker"].strip().upper(),
        "analysis_date": form["analysis_date"],
        "created_at": time.time(),
        "started_at": None,
        "completed_at": None,
        "status": "pending",
        "config": {
            "llm_provider": form["llm_provider"],
            "backend_url": form.get("backend_url"),
            "quick_think_llm": form["quick_think_llm"],
            "deep_think_llm": form["deep_think_llm"],
            "research_depth": int(form.get("research_depth", 1)),
            "google_thinking_level": form.get("google_thinking_level"),
            "openai_reasoning_effort": form.get("openai_reasoning_effort"),
            "anthropic_effort": form.get("anthropic_effort"),
            "output_language": form.get("output_language", "English"),
            "analysts": analysts,
        },
        "agent_status": agent_status,
        "report_sections": {},
        "messages": [],
        "error": None,
        "stats": {"llm_calls": 0, "tool_calls": 0, "tokens_in": 0, "tokens_out": 0},
        "team_timings": {},
    }
