"""Background analysis runner: drives AgenticWhalesGraph and streams events."""

from __future__ import annotations

import asyncio
import threading
import time
import traceback
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from agenticwhales.default_config import DEFAULT_CONFIG
from agenticwhales.graph.trading_graph import AgenticWhalesGraph
from agenticwhales import portfolio
from agenticwhales.agents.schemas import (
    GuardOutcome,
    OrderSide,
    OutputPolicy,
    PaperAccount,
    PaperPosition,
    PortfolioDecision,
    PortfolioRating,
)
from agenticwhales.market_snapshot import fetch_snapshot_block

from cli.stats_handler import StatsCallbackHandler

from . import storage


# ---- Mirrors cli/main.py MessageBuffer mappings ----

ANALYST_ORDER = ["market", "quant", "social", "news", "fundamentals"]

ANALYST_AGENT_NAMES = {
    "market": "Market Analyst",
    "quant": "Quant Analyst",
    "social": "Social Analyst",
    "news": "News Analyst",
    "fundamentals": "Fundamentals Analyst",
}

ANALYST_REPORT_MAP = {
    "market": "market_report",
    "quant": "quant_radar",
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


class SessionRunner:
    """Owns one analysis run, its in-memory state, and websocket fan-out."""

    def __init__(self, session: Dict[str, Any], loop: asyncio.AbstractEventLoop):
        self.session = session
        self.loop = loop
        self.subscribers: List[asyncio.Queue] = []
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._cancel_requested = threading.Event()

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

    def cancel(self) -> bool:
        """Request cancellation. Returns True if the request was accepted."""
        with self._lock:
            status = self.session.get("status")
            if status not in ("pending", "running"):
                return False
        self._cancel_requested.set()
        return True

    def is_cancelled(self) -> bool:
        return self._cancel_requested.is_set()

    def _run_safe(self) -> None:
        try:
            self._run()
        except Exception as exc:
            # Treat exceptions raised after a cancel as cancellation, not failure
            # — the inner stream() can blow up arbitrarily when the host process
            # tears down its session/connection.
            if self._cancel_requested.is_set():
                self._finalize_cancelled()
                return
            self._set_session(
                status="failed",
                error=str(exc),
                error_traceback=traceback.format_exc(),
                completed_at=time.time(),
            )

    def _finalize_cancelled(self) -> None:
        self._set_session(status="cancelled", completed_at=time.time())

    # ---- Phase 1 post-decision hook ----

    def _post_decision_hook(self) -> None:
        """After a recipe-fired session completes, drive RiskGuard + paper-trade.

        Honors the recipe's `output_policy`:
          - "notify" / "assist_only" — no order, no event.
          - "alert_conviction" — broadcast a "conviction_alert" WS event if
            the conviction score crosses the recipe's threshold; no order.
          - "paper_trade" — RiskGuard.evaluate → place_order → record conviction.

        For ad-hoc sessions (no recipe_id), records a conviction score against
        the user's portfolio but does NOT place orders — paper-trade is recipe-
        scoped only.
        """
        from agenticwhales import paper, risk as risk_mod, recipes as recipes_mod
        from agenticwhales.audit import impersonate
        from agenticwhales.observability import METRICS
        from . import auth

        pm_dict = self.session.get("pm_decision")
        if not pm_dict:
            return  # No structured decision (free-text fallback fired).

        user_id = self.session.get("user_id")
        if not user_id:
            return

        decision = PortfolioDecision.model_validate(pm_dict)
        ticker = self.session["ticker"].upper()
        recipe_id = self.session.get("recipe_id")
        fire_id = self.session.get("fire_id") or self.session["id"]
        session_id = self.session["id"]

        # Pull recipe (if any) for output_policy + conviction_threshold.
        recipe = recipes_mod.load(recipe_id) if recipe_id else None
        output_policy = (
            OutputPolicy(recipe.output_policy.value)
            if recipe and hasattr(recipe.output_policy, "value")
            else OutputPolicy(recipe.output_policy) if recipe
            else OutputPolicy.NOTIFY
        )
        conviction = paper.score_from_decision(decision)

        # Always record the conviction score — useful timeseries for the UI.
        auth.insert_conviction_score({
            "user_id": user_id,
            "recipe_id": recipe_id,
            "session_id": session_id,
            "ticker": ticker,
            "rating": decision.rating.value if hasattr(decision.rating, "value") else str(decision.rating),
            "conviction_score": conviction,
            "expected_return_pct": decision.expected_return_pct,
            "expected_volatility_pct": decision.expected_volatility_pct,
            "prob_of_profit": decision.prob_of_profit,
            "recorded_at": datetime.utcnow().isoformat(),
        })

        # Phase 2: auto-draft a journal entry on every completed session.
        # The user can open, edit, and commit it (flipping is_draft=false) on
        # /fund. Drafts are the cheapest entry point into the journaling
        # habit — there's something there waiting, not a blank page.
        try:
            self._auto_draft_journal(
                user_id=user_id, session_id=session_id, recipe_id=recipe_id,
                decision=decision, ticker=ticker, conviction=conviction,
            )
        except Exception:
            traceback.print_exc()

        # Notify-only / assist-only: stop here.
        if output_policy in (OutputPolicy.NOTIFY, OutputPolicy.ASSIST_ONLY):
            return

        if output_policy == OutputPolicy.ALERT_CONVICTION:
            threshold = recipe.conviction_threshold if recipe else 7
            if conviction >= threshold:
                self._broadcast({
                    "type": "conviction_alert",
                    "ticker": ticker,
                    "rating": decision.rating.value if hasattr(decision.rating, "value") else str(decision.rating),
                    "conviction_score": conviction,
                    "threshold": threshold,
                })
            return

        # paper_trade policy: gate → size → place. Everything inside the
        # impersonation block is audit-logged.
        from agenticwhales.audit import impersonate
        impersonation = impersonate(user_id, "scheduler_fire", fire_id=fire_id)
        token = impersonation.__enter__()
        try:
            self._do_paper_trade(
                token=token, decision=decision, ticker=ticker, conviction=conviction,
                recipe_id=recipe_id, fire_id=fire_id, session_id=session_id,
            )
        finally:
            impersonation.__exit__(None, None, None)

    def _do_paper_trade(
        self,
        token,
        decision: PortfolioDecision,
        ticker: str,
        conviction: int,
        recipe_id: Optional[str],
        fire_id: str,
        session_id: str,
    ) -> None:
        from agenticwhales import behavioral, paper, risk as risk_mod
        from agenticwhales.observability import METRICS
        from . import auth

        user_id = token.user_id

        # Phase 2 #5: tilt/revenge cooldown circuit-breaker. Opt-in. If the
        # user has consented AND a tilt/revenge finding fired in the last 60
        # min, block this order with a `tilt_cooldown` risk event. Detection
        # itself runs by default; the *action* (blocking) is gated.
        cooldown_finding = behavioral.cooldown_in_effect(user_id)
        if cooldown_finding:
            risk_mod.record_event(
                token, recipe_id=recipe_id, session_id=session_id, ticker=ticker,
                rule="tilt_cooldown",
                details={
                    "pattern": cooldown_finding.get("pattern"),
                    "severity": cooldown_finding.get("severity"),
                    "summary": (cooldown_finding.get("evidence") or {}).get("summary"),
                    "cooldown_minutes": behavioral.COOLDOWN_MIN,
                },
            )
            self._broadcast({
                "type": "risk_event", "rule": "tilt_cooldown",
                "ticker": ticker, "pattern": cooldown_finding.get("pattern"),
            })
            return

        limits_row = auth.load_risk_limits(user_id) or auth._default_risk_limits_row(user_id)
        limits = risk_mod.RiskLimits(
            max_position_pct=float(limits_row.get("max_position_pct", 0.10)),
            max_daily_drawdown_pct=float(limits_row.get("max_daily_drawdown_pct", 0.03)),
            max_slippage_bps=int(limits_row.get("max_slippage_bps", 10)),
            kelly_fraction_cap=float(limits_row.get("kelly_fraction_cap", 0.10)),
            global_kill_switch=bool(limits_row.get("global_kill_switch", False)),
            allow_shorts=bool(limits_row.get("allow_shorts", False)),
        )

        account_row = auth.load_paper_account(user_id) or {
            "user_id": user_id,
            "cash": paper.DEFAULT_STARTING_CASH,
            "starting_cash": paper.DEFAULT_STARTING_CASH,
            "realized_pnl": 0.0,
            "short_collateral_reserved": 0.0,
            "nav_open_today": None,
            "nav_open_today_date": None,
        }
        account = PaperAccount(
            user_id=user_id,
            starting_cash=float(account_row.get("starting_cash", paper.DEFAULT_STARTING_CASH)),
            cash=float(account_row["cash"]),
            short_collateral_reserved=float(account_row.get("short_collateral_reserved", 0.0)),
            realized_pnl=float(account_row.get("realized_pnl", 0.0)),
            nav_open_today=float(account_row["nav_open_today"]) if account_row.get("nav_open_today") else None,
            nav_open_today_date=account_row.get("nav_open_today_date"),
        )

        position_rows = auth.list_paper_positions(user_id)
        positions = [
            PaperPosition(
                user_id=user_id, ticker=r["ticker"], qty=float(r["qty"]),
                avg_cost=float(r["avg_cost"]),
                last_price=float(r["last_price"]) if r.get("last_price") is not None else None,
            ) for r in position_rows
        ]

        last_price = self._latest_price_for(ticker)
        sizing = paper.kelly_sizing(
            decision, nav=account.cash + sum(p.qty * (p.last_price or p.avg_cost) for p in positions),
            last_price=last_price,
            kelly_fraction_cap=limits.kelly_fraction_cap,
            user_id=user_id,                    # Phase 2 #3: apply calibration if opted in
        )

        if sizing.qty == 0:
            # Demis review D2/D9 — surface the *reason* the system abstained.
            # Otherwise a user firing a recipe with output_policy='paper_trade'
            # sees no order land and has no way to debug whether (a) Kelly
            # said no bet, (b) PM scalars were missing, (c) rating was Hold,
            # or (d) last_price lookup returned 0.
            reason = self._abstain_reason(decision, sizing, last_price)
            risk_mod.record_event(
                token, recipe_id=recipe_id, session_id=session_id, ticker=ticker,
                rule="abstain",
                details={
                    "reason": reason,
                    "rating": decision.rating.value if hasattr(decision.rating, "value") else str(decision.rating),
                    "prob_of_profit": decision.prob_of_profit,
                    "expected_return_pct": decision.expected_return_pct,
                    "expected_volatility_pct": decision.expected_volatility_pct,
                    "kelly_fraction": sizing.fraction,
                    "last_price": last_price,
                },
            )
            self._broadcast({
                "type": "risk_event", "rule": "abstain",
                "ticker": ticker, "reason": reason,
            })
            return

        guard = risk_mod.RiskGuard(
            user_id=user_id, limits=limits, account=account, positions=positions,
        )
        outcome = guard.evaluate(
            decision=decision, ticker=ticker,
            target_qty=abs(sizing.qty),
            last_price=self._latest_price_for(ticker),
        )

        if outcome.rule:
            # Either a hard block or a partial clamp — surface the event.
            risk_mod.record_event(
                token,
                recipe_id=recipe_id, session_id=session_id, ticker=ticker,
                rule=outcome.rule,
                details={
                    "target_qty": abs(sizing.qty),
                    "allowed_qty": outcome.allowed_qty,
                    "reason": outcome.reason,
                },
            )
            self._broadcast({
                "type": "risk_event",
                "rule": outcome.rule,
                "ticker": ticker,
                "target_qty": abs(sizing.qty),
                "allowed_qty": outcome.allowed_qty,
                "reason": outcome.reason,
            })
            if METRICS.enabled:
                METRICS.risk_event.labels(rule=outcome.rule).inc()
            if not outcome.allowed:
                return

        # Pick the side from existing position + direction.
        side = self._side_for(sizing.direction, positions, ticker)
        result = paper.place_order(
            token,
            fire_id=fire_id, session_id=session_id, recipe_id=recipe_id,
            ticker=ticker, side=side, qty=abs(sizing.qty),
            market_price=self._latest_price_for(ticker),
            slippage_bps=limits.max_slippage_bps,
            decision=decision, conviction=conviction,
            kelly_fraction=sizing.fraction, guard=outcome,
        )
        if METRICS.enabled:
            METRICS.paper_order.labels(side=side.value, status=result.status.value).inc()

        self._broadcast({
            "type": "paper_order",
            "order_id": result.order_id,
            "status": result.status.value,
            "ticker": ticker, "side": side.value,
            "qty": result.qty, "fill_price": result.fill_price,
            "idempotent": result.idempotent,
        })

    def _auto_draft_journal(
        self,
        *,
        user_id: str,
        session_id: str,
        recipe_id: Optional[str],
        decision: PortfolioDecision,
        ticker: str,
        conviction: int,
    ) -> None:
        """Compose a journal-entry draft from the PM decision the user can
        edit + commit. The draft text is intentionally bullet-y rather than
        prose — gives the user a scaffold without putting words in their
        mouth.

        Drafts are deduplicated per session: if one already exists for this
        session, we don't write another."""
        import uuid as _uuid
        from . import auth

        existing = auth.list_journal_entries(
            user_id, session_id=session_id, kind="auto_draft", limit=1,
        )
        if existing:
            return

        rating = decision.rating.value if hasattr(decision.rating, "value") else str(decision.rating)
        lines = [
            f"## {ticker} — {rating} (conviction {conviction}/10)",
            "",
            "**The fund's read:**",
            "- " + (decision.executive_summary or "(no executive summary)").replace("\n", "\n  ").strip(),
            "",
            "**Key scalars:**",
            f"- Expected return: {decision.expected_return_pct}%" if decision.expected_return_pct is not None else "- Expected return: not provided",
            f"- Volatility: {decision.expected_volatility_pct}% annualized" if decision.expected_volatility_pct is not None else "- Volatility: not provided",
            f"- Probability of profit: {decision.prob_of_profit:.0%}" if decision.prob_of_profit is not None else "- Probability of profit: not provided",
            f"- Expected hold: {decision.expected_hold_days} days" if decision.expected_hold_days is not None else "- Expected hold: not provided",
            "",
            "**Your read:** _replace with what you actually think — agree, disagree, what you'd do differently._",
        ]
        now = datetime.utcnow().isoformat()
        auth.save_journal_entry({
            "id": _uuid.uuid4().hex,
            "user_id": user_id,
            "session_id": session_id,
            "paper_order_id": None,
            "thesis_id": recipe_id,
            "kind": "auto_draft",
            "body": "\n".join(lines),
            "sentiment_score": None,
            "is_draft": True,
            "created_at": now,
            "updated_at": now,
        })

    def _maybe_apply_adaptive_depth(self, config: dict) -> None:
        """Phase 2 #9. Run a 3-sample quick-model pre-pass; escalate this
        fire if the samples disagree above the user's threshold.

        Skips entirely when:
          - no `user_id` is set (anonymous CLI runs aren't tracked anyway)
          - the user's `risk_limits.adaptive_depth_variance_threshold` is 0
          - `agenticwhales.llm_clients.factory.create_llm_client` errors
            (no key, missing provider) — we just keep the original config

        On escalation we mutate the per-fire config dict in place:
          - `quick_think_llm` ← `deep_think_llm` (analysts upgrade to deep)
          - `max_debate_rounds` += 1 (one extra Bull/Bear round)
        """
        from agenticwhales.adaptive import should_escalate, DEFAULT_VARIANCE_THRESHOLD
        from . import auth

        user_id = self.session.get("user_id")
        if not user_id:
            return
        limits = auth.load_risk_limits(user_id) or auth._default_risk_limits_row(user_id)
        threshold = float(limits.get("adaptive_depth_variance_threshold", 0) or 0)
        if threshold <= 0:
            return

        # Generate 3 quick samples. Best-effort — provider errors get
        # swallowed because the main debate is what matters.
        samples: list[str] = []
        try:
            from agenticwhales.llm_clients.factory import create_llm_client
            client = create_llm_client(
                provider=config["llm_provider"],
                quick_think_llm=config["quick_think_llm"],
                deep_think_llm=config["deep_think_llm"],
                backend_url=config.get("backend_url"),
            )
            quick = client.get_quick_thinking_llm()
        except Exception:
            return  # No client — keep original config.

        ticker = self.session["ticker"]
        date = self.session["analysis_date"]
        prompt = (
            f"You are scanning {ticker} as of {date}. In one sentence, "
            f"give your bias (long / short / neutral) and your top reason. "
            "Be concise — under 25 words."
        )
        for _ in range(3):
            try:
                resp = quick.invoke(prompt)
                text = getattr(resp, "content", None) or str(resp)
                if text:
                    samples.append(text)
            except Exception:
                # Single-sample failure → skip; the gate is over the
                # samples we DO get.
                continue
        if len(samples) < 2:
            return

        if should_escalate(samples, threshold=threshold):
            # Escalate.
            old_quick = config["quick_think_llm"]
            old_rounds = config.get("max_debate_rounds", 1)
            config["quick_think_llm"] = config["deep_think_llm"]
            config["max_debate_rounds"] = old_rounds + 1
            config["max_risk_discuss_rounds"] = config.get("max_risk_discuss_rounds", 1) + 1
            self._broadcast({
                "type": "adaptive_depth_escalation",
                "samples": samples,
                "old_quick": old_quick,
                "new_quick": config["quick_think_llm"],
                "rounds": config["max_debate_rounds"],
            })

    def _abstain_reason(self, decision: PortfolioDecision, sizing, last_price: float) -> str:
        """Human-readable explanation for why Kelly returned zero. Drives the
        UI abstain card so users aren't left wondering 'did anything happen?'."""
        if decision.rating == PortfolioRating.HOLD:
            return "Rating is Hold — no directional view, intentional no-trade."
        if last_price is None or last_price <= 0:
            return f"Last price lookup failed ({last_price}); can't size order."
        if decision.prob_of_profit is None:
            return "PM omitted prob_of_profit; Kelly needs it to size."
        if decision.expected_return_pct is None:
            return "PM omitted expected_return_pct; Kelly needs it to size."
        if sizing.fraction == 0:
            return (
                f"Kelly math says no bet: p={decision.prob_of_profit}, "
                f"er={decision.expected_return_pct}% — fractional Kelly is "
                f"≤0, meaning the expected edge doesn't justify the risk."
            )
        return "Kelly returned zero quantity (rounding floor)."

    def _latest_price_for(self, ticker: str) -> float:
        """Best-effort last price from market_snapshot + paper_positions cache."""
        # Try the position's cached last_price first.
        for line in (self.session.get("market_snapshot") or "").splitlines():
            if "Latest close" in line or "latest close" in line:
                try:
                    # Heuristic: pull the first float after the colon.
                    after = line.split(":", 1)[1]
                    for tok in after.replace("$", " ").replace(",", " ").split():
                        try:
                            return float(tok)
                        except ValueError:
                            continue
                except Exception:
                    pass
        # Fall back to a sentinel that won't trigger a non-zero order.
        return 0.0

    def _side_for(
        self,
        direction: int,
        positions: list,
        ticker: str,
    ) -> OrderSide:
        """Map a Kelly direction onto a paper-order side given current holdings.

        direction > 0:
          - flat / long → BUY
          - short → COVER
        direction < 0:
          - flat / short → SHORT (only if allow_shorts; caller should already
            have skipped sizing if not)
          - long → SELL
        """
        existing_qty = 0.0
        for p in positions:
            if p.ticker.upper() == ticker.upper():
                existing_qty = p.qty
                break
        if direction > 0:
            return OrderSide.COVER if existing_qty < 0 else OrderSide.BUY
        if direction < 0:
            return OrderSide.SELL if existing_qty > 0 else OrderSide.SHORT
        return OrderSide.BUY  # caller should not reach here with direction=0

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

        # Phase 2 #9 wiring: adaptive depth pre-pass. Three cheap
        # quick-model samples on a one-shot rating prompt; if they disagree
        # above the user's threshold we upgrade THIS fire to use the deep
        # model for the analysts AND bump research_depth by one round. The
        # cost of three samples is ~0.5% of a typical full-debate cost, so
        # the pre-pass pays for itself when it catches even one hard call
        # that would otherwise have gotten the cheap-and-wrong treatment.
        try:
            self._maybe_apply_adaptive_depth(config)
        except Exception:
            traceback.print_exc()

        graph = AgenticWhalesGraph(
            analysts, config=config, debug=False, callbacks=[stats_handler]
        )
        # Phase 2 #4 wiring: stamp the runner's user_id onto the graph so
        # `_augment_with_memory_v2` can scope retrieval to this user's
        # journal corpus. The graph is per-session; safe to mutate.
        graph.user_id = self.session.get("user_id")

        if self._cancel_requested.is_set():
            self._finalize_cancelled()
            return

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
            if self._cancel_requested.is_set():
                self._finalize_cancelled()
                return
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
                # Phase 1: capture the structured PortfolioDecision so the
                # post-decision hook can drive paper-trade placement. The
                # dict form survives the LangGraph state serialization.
                if chunk.get("pm_decision"):
                    with self._lock:
                        self.session["pm_decision"] = chunk["pm_decision"]

            # Snapshot accumulated token / call counts after each chunk so the UI
            # streams progress instead of waiting for completion.
            self._set_stats(stats_handler.get_stats())

        for name in list(self.session["agent_status"].keys()):
            self._set_status(name, "completed")
        self._set_stats(stats_handler.get_stats())
        self._set_session(status="completed", completed_at=time.time())

        # Phase 1: record fire cost (debits recipe_usage + user_spend_daily
        # + llm_call_log). Best-effort — never blocks the completed session.
        try:
            from agenticwhales.llm_clients.cost_middleware import record_fire_cost
            cfg = self.session.get("config") or {}
            user_id = self.session.get("user_id")
            started_at = self.session.get("started_at") or self.session.get("created_at")
            completed_at = self.session.get("completed_at") or time.time()
            wall_ms = int((completed_at - started_at) * 1000) if started_at else None
            if user_id:
                record_fire_cost(
                    user_id=user_id,
                    recipe_id=self.session.get("recipe_id"),
                    session_id=self.session["id"],
                    provider=cfg.get("llm_provider", ""),
                    quick_model=cfg.get("quick_think_llm", ""),
                    deep_model=cfg.get("deep_think_llm", ""),
                    stats=self.session.get("stats") or {},
                    wall_time_ms=wall_ms,
                )
        except Exception:
            traceback.print_exc()

        # Phase 1 post-decision hook: if this session was recipe-fired with a
        # paper-trade output policy AND the PM produced a structured decision,
        # run RiskGuard + paper-order placement. Failures here are isolated —
        # the user-visible session is already "completed" by the time we
        # reach this point, so a hook error becomes a risk_event, not a
        # session-failure status flip.
        # Phase 3 #3 — when this session is one leg of a multi-TF fan-out,
        # the orchestrator runs the hook once against the merged decision.
        if self.session.get("skip_post_decision"):
            return
        try:
            self._post_decision_hook()
        except Exception as exc:
            traceback.print_exc()
            self._broadcast({
                "type": "risk_event",
                "rule": "hook_error",
                "details": {"error": str(exc)},
            })

        # Phase 2 #5: behavioral pattern scan. Cheap (single user, ≤500 rows)
        # so we run it after every session rather than only nightly. Findings
        # surface immediately on /fund; cooldown circuit-breaker reads them
        # on the *next* paper-trade fire.
        try:
            from agenticwhales import behavioral
            user_id = self.session.get("user_id")
            if user_id:
                behavioral.scan_user(user_id)
        except Exception:
            traceback.print_exc()

        # Phase 2 #7: record Bull/Bear disagreement + optionally inject the
        # Classical Analyst as a third voice. Cosine similarity over the
        # debate histories — deterministic, fast, no provider calls.
        try:
            self._record_disagreement_and_maybe_inject_classical()
        except Exception:
            traceback.print_exc()

    def _record_disagreement_and_maybe_inject_classical(self) -> None:
        """Phase 2 #7. Compute disagreement index + auto-inject Classical
        when configured. Best-effort; failures never fail the user's session."""
        from agenticwhales import disagreement, classical, recipes as recipes_mod

        user_id = self.session.get("user_id")
        if not user_id:
            return
        sections = self.session.get("report_sections") or {}
        bull = sections.get("bull_history") or ""
        bear = sections.get("bear_history") or ""
        if not bull and not bear:
            return  # debate didn't run (analysts-only flow)

        recipe_id = self.session.get("recipe_id")
        recipe_row: dict = {}
        if recipe_id:
            try:
                rec = recipes_mod.load(recipe_id)
                if rec:
                    recipe_row = rec.model_dump(mode="json")
            except Exception:
                pass

        snapshot = disagreement.record_disagreement(
            user_id=user_id,
            session_id=self.session["id"],
            bull_history=bull, bear_history=bear,
            bull_model=recipe_row.get("bull_model"),
            bear_model=recipe_row.get("bear_model"),
            recipe_id=recipe_id,
        )
        self._broadcast({
            "type": "disagreement",
            "similarity": snapshot.similarity,
            "rating_agreement": snapshot.rating_agreement,
            "auto_injecting_classical": disagreement.should_auto_inject(recipe_row, snapshot.similarity),
        })

        # Auto-inject the Classical Analyst as a third voice when Bull/Bear
        # are too consensus-y. Stash its decision on the session so the UI
        # can surface "Classical disagrees" cards.
        if disagreement.should_auto_inject(recipe_row, snapshot.similarity):
            try:
                result = classical.analyze_classical(
                    self.session["ticker"], self.session["analysis_date"],
                )
                if result is not None:
                    with self._lock:
                        self.session["classical_decision"] = result.decision.model_dump(mode="json")
                        self.session["classical_radar"] = result.radar.model_dump(mode="json")
                        self.session["classical_score"] = result.aggregate_score
                    self._broadcast({
                        "type": "classical_voice",
                        "rating": result.decision.rating.value,
                        "aggregate_score": result.aggregate_score,
                    })
            except Exception:
                traceback.print_exc()

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


def config_signature(payload: Dict[str, Any]) -> str:
    """Opaque signature distinguishing meaningfully different runs. Two
    submissions with the same signature on the same (user, ticker,
    analysis_date) within the cache TTL hit the cache instead of re-running."""
    parts = (
        (payload.get("llm_provider") or "").lower(),
        (payload.get("quick_think_llm") or "").lower(),
        (payload.get("deep_think_llm") or "").lower(),
        int(payload.get("research_depth") or 0),
        ",".join(sorted(payload.get("analysts") or [])),
        (payload.get("output_language") or "").lower(),
        (payload.get("google_thinking_level") or ""),
        (payload.get("openai_reasoning_effort") or ""),
        (payload.get("anthropic_effort") or ""),
    )
    return "|".join(str(p) for p in parts)


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
