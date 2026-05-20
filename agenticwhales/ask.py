"""Ask the fund — templated answers over the user's own corpus.

Phase 2 deliverable #2. Each template is a pure function that:
  1. Pulls data via `web.auth` helpers (one user only — never cross-user).
  2. Aggregates / filters deterministically.
  3. Returns a typed `AskResult` containing markdown + optional table rows
     that the UI renders directly.

No LLM synthesis in v1. The questions are concrete enough that templated
markdown with substituted numbers reads cleanly. LLM narrative is a Phase 2.x
enhancement once we see what users actually ask.

Templates that depend on data the system doesn't have *yet* (disagreement_log
populated only after deliverable #7; behavioral_findings only after #5)
degrade gracefully — they return the "Data not yet available" shape with
an explanation rather than an empty/broken card.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple


# ----------------------------------------------------------------------------
# Result shape
# ----------------------------------------------------------------------------

@dataclass
class AskResult:
    template_id: int
    slug: str
    question: str
    markdown: str                                    # rendered answer body
    data_points: int = 0                             # rows that informed the answer
    table: Optional[List[Dict[str, Any]]] = None     # optional structured table for UI
    cta: Optional[str] = None                        # "do this next" hint
    confidence: str = "ok"                           # 'ok' | 'low' | 'no_data'

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _no_data(template_id: int, slug: str, question: str, reason: str) -> AskResult:
    return AskResult(
        template_id=template_id, slug=slug, question=question,
        markdown=f"_Not enough data yet._\n\n{reason}",
        confidence="no_data",
    )


# ----------------------------------------------------------------------------
# Data fetchers (one-trip helpers, kept thin so each template stays readable)
# ----------------------------------------------------------------------------

def _orders(user_id: str, *, limit: int = 500) -> List[Dict[str, Any]]:
    from web import auth
    return auth.list_paper_orders(user_id, limit=limit) or []


def _outcomes(user_id: str, *, limit: int = 500) -> List[Dict[str, Any]]:
    from agenticwhales import outcomes
    return outcomes.list_outcomes_for_user(user_id, limit=limit) or []


def _join_orders_outcomes(user_id: str) -> List[Dict[str, Any]]:
    """Inner-join paper_orders with decision_outcomes on paper_order_id.
    Orders without resolved outcomes are dropped — calibration questions
    need realized PnL to mean anything."""
    out_by_oid: Dict[str, Dict[str, Any]] = {
        o["paper_order_id"]: o for o in _outcomes(user_id)
    }
    rows: List[Dict[str, Any]] = []
    for o in _orders(user_id):
        outcome = out_by_oid.get(o["id"])
        if not outcome:
            continue
        rows.append({**o, **{f"out_{k}": v for k, v in outcome.items()}})
    return rows


def _journal(user_id: str) -> List[Dict[str, Any]]:
    from web import auth
    return auth.list_journal_entries(user_id, include_drafts=False) or []


def _recipes_by_id(user_id: str) -> Dict[str, Dict[str, Any]]:
    from agenticwhales import recipes as rmod
    return {r.id: r.model_dump(mode="json") for r in rmod.list_for_user(user_id)}


def _parse_ts(ts: Any) -> Optional[datetime]:
    if ts is None:
        return None
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(float(ts), tz=timezone.utc)
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _fmt_pct(p: Optional[float]) -> str:
    return "—" if p is None else f"{p:+.2f}%"


def _fmt_usd(v: Optional[float]) -> str:
    return "—" if v is None else f"${v:,.2f}"


# ----------------------------------------------------------------------------
# Template 1: Which day of the week am I losing money?
# ----------------------------------------------------------------------------

def template_1_dow_pnl(user_id: str) -> AskResult:
    joined = _join_orders_outcomes(user_id)
    if not joined:
        return _no_data(
            1, "dow_pnl", "Which day of the week am I losing money?",
            "Need at least one resolved outcome. Resolve outcomes on Overview and try again.",
        )

    dow_buckets: Dict[str, List[float]] = {d: [] for d in
        ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")}
    dow_index = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for r in joined:
        ts = _parse_ts(r.get("created_at"))
        if ts is None:
            continue
        ret = r.get("out_realized_return_pct")
        if ret is None:
            continue
        dow_buckets[dow_index[ts.weekday()]].append(float(ret))

    rows = []
    for d in dow_index:
        v = dow_buckets[d]
        if not v:
            continue
        avg = sum(v) / len(v)
        wins = sum(1 for x in v if x > 0)
        rows.append({
            "day": d, "trades": len(v),
            "avg_return_pct": round(avg, 3),
            "hit_rate": round(wins / len(v), 3),
        })

    if not rows:
        return _no_data(1, "dow_pnl", "Which day of the week am I losing money?",
                       "Resolved outcomes don't have valid timestamps yet.")

    worst = min(rows, key=lambda r: r["avg_return_pct"])
    best = max(rows, key=lambda r: r["avg_return_pct"])

    lines = [
        f"You traded on **{len(rows)}** distinct weekday(s) across **{sum(r['trades'] for r in rows)}** decisions.",
        "",
        f"- 🟢 **Best day:** {best['day']} — avg return {_fmt_pct(best['avg_return_pct'])} over {best['trades']} trades (hit rate {best['hit_rate']:.0%}).",
        f"- 🔴 **Worst day:** {worst['day']} — avg return {_fmt_pct(worst['avg_return_pct'])} over {worst['trades']} trades (hit rate {worst['hit_rate']:.0%}).",
    ]
    if worst["avg_return_pct"] < 0 and worst["trades"] >= 3:
        lines.append("")
        lines.append(f"> Worth investigating why **{worst['day']}** trades underperform. Check journal entries from those days.")

    return AskResult(
        template_id=1, slug="dow_pnl",
        question="Which day of the week am I losing money?",
        markdown="\n".join(lines),
        data_points=sum(r["trades"] for r in rows),
        table=rows,
    )


# ----------------------------------------------------------------------------
# Template 2: Which thesis is my most / least profitable?
# ----------------------------------------------------------------------------

def template_2_thesis_pnl(user_id: str) -> AskResult:
    joined = _join_orders_outcomes(user_id)
    if not joined:
        return _no_data(
            2, "thesis_pnl", "Which thesis is my most / least profitable?",
            "Need resolved outcomes joined to theses. Fire a thesis with `paper_trade` policy and resolve outcomes.",
        )
    recipes = _recipes_by_id(user_id)

    by_thesis: Dict[str, List[float]] = {}
    for r in joined:
        rid = r.get("recipe_id")
        if not rid:
            continue
        ret = r.get("out_realized_return_pct")
        if ret is None:
            continue
        by_thesis.setdefault(rid, []).append(float(ret))

    if not by_thesis:
        return _no_data(2, "thesis_pnl", "Which thesis is my most / least profitable?",
                       "Resolved outcomes exist but none are linked to a thesis. (Ad-hoc analyses don't count.)")

    rows = []
    for rid, returns in by_thesis.items():
        rname = (recipes.get(rid) or {}).get("name") or rid[:8]
        rows.append({
            "thesis_id": rid,
            "name": rname,
            "trades": len(returns),
            "avg_return_pct": round(sum(returns) / len(returns), 3),
            "total_return_pct": round(sum(returns), 3),
        })
    rows.sort(key=lambda r: r["avg_return_pct"], reverse=True)

    best, worst = rows[0], rows[-1]
    lines = [
        f"You have **{len(rows)}** thesis(es) with resolved outcomes.",
        "",
        f"- 🥇 **Best:** {best['name']} — avg {_fmt_pct(best['avg_return_pct'])} over {best['trades']} fires.",
        f"- 🪨 **Worst:** {worst['name']} — avg {_fmt_pct(worst['avg_return_pct'])} over {worst['trades']} fires." if best is not worst else "_(Only one thesis with data.)_",
    ]
    return AskResult(
        template_id=2, slug="thesis_pnl",
        question="Which thesis is my most / least profitable?",
        markdown="\n".join(lines),
        data_points=sum(r["trades"] for r in rows),
        table=rows,
    )


# ----------------------------------------------------------------------------
# Template 3: How calibrated are my probability estimates?
# ----------------------------------------------------------------------------

def template_3_calibration(user_id: str) -> AskResult:
    outcomes_rows = _outcomes(user_id)
    valid = [r for r in outcomes_rows if r.get("brier_component") is not None]
    if not valid:
        return _no_data(
            3, "calibration", "How calibrated are my probability estimates?",
            "Need resolved outcomes that include a Brier component. Resolve outcomes and try again.",
        )
    brier = sum(float(r["brier_component"]) for r in valid) / len(valid)
    n = len(valid)
    hits = sum(1 for r in valid if r.get("hit"))
    base_rate = hits / n

    # Build a coarse reliability table: bucket predictions into 5 bins.
    bins: Dict[str, List[Tuple[float, bool]]] = {
        "0.0-0.2": [], "0.2-0.4": [], "0.4-0.6": [], "0.6-0.8": [], "0.8-1.0": [],
    }
    for r in valid:
        p = r.get("predicted_prob_of_profit")
        if p is None:
            continue
        p = float(p)
        hit = bool(r.get("hit"))
        if p < 0.2:    bins["0.0-0.2"].append((p, hit))
        elif p < 0.4:  bins["0.2-0.4"].append((p, hit))
        elif p < 0.6:  bins["0.4-0.6"].append((p, hit))
        elif p < 0.8:  bins["0.6-0.8"].append((p, hit))
        else:          bins["0.8-1.0"].append((p, hit))

    table = []
    for label, pairs in bins.items():
        if not pairs: continue
        avg_pred = sum(p for p, _ in pairs) / len(pairs)
        hit_rate = sum(1 for _, h in pairs if h) / len(pairs)
        table.append({
            "bin": label, "n": len(pairs),
            "avg_predicted_prob": round(avg_pred, 3),
            "actual_hit_rate": round(hit_rate, 3),
            "gap": round(avg_pred - hit_rate, 3),
        })

    interp = (
        "well-calibrated — probabilities track reality" if brier < 0.20
        else "rough — calibration head will help once N≥50" if brier < 0.30
        else "significantly miscalibrated — probabilities are not predictive yet"
    )

    lines = [
        f"**Brier score:** {brier:.4f} (lower is better; 0.25 = uninformative).",
        f"**Sample size:** {n} resolved outcomes.",
        f"**Base hit rate:** {base_rate:.1%}.",
        "",
        f"**Verdict:** {interp}.",
    ]
    if n < 50:
        lines.append("")
        lines.append(f"> Calibration head opt-in unlocks at N≥50. You're at {n}.")

    return AskResult(
        template_id=3, slug="calibration",
        question="How calibrated are my probability estimates?",
        markdown="\n".join(lines),
        data_points=n, table=table,
        cta="Resolve more outcomes" if n < 50 else "Enable calibration head opt-in",
    )


# ----------------------------------------------------------------------------
# Template 4: Which model has been right most often?
# ----------------------------------------------------------------------------

def template_4_model_accuracy(user_id: str) -> AskResult:
    joined = _join_orders_outcomes(user_id)
    if not joined:
        return _no_data(
            4, "model_accuracy", "Which model has been right most often?",
            "Need resolved outcomes joined to thesis configs.",
        )
    recipes = _recipes_by_id(user_id)

    # Bucket by deep_model (the synthesizer). PM uses deep_think_llm.
    by_model: Dict[str, List[bool]] = {}
    for r in joined:
        rid = r.get("recipe_id")
        rec = recipes.get(rid) if rid else None
        # Ad-hoc paths might not have a recipe row; fall back to "ad_hoc".
        model = (rec or {}).get("deep_model") or "ad_hoc"
        hit = r.get("out_hit")
        if hit is None: continue
        by_model.setdefault(model, []).append(bool(hit))

    if not by_model:
        return _no_data(4, "model_accuracy", "Which model has been right most often?",
                       "Resolved outcomes don't have a `hit` value yet.")

    rows = []
    for model, hits in by_model.items():
        rows.append({
            "model": model,
            "n": len(hits),
            "hit_rate": round(sum(hits) / len(hits), 3),
        })
    rows.sort(key=lambda r: r["hit_rate"], reverse=True)

    best = rows[0]
    lines = [
        f"You've used **{len(rows)}** distinct deep model(s) across {sum(r['n'] for r in rows)} resolved decisions.",
        "",
        f"- 🎯 **Highest hit rate:** {best['model']} — {best['hit_rate']:.0%} over {best['n']} trades.",
    ]
    if len(rows) > 1:
        worst = rows[-1]
        lines.append(f"- ❄️ **Lowest hit rate:** {worst['model']} — {worst['hit_rate']:.0%} over {worst['n']} trades.")
    if best["n"] < 10:
        lines.append("")
        lines.append("> Small samples; trust this once each model has ≥30 resolved outcomes.")

    return AskResult(
        template_id=4, slug="model_accuracy",
        question="Which model has been right most often?",
        markdown="\n".join(lines),
        data_points=sum(r["n"] for r in rows),
        table=rows,
    )


# ----------------------------------------------------------------------------
# Template 5: Average holding period — does shorter help or hurt?
# ----------------------------------------------------------------------------

def template_5_holding_period(user_id: str) -> AskResult:
    joined = _join_orders_outcomes(user_id)
    valid = [r for r in joined if r.get("expected_hold_days") and r.get("out_realized_return_pct") is not None]
    if not valid:
        return _no_data(
            5, "hold_period", "What's my average holding period — and does shorter help or hurt?",
            "Need resolved outcomes that include an expected hold period.",
        )

    buckets = {"≤7d": [], "8-30d": [], "31-90d": [], "91-365d": [], ">365d": []}
    for r in valid:
        d = int(r["expected_hold_days"])
        ret = float(r["out_realized_return_pct"])
        if d <= 7:        buckets["≤7d"].append(ret)
        elif d <= 30:     buckets["8-30d"].append(ret)
        elif d <= 90:     buckets["31-90d"].append(ret)
        elif d <= 365:    buckets["91-365d"].append(ret)
        else:             buckets[">365d"].append(ret)

    rows = []
    for label, returns in buckets.items():
        if not returns: continue
        rows.append({
            "horizon": label,
            "n": len(returns),
            "avg_return_pct": round(sum(returns) / len(returns), 3),
            "hit_rate": round(sum(1 for x in returns if x > 0) / len(returns), 3),
        })

    if not rows:
        return _no_data(5, "hold_period", "What's my average holding period — and does shorter help or hurt?",
                       "Holding period data is missing on all resolved trades.")
    best = max(rows, key=lambda r: r["avg_return_pct"])
    avg_hold = statistics.mean(int(r["expected_hold_days"]) for r in valid)

    lines = [
        f"**Average expected hold:** {avg_hold:.0f} days across {len(valid)} resolved trades.",
        f"**Best-performing horizon:** {best['horizon']} — avg {_fmt_pct(best['avg_return_pct'])} on {best['n']} trades.",
    ]
    return AskResult(
        template_id=5, slug="hold_period",
        question="What's my average holding period — and does shorter help or hurt?",
        markdown="\n".join(lines),
        data_points=len(valid),
        table=rows,
    )


# ----------------------------------------------------------------------------
# Template 6: When Bull and Bear agreed, did I do better? (DEFERRED to #7)
# ----------------------------------------------------------------------------

def template_6_bull_bear_agreement(user_id: str) -> AskResult:
    """Cross-join disagreement_log × decision_outcomes by session_id, split
    by `rating_agreement`, compare hit rates and average return."""
    from agenticwhales import disagreement as dmod
    rows = dmod.list_for_user(user_id, limit=500)
    if not rows:
        return _no_data(6, "bull_bear_agreement",
                       "When Bull and Bear agreed, did I do better?",
                       "Trigger some Theses with paper_trade or notify policy — "
                       "disagreement is recorded after every fire.")
    # Index outcomes by session_id (each session has exactly one paper_order,
    # so we cross via paper_orders).
    joined = _join_orders_outcomes(user_id)
    outcomes_by_session: Dict[str, Dict[str, Any]] = {
        r.get("session_id"): r for r in joined if r.get("session_id")
    }

    agree_returns: List[float] = []
    disagree_returns: List[float] = []
    for d in rows:
        sid = d.get("session_id")
        outcome = outcomes_by_session.get(sid)
        if not outcome or outcome.get("out_realized_return_pct") is None:
            continue
        bucket = agree_returns if d.get("rating_agreement") else disagree_returns
        bucket.append(float(outcome["out_realized_return_pct"]))

    if not agree_returns and not disagree_returns:
        return _no_data(6, "bull_bear_agreement",
                       "When Bull and Bear agreed, did I do better?",
                       f"Have {len(rows)} disagreement rows but none are joined to "
                       f"resolved outcomes yet. Resolve outcomes and try again.")

    def _stats(bucket):
        if not bucket:
            return None
        return {
            "n": len(bucket),
            "avg_return_pct": round(sum(bucket) / len(bucket), 3),
            "hit_rate": round(sum(1 for x in bucket if x > 0) / len(bucket), 3),
        }

    agree_stats = _stats(agree_returns)
    disagree_stats = _stats(disagree_returns)

    lines = []
    if agree_stats:
        lines.append(
            f"- 🤝 **When Bull and Bear agreed** ({agree_stats['n']} fires): "
            f"avg return {_fmt_pct(agree_stats['avg_return_pct'])}, "
            f"hit rate {agree_stats['hit_rate']:.0%}."
        )
    if disagree_stats:
        lines.append(
            f"- ⚔️ **When they disagreed** ({disagree_stats['n']} fires): "
            f"avg return {_fmt_pct(disagree_stats['avg_return_pct'])}, "
            f"hit rate {disagree_stats['hit_rate']:.0%}."
        )
    if agree_stats and disagree_stats:
        delta = agree_stats["avg_return_pct"] - disagree_stats["avg_return_pct"]
        verdict = (
            "Agreement *helps* — collinear Bull/Bear narratives correlate with better outcomes."
            if delta > 1 else
            "Agreement *hurts* — when both sides converge you do worse, suggesting groupthink."
            if delta < -1 else
            "No clear edge from agreement vs disagreement at this sample size."
        )
        lines.append("")
        lines.append(f"> **Verdict:** {verdict} (delta {delta:+.2f}%)")

    return AskResult(
        template_id=6, slug="bull_bear_agreement",
        question="When Bull and Bear agreed, did I do better?",
        markdown="\n".join(lines) or "_No resolved outcomes joined to disagreement rows._",
        data_points=(agree_stats or {}).get("n", 0) + (disagree_stats or {}).get("n", 0),
        table=[
            {"side": "agreed",    **(agree_stats or {})},
            {"side": "disagreed", **(disagree_stats or {})},
        ] if (agree_stats or disagree_stats) else None,
    )


# ----------------------------------------------------------------------------
# Template 7: Worst 5 decisions + journal context
# ----------------------------------------------------------------------------

def template_7_worst_decisions(user_id: str) -> AskResult:
    joined = _join_orders_outcomes(user_id)
    valid = [r for r in joined if r.get("out_realized_return_pct") is not None]
    if not valid:
        return _no_data(
            7, "worst_decisions", "Show me my worst 5 decisions and what I wrote about them.",
            "No resolved outcomes yet.",
        )
    valid.sort(key=lambda r: float(r["out_realized_return_pct"]))
    worst = valid[:5]

    journal_rows = _journal(user_id)
    journal_by_session: Dict[str, List[Dict[str, Any]]] = {}
    for j in journal_rows:
        sid = j.get("session_id")
        if sid:
            journal_by_session.setdefault(sid, []).append(j)

    lines = ["Your 5 worst decisions by realized return:", ""]
    table = []
    for o in worst:
        sid = o.get("session_id")
        entries = journal_by_session.get(sid, []) if sid else []
        note = entries[0]["body"][:140] if entries else "_(no journal entry)_"
        row = {
            "ticker": o["ticker"],
            "side": o["side"],
            "rating": o.get("pm_rating"),
            "realized_return_pct": round(float(o["out_realized_return_pct"]), 3),
            "session_id": sid,
            "note": note,
        }
        table.append(row)
        lines.append(
            f"- **{o['ticker']}** ({o['side']}, rated {o.get('pm_rating') or '?'}): "
            f"{_fmt_pct(row['realized_return_pct'])} — _{note[:80]}…_"
            if len(note) > 80 else
            f"- **{o['ticker']}** ({o['side']}, rated {o.get('pm_rating') or '?'}): "
            f"{_fmt_pct(row['realized_return_pct'])} — _{note}_"
        )

    return AskResult(
        template_id=7, slug="worst_decisions",
        question="Show me my worst 5 decisions and what I wrote about them.",
        markdown="\n".join(lines),
        data_points=len(worst),
        table=table,
    )


# ----------------------------------------------------------------------------
# Template 8: What did I write before my biggest losses?
# ----------------------------------------------------------------------------

def template_8_writings_before_losses(user_id: str) -> AskResult:
    from datetime import timedelta
    joined = _join_orders_outcomes(user_id)
    losers = [r for r in joined if r.get("out_realized_return_pct") is not None and float(r["out_realized_return_pct"]) < -5.0]
    if not losers:
        return _no_data(
            8, "writings_losses", "What did I write before my biggest losses?",
            "No trades with > -5% realized return yet (lucky you, or not enough data).",
        )
    journal_rows = _journal(user_id)

    findings = []
    for loser in losers[:10]:
        order_ts = _parse_ts(loser.get("created_at"))
        if not order_ts:
            continue
        window_start = order_ts - timedelta(hours=24)
        nearby = [
            j for j in journal_rows
            if (jt := _parse_ts(j.get("created_at")))
            and window_start <= jt <= order_ts
        ]
        if not nearby:
            continue
        findings.append({
            "ticker": loser["ticker"],
            "loss_pct": round(float(loser["out_realized_return_pct"]), 3),
            "entries": [n["body"][:200] for n in nearby[:3]],
        })

    if not findings:
        return _no_data(8, "writings_losses", "What did I write before my biggest losses?",
                       "Big losses exist but no journal entries within 24h before each one. "
                       "Start journaling before placing trades for this question to work.")

    lines = [f"Found **{len(findings)}** loss(es) with journal context within 24h before the trade:", ""]
    for f in findings:
        lines.append(f"### {f['ticker']} ({_fmt_pct(f['loss_pct'])})")
        for e in f["entries"]:
            lines.append(f"> {e}")
        lines.append("")
    return AskResult(
        template_id=8, slug="writings_losses",
        question="What did I write before my biggest losses?",
        markdown="\n".join(lines),
        data_points=len(findings),
    )


# ----------------------------------------------------------------------------
# Template 9: Have I been tilting recently? (DEFERRED to #5)
# ----------------------------------------------------------------------------

def template_9_tilting(user_id: str) -> AskResult:
    from datetime import timedelta
    from agenticwhales import behavioral
    rows = behavioral.list_recent_findings(user_id, limit=20)
    if not rows:
        # Heuristic fallback: scan recent orders for rapid-fire same-ticker repeats.
        orders = sorted(_orders(user_id), key=lambda o: o.get("created_at") or "")
        tilts: List[str] = []
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=14)
        recent = [o for o in orders if (ts := _parse_ts(o.get("created_at"))) and ts >= cutoff]
        # Two trades on the same ticker within 30 minutes counts as a flag.
        seen_ticker_time: Dict[str, datetime] = {}
        for o in recent:
            t = o["ticker"]
            ts = _parse_ts(o.get("created_at"))
            if t in seen_ticker_time and (ts - seen_ticker_time[t]).total_seconds() < 1800:
                tilts.append(f"{t} @ {ts.isoformat()[:16]} (re-entry within 30min)")
            seen_ticker_time[t] = ts
        if tilts:
            md = (
                "_Heuristic only — full behavioral detector ships in deliverable #5._\n\n"
                f"In the last 14 days you re-entered the same ticker within 30 minutes "
                f"on **{len(tilts)}** occasions:\n\n"
                + "\n".join(f"- {t}" for t in tilts[:10])
            )
            return AskResult(
                template_id=9, slug="tilting",
                question="Have I been tilting recently?",
                markdown=md, data_points=len(tilts), confidence="low",
            )
        return AskResult(
            template_id=9, slug="tilting",
            question="Have I been tilting recently?",
            markdown=(
                "**No obvious tilt patterns** in the last 14 days (heuristic only).\n\n"
                "Full behavioral pattern detection — tilt, revenge-trading, anchoring, "
                "overconfidence — ships in Phase 2 deliverable #5. Until then this answer "
                "is a single rapid-re-entry heuristic."
            ),
            data_points=0, confidence="low",
            cta="Full detector ships in Phase 2 deliverable #5",
        )
    lines = [f"Found **{len(rows)}** behavioral finding(s) in the last 14 days:", ""]
    for r in rows[:8]:
        summary = (r.get("evidence") or {}).get("summary") or "(no summary)"
        ack = " ✓ acknowledged" if r.get("acknowledged") else ""
        dismissed = " ⊘ dismissed" if r.get("dismissed") else ""
        lines.append(
            f"- **{r['pattern']}** (severity {float(r['severity']):.2f}){ack}{dismissed} — {summary}"
        )
    return AskResult(
        template_id=9, slug="tilting",
        question="Have I been tilting recently?",
        markdown="\n".join(lines),
        data_points=len(rows),
        table=[{
            "pattern": r["pattern"],
            "severity": float(r["severity"]),
            "summary": (r.get("evidence") or {}).get("summary"),
            "created_at": r.get("created_at"),
        } for r in rows],
    )


# ----------------------------------------------------------------------------
# Template 10: What's my edge?
# ----------------------------------------------------------------------------

def template_10_my_edge(user_id: str) -> AskResult:
    joined = _join_orders_outcomes(user_id)
    valid = [r for r in joined if r.get("out_hit") is not None]
    if len(valid) < 5:
        return _no_data(
            10, "my_edge", "What's my edge?",
            f"Need at least 5 resolved outcomes to look for patterns. You have {len(valid)}.",
        )

    # Bucket by rating × ticker-class (use first letter as a coarse class).
    overall_hit = sum(1 for r in valid if r["out_hit"]) / len(valid)
    by_rating: Dict[str, List[bool]] = {}
    for r in valid:
        rating = r.get("pm_rating") or "?"
        by_rating.setdefault(rating, []).append(bool(r["out_hit"]))

    rows = []
    for rating, hits in by_rating.items():
        if len(hits) < 3: continue
        hr = sum(hits) / len(hits)
        rows.append({
            "rating": rating, "n": len(hits),
            "hit_rate": round(hr, 3),
            "vs_baseline": round(hr - overall_hit, 3),
        })
    rows.sort(key=lambda r: r["vs_baseline"], reverse=True)

    if not rows:
        return _no_data(10, "my_edge", "What's my edge?",
                       "Not enough samples per rating to find a pattern. Need ≥3 outcomes per rating.")

    best = rows[0]
    lines = [
        f"**Overall hit rate:** {overall_hit:.1%} across {len(valid)} trades.",
        "",
        f"- 🎯 **Your strongest rating:** **{best['rating']}** at {best['hit_rate']:.1%} "
        f"({best['vs_baseline']:+.1%} vs your baseline) over {best['n']} trades.",
    ]
    if len(rows) > 1:
        worst = rows[-1]
        lines.append(f"- ⚠️ **Your weakest rating:** **{worst['rating']}** at {worst['hit_rate']:.1%} "
                     f"({worst['vs_baseline']:+.1%} vs baseline) over {worst['n']} trades.")
    if len(valid) < 30:
        lines.append("")
        lines.append("> Small sample. This becomes meaningful around 50+ resolved outcomes.")

    return AskResult(
        template_id=10, slug="my_edge",
        question="What's my edge?",
        markdown="\n".join(lines),
        data_points=len(valid),
        table=rows,
    )


# ----------------------------------------------------------------------------
# Registry + dispatch
# ----------------------------------------------------------------------------

TEMPLATES: Dict[int, Tuple[str, str, Callable[[str], AskResult]]] = {
    1:  ("dow_pnl",            "Which day of the week am I losing money?",                                  template_1_dow_pnl),
    2:  ("thesis_pnl",         "Which thesis is my most / least profitable?",                                template_2_thesis_pnl),
    3:  ("calibration",        "How calibrated are my probability estimates?",                              template_3_calibration),
    4:  ("model_accuracy",     "Which model has been right most often?",                                    template_4_model_accuracy),
    5:  ("hold_period",        "What's my average holding period — and does shorter help or hurt?",         template_5_holding_period),
    6:  ("bull_bear_agreement","When Bull and Bear agreed, did I do better?",                                template_6_bull_bear_agreement),
    7:  ("worst_decisions",    "Show me my worst 5 decisions and what I wrote about them.",                  template_7_worst_decisions),
    8:  ("writings_losses",    "What did I write before my biggest losses?",                                  template_8_writings_before_losses),
    9:  ("tilting",            "Have I been tilting recently?",                                              template_9_tilting),
    10: ("my_edge",            "What's my edge?",                                                            template_10_my_edge),
}


def list_templates() -> List[Dict[str, Any]]:
    """Surface the menu of questions for the UI to render as buttons."""
    return [
        {"template_id": tid, "slug": slug, "question": question}
        for tid, (slug, question, _) in TEMPLATES.items()
    ]


def answer(user_id: str, template_id: int) -> AskResult:
    """Dispatch to one of the 10 templates. Raises KeyError on unknown id."""
    entry = TEMPLATES.get(int(template_id))
    if entry is None:
        raise KeyError(f"unknown template_id: {template_id}")
    _, _, fn = entry
    return fn(user_id)
