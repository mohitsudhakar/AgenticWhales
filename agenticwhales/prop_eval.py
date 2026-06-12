"""Prop-firm evaluation tracker — deterministic scorekeeping, never advice.

An evaluation (FTMO-style challenge, Topstep-style combine, or custom) is a
set of dollar thresholds over a trading window: a profit target, a max loss
per day, and a max total drawdown. This module re-scores the user's ACTUAL
realized P&L (closed round-trips, bucketed by exit date) against those
thresholds and reports what happened: progress, breaches, proximity.

Design laws: every figure is deterministic arithmetic on the user's own
trades; all copy is past-tense fact ("breached", "reached") — the product
never tells anyone to trade, stop trading, or size differently.

Granularity caveat (stated in the UI): fills are date-level, so "daily loss"
is realized P&L by EXIT day. Intraday equity swings and open positions are
invisible to a statement-level audit — a prop firm's own dashboard remains
the source of truth; this is the discipline mirror pointed at the same wall.
"""

from __future__ import annotations

import datetime as _dt
from typing import Dict, List, Optional, Sequence

from agenticwhales.coach import RoundTrip, _d

# Threshold presets, expressed as fractions of account size. "Style" naming
# is deliberate — these mirror common public structures, not any firm's
# current terms (which change; users can always pick custom).
PRESETS: Dict[str, Dict[str, float]] = {
    "ftmo_style":    {"profit_target_pct": 0.10, "daily_loss_pct": 0.05, "max_drawdown_pct": 0.10},
    "topstep_style": {"profit_target_pct": 0.06, "daily_loss_pct": 0.02, "max_drawdown_pct": 0.04},
    "custom":        {"profit_target_pct": 0.08, "daily_loss_pct": 0.04, "max_drawdown_pct": 0.08},
}


def resolve_config(cfg: Dict) -> Dict:
    """Fill a (possibly partial) eval config from its preset; dollars derive
    from account_size so every threshold is a concrete number."""
    preset = PRESETS.get(str(cfg.get("preset") or "custom"), PRESETS["custom"])
    size = float(cfg.get("account_size") or 0)
    out = {
        "preset": cfg.get("preset") or "custom",
        "account_size": size,
        "start_date": str(cfg.get("start_date") or "")[:10],
        "end_date": str(cfg.get("end_date") or "")[:10] or None,
        "profit_target": float(cfg.get("profit_target") or size * preset["profit_target_pct"]),
        "daily_loss_limit": float(cfg.get("daily_loss_limit") or size * preset["daily_loss_pct"]),
        "max_drawdown": float(cfg.get("max_drawdown") or size * preset["max_drawdown_pct"]),
    }
    return out


def evaluate(cfg: Dict, trips: Sequence[RoundTrip],
             today: Optional[_dt.date] = None) -> Dict:
    """Score the evaluation window against realized P&L by exit date."""
    cfg = resolve_config(cfg)
    start = _d(cfg["start_date"])
    if not start:
        return {"error": "start_date required (yyyy-mm-dd)"}
    end = _d(cfg["end_date"]) if cfg["end_date"] else None
    today = today or _dt.date.today()
    horizon = min(end, today) if end else today

    daily: Dict[str, float] = {}
    for t in trips:
        d = _d(t.exit_date)
        if d and start <= d <= horizon:
            daily[d.isoformat()] = daily.get(d.isoformat(), 0.0) + t.pnl

    days = sorted(daily)
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    breaches: List[Dict] = []
    for day in days:
        pnl = daily[day]
        cum += pnl
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
        if -pnl > cfg["daily_loss_limit"] > 0:
            breaches.append({
                "date": day, "kind": "daily_loss",
                "pnl": round(pnl, 2), "limit": cfg["daily_loss_limit"],
                # Past-tense fact; date-granularity stated, no instruction.
                "note": (f"realized {pnl:,.0f} on {day} — beyond the "
                         f"{cfg['daily_loss_limit']:,.0f} daily loss limit "
                         "(statement granularity is by exit day)"),
            })
    dd_breached = cfg["max_drawdown"] > 0 and max_dd > cfg["max_drawdown"]
    target_hit = cfg["profit_target"] > 0 and cum >= cfg["profit_target"]

    days_elapsed = (horizon - start).days + 1 if horizon >= start else 0
    days_remaining = (end - today).days if end and end > today else None
    status = ("target_reached" if target_hit
              else "thresholds_breached" if (breaches or dd_breached)
              else "in_progress")
    return {
        "config": cfg,
        "status": status,
        "net_pnl": round(cum, 2),
        "profit_target": cfg["profit_target"],
        "target_progress": round(min(1.0, cum / cfg["profit_target"]), 4)
                           if cfg["profit_target"] > 0 and cum > 0 else 0.0,
        "max_drawdown_seen": round(max_dd, 2),
        "max_drawdown_limit": cfg["max_drawdown"],
        "drawdown_breached": dd_breached,
        "daily_loss_limit": cfg["daily_loss_limit"],
        "daily_breaches": breaches,
        "worst_day": ({"date": min(daily, key=daily.get),
                       "pnl": round(min(daily.values()), 2)} if daily else None),
        "best_day": ({"date": max(daily, key=daily.get),
                      "pnl": round(max(daily.values()), 2)} if daily else None),
        "n_trading_days": len(days),
        "days_elapsed": days_elapsed,
        "days_remaining": days_remaining,
    }
