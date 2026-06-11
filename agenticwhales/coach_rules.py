"""Rule book — user-adopted process constraints derived from coach findings.

This is enforcement vertical of the coach (critique P4): every flagged leak
prescribes a rule; here those rules become STRUCTURED, trackable objects the
product can check — instead of advice text that evaporates.

Compliance posture (same laws as coach.py / pretrade.py):
- Rules are process constraints the USER adopts for themselves — never
  directives. Copy is neutral and past-tense; `pretrade.contains_directive`
  sweeps every generated string in tests.
- Violation "dollars" are the realized P&L of the trades that broke the rule —
  labeled exactly that, never "cost you" (a winning trade can break a rule).
- Fill data is date-granular: a 24-hour cooldown can only observe same-day or
  next-day re-entry. Evidence strings say so.

Checkability split (be honest about what fill history can show):
- checkable from fills: cooldown_after_loss, size_cap_x_median,
  max_trades_per_week.
- pre-trade only (fills carry no stop/target/equity): stop_required,
  min_payoff, max_risk_pct — these personalize pretrade.check_trade and are
  labeled "checked at pre-trade time" in the UI.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .coach import RoundTrip, leak_key  # noqa: F401  (leak_key re-exported for callers)

# leak_key -> list of rule templates it seeds. `params` are the defaults the
# user can tune within `bounds` (deterministic clamps, no free text).
RULE_TEMPLATES: Dict[str, List[Dict]] = {
    "revenge_trading": [{
        "rule_kind": "cooldown_after_loss",
        "label": "Cooldown after a loss",
        "params": {"cooldown_hours": 24},
        "bounds": {"cooldown_hours": (4, 168)},
        "checkable_from_fills": True,
        "description": "No new position within the cooldown window after a losing exit.",
    }],
    "oversizing_tail_risk": [{
        "rule_kind": "size_cap_x_median",
        "label": "Position size cap",
        "params": {"cap_x": 2.0},
        "bounds": {"cap_x": (1.0, 5.0)},
        "checkable_from_fills": True,
        "description": "No position larger than cap_x times the median size of prior trades.",
    }, {
        "rule_kind": "max_risk_pct",
        "label": "Risk per trade cap",
        "params": {"max_risk_pct": 0.02},
        "bounds": {"max_risk_pct": (0.0025, 0.05)},
        "checkable_from_fills": False,
        "description": "Each trade risks at most this fraction of equity (pre-trade check).",
    }],
    "disposition_effect": [{
        "rule_kind": "stop_required",
        "label": "Stop defined before entry",
        "params": {},
        "bounds": {},
        "checkable_from_fills": False,
        "description": "Every pre-trade check requires a stop price (pre-trade check).",
    }],
    "no_stop_loss": [{
        "rule_kind": "stop_required",
        "label": "Stop defined before entry",
        "params": {},
        "bounds": {},
        "checkable_from_fills": False,
        "description": "Every pre-trade check requires a stop price (pre-trade check).",
    }],
    "inverted_payoff": [{
        "rule_kind": "min_payoff",
        "label": "Minimum reward:risk",
        "params": {"min_payoff": 1.5},
        "bounds": {"min_payoff": (1.0, 4.0)},
        "checkable_from_fills": False,
        "description": "Targets must be at least min_payoff times the risk (pre-trade check).",
    }],
    "overtrading_cost_drag": [{
        "rule_kind": "max_trades_per_week",
        "label": "Weekly trade budget",
        "params": {"max_trades": 10},
        "bounds": {"max_trades": (1, 50)},
        "checkable_from_fills": True,
        "description": "At most this many new positions opened per ISO week.",
    }],
}

# Statuses: suggested (seeded, not yet adopted) -> active -> paused.
RULE_STATUSES = ("suggested", "active", "paused")


def clamp_params(rule_kind: str, params: Dict) -> Dict:
    """Clamp tuned params into the template bounds; drop unknown keys."""
    template = None
    for templates in RULE_TEMPLATES.values():
        for t in templates:
            if t["rule_kind"] == rule_kind:
                template = t
                break
    if template is None:
        return {}
    out = dict(template["params"])
    for k, (lo, hi) in template["bounds"].items():
        if k in (params or {}):
            try:
                out[k] = min(hi, max(lo, float(params[k])))
            except (TypeError, ValueError):
                pass
    if "max_trades" in out:
        out["max_trades"] = int(out["max_trades"])
    return out


def suggest_rules(open_findings: Sequence[Dict],
                  existing_rules: Sequence[Dict]) -> List[Dict]:
    """Deterministically seed rule rows from open findings — one suggestion per
    (leak_key, rule_kind) not already present in any status."""
    have = {(r.get("leak_key"), r.get("rule_kind")) for r in existing_rules}
    out: List[Dict] = []
    for f in open_findings:
        key = f.get("leak_key") or ""
        for t in RULE_TEMPLATES.get(key, []):
            ident = (key, t["rule_kind"])
            if ident in have:
                continue
            have.add(ident)
            out.append({
                "id": hashlib.sha1(
                    f"{f.get('user_id','')}|{key}|{t['rule_kind']}".encode()
                ).hexdigest()[:32],
                "user_id": f.get("user_id", ""),
                "leak_key": key,
                "rule_kind": t["rule_kind"],
                "label": t["label"],
                "description": t["description"],
                "params": dict(t["params"]),
                "checkable_from_fills": t["checkable_from_fills"],
                "status": "suggested",
                "source_finding_id": f.get("id", ""),
                "adopted_at": None,
            })
    return out


# --------------------------------------------------------------------------- #
# Violation detection (fills-checkable kinds only)
# --------------------------------------------------------------------------- #

@dataclass
class Violation:
    id: str                  # deterministic: re-audits of merged history are no-ops
    rule_id: str
    rule_kind: str
    leak_key: str
    trade_key: str
    occurred_on: str         # entry date of the violating trade
    dollars: float           # realized P&L of the trade(s) that broke the rule
    evidence: str
    symbol: str = ""

    def to_row(self, user_id: str) -> Dict:
        d = self.__dict__.copy()
        d["user_id"] = user_id
        return d


def _trip_key(t: RoundTrip) -> str:
    return f"{t.symbol}|{t.entry_date[:10]}|{t.exit_date[:10]}|{round(t.qty, 4)}|{round(t.entry_px, 4)}"


def _violation_id(user_id: str, rule_id: str, trade_key: str) -> str:
    return hashlib.sha1(f"{user_id}|{rule_id}|{trade_key}".encode()).hexdigest()


def _d(s: str) -> Optional[_dt.date]:
    try:
        return _dt.date.fromisoformat(str(s)[:10])
    except Exception:  # noqa: BLE001
        return None


def detect_violations(rules: Sequence[Dict], trips: Sequence[RoundTrip],
                      *, user_id: str = "") -> List[Violation]:
    """Check ACTIVE, fills-checkable rules against round-trips entered on or
    after each rule's adoption date. Pure + deterministic."""
    out: List[Violation] = []
    trips = sorted(trips, key=lambda t: (t.entry_date, t.exit_date))
    for rule in rules:
        if rule.get("status") != "active" or not rule.get("checkable_from_fills"):
            continue
        adopted = str(rule.get("adopted_at") or "")[:10]
        if not adopted:
            continue
        kind = rule.get("rule_kind")
        params = rule.get("params") or {}
        scoped = [t for t in trips if t.entry_date[:10] >= adopted]

        if kind == "cooldown_after_loss":
            hours = float(params.get("cooldown_hours", 24))
            window_days = max(1, int(hours / 24 + 0.999))
            # prior trip by exit date (full history provides the prior context)
            by_exit = sorted(trips, key=lambda t: t.exit_date)
            for t in scoped:
                ed = _d(t.entry_date)
                if ed is None:
                    continue
                prior = [p for p in by_exit
                         if p.exit_date[:10] <= t.entry_date[:10]
                         and _trip_key(p) != _trip_key(t)]
                if not prior:
                    continue
                last = prior[-1]
                xd = _d(last.exit_date)
                if xd is None or last.is_win:
                    continue
                gap_days = (ed - xd).days
                if 0 <= gap_days < window_days:
                    out.append(Violation(
                        id=_violation_id(user_id, rule["id"], _trip_key(t)),
                        rule_id=rule["id"], rule_kind=kind,
                        leak_key=rule.get("leak_key", ""),
                        trade_key=_trip_key(t), occurred_on=t.entry_date[:10],
                        dollars=round(t.pnl, 2), symbol=t.symbol,
                        evidence=(f"{t.symbol} entered {gap_days} day(s) after the "
                                  f"{last.symbol} loss closed — inside the "
                                  f"{hours:.0f}h cooldown (fill timestamps are "
                                  f"date-only). That trade realized ${t.pnl:,.0f}."),
                    ))

        elif kind == "size_cap_x_median":
            cap_x = float(params.get("cap_x", 2.0))
            for i, t in enumerate(trips):
                if t.entry_date[:10] < adopted:
                    continue
                prior = trips[:i]
                if len(prior) < 5:
                    continue
                med = statistics.median(abs(p.qty * p.entry_px) for p in prior)
                notional = abs(t.qty * t.entry_px)
                if med > 0 and notional > cap_x * med:
                    out.append(Violation(
                        id=_violation_id(user_id, rule["id"], _trip_key(t)),
                        rule_id=rule["id"], rule_kind=kind,
                        leak_key=rule.get("leak_key", ""),
                        trade_key=_trip_key(t), occurred_on=t.entry_date[:10],
                        dollars=round(t.pnl, 2), symbol=t.symbol,
                        evidence=(f"{t.symbol} was ${notional:,.0f} — "
                                  f"{notional / med:.1f}x the median size of the "
                                  f"trades before it (cap: {cap_x:.1f}x). That "
                                  f"trade realized ${t.pnl:,.0f}."),
                    ))

        elif kind == "max_trades_per_week":
            max_trades = int(params.get("max_trades", 10))
            weeks: Dict[str, List[RoundTrip]] = {}
            for t in scoped:
                ed = _d(t.entry_date)
                if ed is None:
                    continue
                iso = ed.isocalendar()
                weeks.setdefault(f"{iso[0]}-W{iso[1]:02d}", []).append(t)
            for week, wtrips in weeks.items():
                wtrips.sort(key=lambda t: t.entry_date)
                excess = wtrips[max_trades:]
                if not excess:
                    continue
                pnl = sum(t.pnl for t in excess)
                out.append(Violation(
                    id=_violation_id(user_id, rule["id"], f"week|{week}"),
                    rule_id=rule["id"], rule_kind=kind,
                    leak_key=rule.get("leak_key", ""),
                    trade_key=f"week|{week}", occurred_on=excess[0].entry_date[:10],
                    dollars=round(pnl, 2),
                    evidence=(f"{len(wtrips)} positions opened in week {week} — "
                              f"{len(excess)} beyond the {max_trades}/week budget. "
                              f"Those trades realized ${pnl:,.0f}."),
                ))
    out.sort(key=lambda v: v.occurred_on)
    return out


def compute_streak(rules: Sequence[Dict], events: Sequence[Dict],
                   today: Optional[_dt.date] = None) -> Dict:
    """Consecutive ISO weeks (ending with the current week) with zero rule
    violations, counted only from the earliest adoption — honest framing is
    "weeks without a rule violation" (a trade-free week counts; you can't
    violate before you adopted)."""
    today = today or _dt.date.today()
    adopted_dates = [d for d in (_d(r.get("adopted_at") or "") for r in rules
                                 if r.get("status") in ("active", "paused")) if d]
    if not adopted_dates:
        return {"weeks": 0, "since": None}
    start = min(adopted_dates)
    violation_weeks = set()
    for e in events:
        d = _d(e.get("occurred_on") or "")
        if d:
            iso = d.isocalendar()
            violation_weeks.add((iso[0], iso[1]))
    weeks = 0
    cursor = today
    while cursor >= start:
        iso = cursor.isocalendar()
        if (iso[0], iso[1]) in violation_weeks:
            break
        weeks += 1
        cursor = cursor - _dt.timedelta(days=7)
    return {"weeks": weeks, "since": start.isoformat()}
