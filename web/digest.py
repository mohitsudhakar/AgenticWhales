"""Weekly discipline digest — the retention heartbeat.

Deterministic summary of the user's PREVIOUS completed week (Mon–Sun), built
entirely from stored rows (no LLM, no new numbers). One digest per user per
week, idempotent by pk `user_id|week_start`.

Channel split (compliance review): the in-app payload may carry realized P&L;
the EMAIL body carries counts, streak, and score delta only — dollars stay
behind auth.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import time
from typing import Dict, Optional

from agenticwhales import coach, coach_rules
from agenticwhales.transactions.models import Transaction
from web import auth, email_service

log = logging.getLogger(__name__)


def week_start_for(today: Optional[_dt.date] = None) -> _dt.date:
    """Monday of the most recent COMPLETED week."""
    today = today or _dt.datetime.now(_dt.timezone.utc).date()
    return today - _dt.timedelta(days=today.weekday() + 7)


def build_digest(user_id: str, week_start: _dt.date) -> Optional[Dict]:
    """Returns the digest payload, or None when the user has no audit history."""
    audits = auth.list_coach_audits(user_id, limit=60)
    if not audits:
        return None
    week_end = week_start + _dt.timedelta(days=6)
    ws, we = week_start.isoformat(), week_end.isoformat()

    txns = []
    for t in auth.get_coach_trades(user_id):
        try:
            txns.append(Transaction(**t))
        except Exception:  # noqa: BLE001
            continue
    trips = coach.reconstruct_round_trips(txns)
    closed = [t for t in trips if ws <= t.exit_date[:10] <= we]

    events = auth.list_coach_rule_events(user_id, limit=500)
    week_events = [e for e in events if ws <= (e.get("occurred_on") or "") <= we]
    violations_by_kind: Dict[str, int] = {}
    for e in week_events:
        k = e.get("rule_kind") or "other"
        violations_by_kind[k] = violations_by_kind.get(k, 0) + 1

    rules = auth.list_coach_rules(user_id)
    streak = coach_rules.compute_streak(rules, events, today=week_end)

    # Score now vs the latest score recorded BEFORE this week started.
    score_now = audits[0].get("discipline_score")
    before = [a for a in audits if (a.get("created_at") or "")[:10] < ws]
    score_before = before[0].get("discipline_score") if before else None
    delta = (round(float(score_now) - float(score_before), 0)
             if score_now is not None and score_before is not None else None)

    open_findings = auth.list_coach_findings(user_id, unresolved_only=True)
    return {
        "week_start": ws,
        "week_end": we,
        "n_trades_closed": len(closed),
        "realized_pnl": round(sum(t.pnl for t in closed), 2),   # in-app only
        "violations": len(week_events),
        "violations_by_kind": violations_by_kind,
        "streak_weeks": streak.get("weeks", 0),
        "score": score_now,
        "score_delta": delta,
        "active_rules": sum(1 for r in rules if r.get("status") == "active"),
        "top_open_finding": (open_findings[0].get("name") if open_findings else None),
    }


def _digest_email_html(payload: Dict, base_url: str) -> str:
    """Counts/streak/score only — NO dollar figures in email bodies."""
    delta = payload.get("score_delta")
    delta_line = ("" if delta is None else
                  f" ({'+' if delta >= 0 else ''}{int(delta)} vs before the week)")
    return (
        f"<h2 style='font-family:Georgia,serif'>Your week in discipline "
        f"({payload['week_start']} – {payload['week_end']})</h2>"
        f"<p>Trades closed: <b>{payload['n_trades_closed']}</b><br>"
        f"Rule breaks: <b>{payload['violations']}</b><br>"
        f"Streak: <b>{payload['streak_weeks']} week(s) without a rule violation</b><br>"
        f"Discipline score: <b>{payload.get('score')}</b>{delta_line}</p>"
        f"<p><a href='{base_url}/coach#rules'>Open your full digest</a> — "
        f"figures and details stay in the app.</p>"
    )


def send_weekly_digests(*, today: Optional[_dt.date] = None) -> Dict[str, int]:
    """Cron entry point. Walks every user with audit history; writes the in-app
    digest row (idempotent) and emails opted-in users when email is configured."""
    ws = week_start_for(today)
    base_url = os.getenv("AGENTICWHALES_PUBLIC_BASE_URL", "").rstrip("/")
    if auth._db_writable():
        rows = auth._select_columns("coach_audits", filters={},
                                    select="user_id", limit=10000)
    else:
        rows = [r for (t, _), r in auth._memstore.items() if t == "coach_audits"]
    user_ids = {r.get("user_id") for r in rows
                if r.get("user_id") and r.get("user_id") != auth.ANONYMOUS_USER_ID}

    written = emailed = 0
    for uid in sorted(user_ids):
        try:
            if any(d.get("week_start") == ws.isoformat()
                   for d in auth.list_coach_digests(uid, limit=3)):
                continue  # idempotent: this week already digested
            payload = build_digest(uid, ws)
            if payload is None:
                continue
            ok = False
            prefs = auth.get_coach_prefs(uid)
            if (prefs.get("email_digest") and prefs.get("digest_email")
                    and email_service.is_configured()):
                unsub = (f"{base_url}/api/coach/digest/unsubscribe"
                         f"?token={prefs.get('unsubscribe_token')}")
                ok = email_service.send_email(
                    prefs["digest_email"],
                    f"Your week in discipline — {payload['week_start']}",
                    _digest_email_html(payload, base_url or ""),
                    unsubscribe_url=unsub)
                if ok:
                    emailed += 1
            auth.insert_coach_digest({
                "user_id": uid, "week_start": ws.isoformat(),
                "payload": payload, "emailed": ok,
                "created_at": auth._ts_iso(time.time()),
            })
            written += 1
        except Exception as exc:  # noqa: BLE001 — one user must not stop the run
            log.warning("digest failed for %s: %s", uid, exc)
    log.info("weekly digest: %d written, %d emailed (week %s)",
             written, emailed, ws.isoformat())
    return {"written": written, "emailed": emailed}
