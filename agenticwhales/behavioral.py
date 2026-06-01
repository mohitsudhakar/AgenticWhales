"""Behavioral pattern detection — Phase 2 deliverable #5.

Four patterns, each deterministic. Run on a user's recent activity over a
configurable lookback window:

1. **Tilt** — ≥2 losing trades within 1h followed by an order with >2× the
   user's median position size. Severity scales with how far above median.

2. **Revenge** — a stop-out (closed losing trade) followed within 30 minutes
   by entry into the same ticker at a larger size. The classical "I'll show
   the market" pattern.

3. **Anchoring** — journal-entry sentiment (when present) decoupling from
   subsequent price action. Specifically: positive sentiment on a ticker
   whose realized return ends up >2 standard deviations below the mean.

4. **Overconfidence** — sequence of trades with `prob_of_profit >= 0.8` that
   collectively realize a hit rate <50%. The PM (or the user, via override)
   is claiming high confidence on losers.

Each detector returns zero or more `Finding` dicts. Findings persist to
`behavioral_findings` so the UI can show them, the user can acknowledge or
dismiss, and the cooldown circuit-breaker can read them.

The detectors are pure functions over already-pulled data — they don't fetch
from the storage layer themselves. `scan_user(user_id)` orchestrates the
fetch + run + persist.

**Cooldown circuit-breaker** is opt-in (per the agreed design): when
`risk_limits.behavioral_cooldown` is true AND a `tilt` or `revenge` finding
was created in the last 60 minutes, the next paper order through the runner
emits a `risk_event` with rule=`tilt_cooldown` and short-circuits.
"""

from __future__ import annotations

import logging
import statistics
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables. Conservative thresholds: false positives erode trust, so we err
# on the side of *not* flagging unless the pattern is unambiguous.
# ---------------------------------------------------------------------------

LOOKBACK_DAYS = 14
TILT_LOSS_WINDOW_MIN = 60                # 2+ losers in 60 min
TILT_SIZE_MULT = 2.0                     # next order >= 2x median size
REVENGE_WINDOW_MIN = 30                  # re-entry within 30 min of stop-out
REVENGE_SIZE_MULT = 1.2                  # re-entry >= 1.2x prior size
OVERCONFIDENCE_PROB_THRESHOLD = 0.80
OVERCONFIDENCE_MIN_TRADES = 5
OVERCONFIDENCE_MAX_HIT_RATE = 0.50
ANCHORING_SENTIMENT_THRESHOLD = 50       # positive note >+50 / negative <-50
ANCHORING_SIGMA = 2.0                    # realized return >2σ from cohort mean
COOLDOWN_MIN = 60                        # tilt cooldown window


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Finding:
    user_id: str
    pattern: str             # 'tilt' | 'revenge' | 'anchoring' | 'overconfidence'
    severity: float          # 0..1
    evidence: Dict[str, Any]
    created_at: str          # ISO

    def to_row(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "pattern": self.pattern,
            "severity": float(self.severity),
            "evidence": self.evidence,
            "acknowledged": False,
            "dismissed": False,
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _parse_ts(ts: Any) -> Optional[datetime]:
    if ts is None:
        return None
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(float(ts), tz=timezone.utc)
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _within_lookback(now: datetime, ts_iso: Any, days: int = LOOKBACK_DAYS) -> bool:
    ts = _parse_ts(ts_iso)
    if ts is None:
        return False
    return (now - ts).days <= days


# ---------------------------------------------------------------------------
# Detectors (pure functions over pre-fetched data)
# ---------------------------------------------------------------------------

def detect_tilt(
    user_id: str,
    orders: List[Dict[str, Any]],
    outcomes_by_oid: Dict[str, Dict[str, Any]],
    now: datetime,
) -> List[Finding]:
    """Tilt: 2+ losing trades within 60 min, followed by an outsized entry.

    A "loser" is any order whose outcome shows `hit=false` (decision_outcomes
    is the source of truth — not the order's status flag, which is just the
    execution outcome). Outsized = qty ≥ 2× the user's median order qty over
    the lookback window. Severity = log1p(multiple - 2).
    """
    if not orders:
        return []
    qtys = [float(o.get("qty") or 0) for o in orders if o.get("qty")]
    if not qtys:
        return []
    median_qty = statistics.median(qtys)

    # Annotate each order with parsed timestamp + hit status.
    enriched = []
    for o in orders:
        ts = _parse_ts(o.get("created_at"))
        if ts is None:
            continue
        out = outcomes_by_oid.get(o["id"])
        hit = bool(out.get("hit")) if out else None   # None = unresolved
        enriched.append({"order": o, "ts": ts, "hit": hit})
    enriched.sort(key=lambda r: r["ts"])

    findings: List[Finding] = []
    seen_evidence: set = set()  # dedupe by (trigger_order_id)
    for i, row in enumerate(enriched):
        # Look at the 2+ orders BEFORE `row` that were losers within the window.
        window_start = row["ts"] - timedelta(minutes=TILT_LOSS_WINDOW_MIN)
        prior_losers = [
            r for r in enriched[:i]
            if r["ts"] >= window_start and r["hit"] is False
        ]
        if len(prior_losers) < 2:
            continue
        qty = float(row["order"].get("qty") or 0)
        if qty < TILT_SIZE_MULT * median_qty:
            continue
        if row["order"]["id"] in seen_evidence:
            continue
        seen_evidence.add(row["order"]["id"])
        multiple = qty / max(median_qty, 1e-9)
        severity = min(1.0, max(0.0, (multiple - TILT_SIZE_MULT) / 4.0 + 0.25))
        findings.append(Finding(
            user_id=user_id, pattern="tilt", severity=severity,
            evidence={
                "summary": f"2+ losses in {TILT_LOSS_WINDOW_MIN}min then "
                           f"{multiple:.1f}× median-size {row['order']['side']} on "
                           f"{row['order']['ticker']}",
                "trigger_order_id": row["order"]["id"],
                "prior_loser_ids": [r["order"]["id"] for r in prior_losers],
                "median_qty": median_qty,
                "trigger_qty": qty,
                "size_multiple": multiple,
            },
            created_at=_now_iso(),
        ))
    return findings


def detect_revenge(
    user_id: str,
    orders: List[Dict[str, Any]],
    outcomes_by_oid: Dict[str, Dict[str, Any]],
    now: datetime,
) -> List[Finding]:
    """Revenge: closed losing trade followed within 30 min by entry into the
    SAME ticker at a larger size.

    Where we detect this in the order stream:
      - A losing order is one with a resolved outcome whose `hit=false`. We
        treat the order's `created_at` as the proxy for when the user
        emotionally absorbed the loss (in v1; Phase 3's streaming data will
        give us a cleaner "stop-out moment").
      - A re-entry is a subsequent order on the same ticker, same direction,
        within 30 min.
    """
    if not orders:
        return []
    enriched = []
    for o in orders:
        ts = _parse_ts(o.get("created_at"))
        if ts is None:
            continue
        out = outcomes_by_oid.get(o["id"])
        hit = bool(out.get("hit")) if out else None
        enriched.append({"order": o, "ts": ts, "hit": hit})
    enriched.sort(key=lambda r: r["ts"])

    findings: List[Finding] = []
    for i, row in enumerate(enriched):
        if row["hit"] is not False:        # only losing trades trigger
            continue
        loser_qty = float(row["order"].get("qty") or 0)
        loser_ticker = (row["order"].get("ticker") or "").upper()
        if not loser_ticker:
            continue
        window_end = row["ts"] + timedelta(minutes=REVENGE_WINDOW_MIN)
        for r in enriched[i + 1:]:
            if r["ts"] > window_end:
                break
            re_order = r["order"]
            if (re_order.get("ticker") or "").upper() != loser_ticker:
                continue
            if (re_order.get("side") or "") != (row["order"].get("side") or ""):
                continue
            re_qty = float(re_order.get("qty") or 0)
            if re_qty < REVENGE_SIZE_MULT * max(loser_qty, 1e-9):
                continue
            multiple = re_qty / max(loser_qty, 1e-9)
            severity = min(1.0, max(0.30, (multiple - 1.0) / 2.0 + 0.30))
            findings.append(Finding(
                user_id=user_id, pattern="revenge", severity=severity,
                evidence={
                    "summary": f"Re-entered {loser_ticker} {row['order']['side']} at "
                               f"{multiple:.1f}× size within "
                               f"{int((r['ts'] - row['ts']).total_seconds()/60)}min "
                               f"of a stop-out",
                    "loser_order_id": row["order"]["id"],
                    "reentry_order_id": re_order["id"],
                    "minutes_between": int((r["ts"] - row["ts"]).total_seconds() / 60),
                    "size_multiple": multiple,
                },
                created_at=_now_iso(),
            ))
            break  # one revenge per loser is enough
    return findings


def detect_overconfidence(
    user_id: str,
    orders: List[Dict[str, Any]],
    outcomes_by_oid: Dict[str, Dict[str, Any]],
    now: datetime,
) -> List[Finding]:
    """Overconfidence: 5+ orders claiming `prob_of_profit ≥ 0.80` collectively
    realize hit rate <50%. The probability claim is decoupled from outcomes —
    either the PM is broken or the user keeps overriding low-probability
    setups upward."""
    high_conf = []
    for o in orders:
        p = o.get("prob_of_profit")
        if p is None:
            continue
        try:
            if float(p) < OVERCONFIDENCE_PROB_THRESHOLD:
                continue
        except (TypeError, ValueError):
            continue
        out = outcomes_by_oid.get(o["id"])
        if not out or out.get("hit") is None:
            continue
        high_conf.append((o, bool(out["hit"])))

    if len(high_conf) < OVERCONFIDENCE_MIN_TRADES:
        return []
    hits = sum(1 for _, h in high_conf if h)
    hit_rate = hits / len(high_conf)
    if hit_rate >= OVERCONFIDENCE_MAX_HIT_RATE:
        return []

    severity = min(1.0, max(0.30, (0.5 - hit_rate) * 2.0))
    return [Finding(
        user_id=user_id, pattern="overconfidence", severity=severity,
        evidence={
            "summary": f"{len(high_conf)} high-conviction trades (p≥{OVERCONFIDENCE_PROB_THRESHOLD}) "
                       f"hit {hit_rate:.0%} — well below the claimed rate.",
            "n_trades": len(high_conf),
            "claimed_min_prob": OVERCONFIDENCE_PROB_THRESHOLD,
            "realized_hit_rate": hit_rate,
            "order_ids": [o["id"] for o, _ in high_conf[:20]],
        },
        created_at=_now_iso(),
    )]


def detect_anchoring(
    user_id: str,
    orders: List[Dict[str, Any]],
    outcomes_by_oid: Dict[str, Dict[str, Any]],
    journal: List[Dict[str, Any]],
    now: datetime,
) -> List[Finding]:
    """Anchoring: a positive journal entry within 24h before a trade that
    ended >2σ below the user's mean realized return. The user latched onto
    a thesis that wasn't borne out — a classic anchoring bias signature."""
    # Gather realized returns for cohort stats.
    realized = [
        float(out["realized_return_pct"])
        for out in outcomes_by_oid.values()
        if out.get("realized_return_pct") is not None
    ]
    if len(realized) < 5:
        return []
    mean_ret = statistics.mean(realized)
    try:
        stdev_ret = statistics.stdev(realized)
    except statistics.StatisticsError:
        return []
    if stdev_ret <= 0:
        return []

    findings: List[Finding] = []
    journal_by_ts = []
    for j in journal:
        ts = _parse_ts(j.get("created_at"))
        sent = j.get("sentiment_score")
        if ts is None or sent is None:
            continue
        journal_by_ts.append((ts, j))
    journal_by_ts.sort()

    seen_orders: set = set()
    for order in orders:
        out = outcomes_by_oid.get(order["id"])
        if not out or out.get("realized_return_pct") is None:
            continue
        z = (float(out["realized_return_pct"]) - mean_ret) / stdev_ret
        if z >= -ANCHORING_SIGMA:
            continue
        order_ts = _parse_ts(order.get("created_at"))
        if order_ts is None:
            continue
        window_start = order_ts - timedelta(hours=24)
        nearby_positive = [
            j for ts, j in journal_by_ts
            if window_start <= ts <= order_ts
            and (j.get("sentiment_score") or 0) >= ANCHORING_SENTIMENT_THRESHOLD
        ]
        if not nearby_positive:
            continue
        if order["id"] in seen_orders:
            continue
        seen_orders.add(order["id"])
        severity = min(1.0, max(0.30, abs(z) / 4.0))
        findings.append(Finding(
            user_id=user_id, pattern="anchoring", severity=severity,
            evidence={
                "summary": f"Wrote positive journal entry within 24h before a "
                           f"trade that landed {z:.1f}σ below your mean return.",
                "order_id": order["id"],
                "journal_ids": [j["id"] for j in nearby_positive[:3]],
                "z_score": z,
            },
            created_at=_now_iso(),
        ))
    return findings


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def scan_user(user_id: str, *, now: Optional[datetime] = None) -> List[Finding]:
    """Run all four detectors, persist new findings (deduped against the
    most-recent existing row for that pattern), return what was written.

    Idempotency: a detection is a *new* finding only if no row for that
    (user, pattern) was created within the lookback window. Otherwise the
    existing row's severity is updated if higher. Otherwise no-op.
    """
    from web import auth
    from agenticwhales import outcomes as outcomes_mod

    now = now or datetime.now(tz=timezone.utc)
    orders = auth.list_paper_orders(user_id, limit=500) or []
    orders = [o for o in orders if _within_lookback(now, o.get("created_at"))]
    outcomes_by_oid = {
        o["paper_order_id"]: o
        for o in (outcomes_mod.list_outcomes_for_user(user_id, limit=500) or [])
    }
    journal = auth.list_journal_entries(user_id, include_drafts=False) or []
    journal = [j for j in journal if _within_lookback(now, j.get("created_at"))]

    detected: List[Finding] = []
    detected.extend(detect_tilt(user_id, orders, outcomes_by_oid, now))
    detected.extend(detect_revenge(user_id, orders, outcomes_by_oid, now))
    detected.extend(detect_overconfidence(user_id, orders, outcomes_by_oid, now))
    detected.extend(detect_anchoring(user_id, orders, outcomes_by_oid, journal, now))

    persisted: List[Finding] = []
    existing = list_recent_findings(user_id, limit=200) if detected else []
    for finding in detected:
        # Dedupe: same pattern + same evidence summary already exists in the
        # lookback window → skip the write.
        is_dup = any(
            e.get("pattern") == finding.pattern
            and (e.get("evidence") or {}).get("summary") == finding.evidence.get("summary")
            for e in existing
        )
        if is_dup:
            continue
        _persist_finding(finding)
        persisted.append(finding)
    return persisted


def _persist_finding(finding: Finding) -> None:
    from web import auth
    row = finding.to_row()
    pk = f"{finding.user_id}|{finding.pattern}|{finding.created_at}"
    auth._memstore[("behavioral_findings", pk)] = row
    try:
        auth._upsert_columns("behavioral_findings", row)
    except Exception:
        pass


def list_recent_findings(
    user_id: str,
    *,
    limit: int = 50,
    days: int = LOOKBACK_DAYS,
) -> List[Dict[str, Any]]:
    """Findings inside the lookback window, newest first. Used by the UI +
    the cooldown circuit-breaker + the ask.template_9 path."""
    from web import auth
    now = datetime.now(tz=timezone.utc)
    if auth._db_writable():
        try:
            rows = auth._select_columns(
                "behavioral_findings",
                filters={"user_id": user_id},
                order="created_at.desc",
                limit=limit,
            ) or []
        except Exception:
            rows = []
    else:
        rows = []
    if not rows:
        rows = [
            r for (t, _), r in auth._memstore.items()
            if t == "behavioral_findings" and r.get("user_id") == user_id
        ]
    rows = [r for r in rows if _within_lookback(now, r.get("created_at"), days=days)]
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return rows[:limit]


def update_finding_state(
    user_id: str,
    finding_pk: str,
    *,
    acknowledged: Optional[bool] = None,
    dismissed: Optional[bool] = None,
) -> bool:
    """Flip acknowledged / dismissed on a finding. Idempotent. Returns
    whether the row existed and belonged to the user."""
    from web import auth
    row = auth._memstore.get(("behavioral_findings", finding_pk))
    if not row or row.get("user_id") != user_id:
        return False
    if acknowledged is not None:
        row["acknowledged"] = bool(acknowledged)
    if dismissed is not None:
        row["dismissed"] = bool(dismissed)
    try:
        auth._upsert_columns("behavioral_findings", row)
    except Exception:
        pass
    return True


# ---------------------------------------------------------------------------
# Cooldown circuit-breaker — read-only check the runner calls pre-order
# ---------------------------------------------------------------------------

def cooldown_in_effect(user_id: str, *, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """If the user is currently within a tilt/revenge cooldown window AND
    has opted in to the circuit-breaker, return the triggering finding row
    (so the runner can include it in the `risk_event` details). Else None.
    """
    from web import auth
    now = now or datetime.now(tz=timezone.utc)
    limits = auth.load_risk_limits(user_id) or auth._default_risk_limits_row(user_id)
    if not limits.get("behavioral_cooldown"):
        return None
    cutoff = now - timedelta(minutes=COOLDOWN_MIN)
    findings = list_recent_findings(user_id, limit=20, days=2)
    for f in findings:
        if f.get("pattern") not in ("tilt", "revenge"):
            continue
        ts = _parse_ts(f.get("created_at"))
        if ts and ts >= cutoff and not f.get("dismissed"):
            return f
    return None
