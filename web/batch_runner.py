"""Sequential batch analysis runner with LLM meta-summary report."""

from __future__ import annotations

import asyncio
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from tradingagents import portfolio
from tradingagents.llm_clients.factory import create_llm_client

from . import batch_storage, storage
from .runner import SessionRunner, build_session


# Type alias for the callback that registers a child SessionRunner so that
# the existing /api/sessions/{id}/stream endpoint can serve its events.
RegisterSessionRunner = Callable[[SessionRunner], None]


class BatchRunner:
    """Runs analyses for a list of tickers sequentially, then composes a report."""

    def __init__(
        self,
        batch: Dict[str, Any],
        loop: asyncio.AbstractEventLoop,
        register_session: RegisterSessionRunner,
    ):
        self.batch = batch
        self.loop = loop
        self.register_session = register_session
        self.subscribers: List[asyncio.Queue] = []
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        # Cooperative cancellation. Cascades to the currently-running child
        # SessionRunners and prevents new ones from being kicked off.
        self._cancelled = threading.Event()
        self._active_children: List[SessionRunner] = []

    def cancel(self) -> None:
        """Request that the basket abort. Cascades to in-flight children
        so their LLM calls stop at the next chunk boundary too."""
        self._cancelled.set()
        for child in list(self._active_children):
            try:
                child.cancel()
            except Exception:
                pass

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
        import copy

        with self._lock:
            return copy.deepcopy(self.batch)

    # ---- mutators ----

    def _broadcast(self, event: Dict[str, Any]) -> None:
        for q in list(self.subscribers):
            self.loop.call_soon_threadsafe(q.put_nowait, event)

    def _patch(self, **fields: Any) -> None:
        with self._lock:
            self.batch.update(fields)
        batch_storage.save(self.batch)
        self._broadcast({"type": "batch", "batch": self.snapshot()})

    def _update_item(self, idx: int, **fields: Any) -> None:
        with self._lock:
            self.batch["items"][idx].update(fields)
        batch_storage.save(self.batch)
        self._broadcast(
            {"type": "item", "index": idx, "item": self.batch["items"][idx]}
        )

    # ---- entrypoint ----

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_safe, daemon=True)
        self._thread.start()

    def _run_safe(self) -> None:
        try:
            self._run()
        except Exception as exc:
            self._patch(
                status="failed",
                error=str(exc),
                error_traceback=traceback.format_exc(),
                completed_at=time.time(),
            )

    def _run_one(self, idx: int, item: Dict[str, Any]) -> None:
        """Run a single ticker analysis to completion. Called from the worker pool."""
        if self._cancelled.is_set():
            self._update_item(idx, status="cancelled", completed_at=time.time())
            return
        ticker = item["ticker"]
        form = {**self.batch["config"], "ticker": ticker, "analysis_date": self.batch["analysis_date"]}
        session = build_session(form)
        # Stamp child sessions with the batch's user_id so the per-user
        # storage filter accepts them.
        if self.batch.get("user_id"):
            session["user_id"] = self.batch["user_id"]
        storage.save(session)

        self._update_item(idx, session_id=session["id"], status="running", started_at=time.time())

        child = SessionRunner(session, self.loop)
        # If a cancel has already been requested, propagate before start so
        # the child aborts at the very first chunk.
        if self._cancelled.is_set():
            child.cancel()
        with self._lock:
            self._active_children.append(child)
        self.register_session(child)
        child.start()

        if child._thread is not None:
            child._thread.join()
        with self._lock:
            if child in self._active_children:
                self._active_children.remove(child)

        final_session = child.snapshot()
        stats = final_session.get("stats") or {}
        team_timings = final_session.get("team_timings") or {}
        self._update_item(
            idx,
            status=final_session.get("status", "unknown"),
            completed_at=time.time(),
            final_decision=final_session.get("report_sections", {}).get("final_trade_decision"),
            trader_plan=final_session.get("report_sections", {}).get("trader_investment_plan"),
            error=final_session.get("error"),
            stats=stats,
            team_timings=team_timings,
        )
        self._recompute_totals()

    def _recompute_totals(self) -> None:
        """Sum per-item stats and team timings; broadcast both."""
        totals = {"llm_calls": 0, "tool_calls": 0, "tokens_in": 0, "tokens_out": 0}
        # team_totals[team] = {total_s, count, max_s, avg_s}
        team_acc: Dict[str, Dict[str, float]] = {}
        with self._lock:
            for it in self.batch["items"]:
                s = it.get("stats") or {}
                for k in totals:
                    totals[k] += int(s.get(k, 0) or 0)

                for team, t in (it.get("team_timings") or {}).items():
                    duration = t.get("duration_s")
                    if duration is None:
                        continue
                    acc = team_acc.setdefault(
                        team, {"total_s": 0.0, "count": 0, "max_s": 0.0, "avg_s": 0.0}
                    )
                    acc["total_s"] += float(duration)
                    acc["count"] += 1
                    if duration > acc["max_s"]:
                        acc["max_s"] = float(duration)
            for team, acc in team_acc.items():
                acc["avg_s"] = round(acc["total_s"] / acc["count"], 2) if acc["count"] else 0.0
                acc["total_s"] = round(acc["total_s"], 2)
                acc["max_s"] = round(acc["max_s"], 2)

            prev_totals = self.batch.get("totals") or {}
            prev_teams = self.batch.get("team_totals") or {}
            if prev_totals == totals and prev_teams == team_acc:
                return
            self.batch["totals"] = totals
            self.batch["team_totals"] = team_acc
        batch_storage.save(self.batch)
        self._broadcast({"type": "totals", "totals": totals, "team_totals": team_acc})

    def _run(self) -> None:
        self._patch(status="running", started_at=time.time())

        max_workers = max(1, int(self.batch["config"].get("max_concurrency", 4)))
        items = list(enumerate(self.batch["items"]))

        if max_workers == 1:
            for idx, item in items:
                if self._cancelled.is_set():
                    break
                self._run_one(idx, item)
        else:
            with ThreadPoolExecutor(
                max_workers=max_workers, thread_name_prefix="batch-worker"
            ) as pool:
                futures = [pool.submit(self._run_one, idx, item) for idx, item in items]
                for fut in futures:
                    # Block until each completes; per-ticker errors are already
                    # captured on the item record, so swallow exceptions here so
                    # one bad ticker doesn't cancel the rest.
                    try:
                        fut.result()
                    except Exception:
                        traceback.print_exc()

        if self._cancelled.is_set():
            # Mark any items that never started as cancelled so the UI
            # doesn't leave them showing "pending" forever.
            for it in self.batch["items"]:
                if it.get("status") in (None, "pending"):
                    it["status"] = "cancelled"
            self._patch(status="cancelled", completed_at=time.time())
            return

        self._patch(status="composing_report")
        try:
            report = self._compose_report()
            self._patch(report=report, status="completed", completed_at=time.time())
        except Exception as exc:
            self._patch(
                status="completed_no_report",
                report_error=str(exc),
                completed_at=time.time(),
            )

    # ---- meta-summary ----

    def _compose_report(self) -> str:
        items = self.batch["items"]
        completed = [it for it in items if it.get("final_decision")]
        if not completed:
            raise RuntimeError("No completed analyses to summarize.")

        cfg = self.batch["config"]
        client = create_llm_client(
            provider=cfg["llm_provider"],
            model=cfg["deep_think_llm"],
            base_url=cfg.get("backend_url"),
            openai_reasoning_effort=cfg.get("openai_reasoning_effort"),
            anthropic_effort=cfg.get("anthropic_effort"),
        )
        llm = client.get_llm()

        # Look up positions per analyzed ticker so the meta-summary can frame
        # each call relative to what the user actually holds.
        positions_by_ticker: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for it in completed:
            related = portfolio.find_related(it["ticker"])
            if related:
                positions_by_ticker[it["ticker"]] = related

        def _position_summary(ticker: str) -> str:
            related = positions_by_ticker.get(ticker)
            if not related:
                return "FLAT (no recorded position)"
            parts = []
            for label, pos in sorted(related.items()):
                qty = float(pos.get("qty", 0) or 0)
                if qty == 0:
                    continue
                side = "SHORT" if qty < 0 else "LONG"
                line = f"{side} {abs(qty):g} {label}"
                if pos.get("avg_cost") is not None:
                    line += f" @ {float(pos['avg_cost']):g}"
                if (pos.get("notes") or "").strip():
                    line += f" — {pos['notes'].strip()}"
                parts.append(line)
            return "; ".join(parts) if parts else "FLAT"

        per_ticker_blocks = []
        for it in completed:
            block = (
                f"### {it['ticker']}\n\n"
                f"**User's position:** {_position_summary(it['ticker'])}\n\n"
                f"**Trader plan:**\n{(it.get('trader_plan') or '_(missing)_').strip()}\n\n"
                f"**Final decision:**\n{(it.get('final_decision') or '').strip()}"
            )
            per_ticker_blocks.append(block)

        joined = "\n\n---\n\n".join(per_ticker_blocks)

        # Roll-up of every recorded position so the strategist sees the whole book.
        all_positions = portfolio.load_all()
        if all_positions:
            book_lines = []
            for sym in sorted(all_positions):
                pos = all_positions[sym]
                qty = float(pos.get("qty", 0) or 0)
                if qty == 0:
                    continue
                side = "SHORT" if qty < 0 else "LONG"
                line = f"- {side} {abs(qty):g} {sym}"
                if pos.get("avg_cost") is not None:
                    line += f" @ {float(pos['avg_cost']):g}"
                if (pos.get("notes") or "").strip():
                    line += f" — {pos['notes'].strip()}"
                book_lines.append(line)
            book_block = "User's full book today:\n" + "\n".join(book_lines)
        else:
            book_block = "User's full book today: FLAT (no positions recorded)."

        system = (
            "You are the chief market strategist consolidating a multi-instrument "
            "research batch. You will receive each instrument's trader plan and the "
            "portfolio manager's final decision, ALONG WITH the user's actual "
            "position in that instrument. Produce a single comprehensive daily "
            "report. Every recommendation MUST be expressed as a delta to the "
            "user's actual position. NEVER write 'reduce long exposure' for an "
            "instrument the user is short, or vice versa. For a SHORT position, "
            "trimming = buying back; covering = closing the short fully; adding "
            "= selling more. For a LONG position, trimming = selling; exiting = "
            "selling fully; adding = buying more. Stops on a SHORT sit ABOVE the "
            "entry price; stops on a LONG sit BELOW. If the source material was "
            "framed long-bias on an instrument the user is short (or vice versa), "
            "REINTERPRET the call against the actual position rather than parrot "
            "the source text."
        )
        user = f"""Date: {self.batch['analysis_date']}
Instruments analyzed: {len(completed)}
Output language: {cfg.get('output_language', 'English')}

{book_block}

Produce a daily report with EXACTLY these sections, in this order. In each section, prefix every line with the user's existing side ([LONG], [SHORT], [FLAT]) before the ticker so the framing is unambiguous.

## 1. Where to Add / Initiate
Ranked list. Cases where the call is to grow exposure on the user's side, or to initiate a new position on a flat instrument. For each: side prefix + ticker, entry zone, target(s), stop, size hint, 1-2 sentence thesis.

## 2. Where to Reduce / Cover / Exit
Same format as #1. For SHORT positions, "cover" or "buy back" — never "sell". For LONG positions, "trim" or "exit" — never "cover". Stop levels must be on the correct side of entry.

## 3. Holds & Fades
Tickers where the call is to maintain the existing position or stand aside. One line each, prefixed with the side.

## 4. Cross-asset Themes
3-6 bullets connecting the calls across asset classes (equities vs commodities vs FX vs crypto). Surface divergences and any net-book implications (e.g. "user is short two energy contracts but long an equity beta proxy").

## 5. Risk Watch
What could invalidate the day's calls. For each named risk, identify which positions it threatens and on which side. Macro events, levels to watch, correlated risks.

Be concise and decision-grade. Do not invent numbers — if a target is not in the source material, say so.

Per-instrument source material follows. Each block shows the user's actual position alongside what the analysts said:

{joined}
"""

        msg = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
        content = getattr(msg, "content", None) or ""
        if isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    parts.append(c.get("text", ""))
                elif isinstance(c, str):
                    parts.append(c)
            content = "\n".join(parts)
        return content.strip()


def build_batch(form: Dict[str, Any]) -> Dict[str, Any]:
    """Create a fresh batch record from validated form data."""
    tickers: List[str] = [t.strip().upper() for t in form["tickers"] if t and t.strip()]
    if not tickers:
        raise ValueError("At least one ticker is required.")

    items = [
        {
            "ticker": t,
            "session_id": None,
            "status": "pending",
            "started_at": None,
            "completed_at": None,
            "final_decision": None,
            "trader_plan": None,
            "error": None,
            "stats": {"llm_calls": 0, "tool_calls": 0, "tokens_in": 0, "tokens_out": 0},
            "team_timings": {},
        }
        for t in tickers
    ]

    return {
        "id": uuid.uuid4().hex,
        "analysis_date": form["analysis_date"],
        "created_at": time.time(),
        "started_at": None,
        "completed_at": None,
        "status": "pending",
        "items": items,
        "report": None,
        "report_error": None,
        "error": None,
        "totals": {"llm_calls": 0, "tool_calls": 0, "tokens_in": 0, "tokens_out": 0},
        "team_totals": {},
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
            "analysts": form.get("analysts") or ["market", "social", "news", "fundamentals"],
            "max_concurrency": int(form.get("max_concurrency", 4)),
        },
    }
