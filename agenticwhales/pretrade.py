"""Pre-trade check — the decision-support half of the product, unified with the coach.

Before a trade is placed, run it past three gates, all deterministic and
defensible (no predictive edge required — which the edge probe proved we lack):

  1. Risk sizing — does this risk more than your per-trade cap?
  2. Structure — is there a stop, and is the reward:risk worth it?
  3. Behavior — does this repeat a leak from YOUR history (revenge size-up,
     oversizing vs your norm, the disposition pattern)?

Output is a GO / CAUTION / BLOCK verdict plus a "disciplined version" of the
trade (resized, with a stop and a target). It closes the loop with `coach.py`:
the coach tells you what your habits cost; the pre-trade check stops you
repeating them in the moment.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .coach import CoachReport, RoundTrip

DEFAULT_STOP_PCT = 0.08
DEFAULT_MAX_RISK_PCT = 0.02
DEFAULT_MAX_CONCENTRATION = 0.25
DEFAULT_MIN_PAYOFF = 1.5


@dataclass
class ProposedTrade:
    symbol: str
    side: str = "long"            # long | short
    qty: float = 0.0
    entry_price: float = 0.0
    stop_price: Optional[float] = None
    target_price: Optional[float] = None


@dataclass
class Check:
    name: str
    status: str                   # pass | warn | fail
    message: str
    suggestion: str = ""


@dataclass
class PretradeVerdict:
    verdict: str                  # GO | CAUTION | BLOCK
    checks: List[Check] = field(default_factory=list)
    risk_dollars: float = 0.0
    risk_pct_equity: float = 0.0
    notional: float = 0.0
    payoff_ratio: Optional[float] = None
    disciplined: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        d = self.__dict__.copy()
        d["checks"] = [c.__dict__ for c in self.checks]
        return d


def check_trade(
    trade: ProposedTrade,
    *,
    equity: float,
    recent_trades: Optional[Sequence[RoundTrip]] = None,
    leak_profile: Optional[CoachReport] = None,
    max_risk_pct: float = DEFAULT_MAX_RISK_PCT,
    max_concentration_pct: float = DEFAULT_MAX_CONCENTRATION,
    min_payoff: float = DEFAULT_MIN_PAYOFF,
    default_stop_pct: float = DEFAULT_STOP_PCT,
) -> PretradeVerdict:
    long = trade.side == "long"
    entry = trade.entry_price
    notional = abs(trade.qty * entry)
    checks: List[Check] = []

    # --- Structure: stop present? ---
    stop = trade.stop_price
    if stop is None:
        stop = entry * (1 - default_stop_pct) if long else entry * (1 + default_stop_pct)
        checks.append(Check(
            "Stop-loss", "warn",
            "No stop set — you'd be trading without a defined exit.",
            f"Set a stop near ${stop:,.2f} (a {default_stop_pct:.0%} move).",
        ))
    risk_per_share = abs(entry - stop)

    # --- Risk: per-trade sizing ---
    risk_dollars = trade.qty * risk_per_share
    risk_pct = risk_dollars / equity if equity > 0 else 0.0
    if risk_pct > max_risk_pct and risk_per_share > 0:
        safe_qty = (max_risk_pct * equity) / risk_per_share
        checks.append(Check(
            "Risk per trade", "fail",
            f"Risks ${risk_dollars:,.0f} = {risk_pct:.1%} of equity (your cap is {max_risk_pct:.0%}).",
            f"Cut size to ~{safe_qty:,.0f} shares to risk {max_risk_pct:.0%} (${max_risk_pct*equity:,.0f}).",
        ))
    else:
        checks.append(Check(
            "Risk per trade", "pass",
            f"Risks ${risk_dollars:,.0f} ({risk_pct:.1%} of equity) — within your {max_risk_pct:.0%} cap.",
        ))

    # --- Risk: concentration ---
    conc = notional / equity if equity > 0 else 0.0
    if conc > max_concentration_pct:
        checks.append(Check(
            "Concentration", "warn",
            f"This position is {conc:.0%} of your equity (> {max_concentration_pct:.0%}).",
            "One name shouldn't dominate the book — scale it down or split it.",
        ))

    # --- Structure: payoff ratio ---
    payoff = None
    tgt_for = entry + (min_payoff * risk_per_share if long else -min_payoff * risk_per_share)
    if trade.target_price and risk_per_share > 0:
        payoff = abs(trade.target_price - entry) / risk_per_share
        if payoff < min_payoff:
            checks.append(Check(
                "Payoff ratio", "warn",
                f"Reward:risk is only {payoff:.1f}:1 (aim for >= {min_payoff:.1f}).",
                f"Move the target to ${tgt_for:,.2f} for {min_payoff:.1f}:1.",
            ))
        else:
            checks.append(Check("Payoff ratio", "pass", f"Reward:risk {payoff:.1f}:1 — good."))
    else:
        checks.append(Check(
            "Payoff ratio", "warn",
            "No target set — define reward:risk before you enter.",
            f"A {min_payoff:.1f}:1 target sits near ${tgt_for:,.2f}.",
        ))

    # --- Behavior: against the trader's OWN history ---
    if recent_trades:
        sizes = [abs(t.qty * t.entry_px) for t in recent_trades]
        med = statistics.median(sizes) if sizes else 0.0
        last = recent_trades[-1]
        if not last.is_win and med and notional > 1.3 * med:
            checks.append(Check(
                "Revenge-trade pattern", "fail",
                f"Your last trade ({last.symbol}) lost ${abs(last.pnl):,.0f}, and this one is "
                f"{notional/med:.1f}x your typical size. That's the revenge pattern.",
                "Take the cooldown. Trade this at normal size or not today.",
            ))
        elif med and notional > 2 * med:
            checks.append(Check(
                "Oversizing vs your norm", "warn",
                f"This is {notional/med:.1f}x your median position size.",
                "Unusual size needs an unusually good reason — size it normally.",
            ))

    # --- Behavior: personalize from the leak profile ---
    if leak_profile and leak_profile.leaks:
        names = " ".join(l.name for l in leak_profile.leaks).lower()
        if "disposition" in names and trade.stop_price is None:
            checks.append(Check(
                "Your #1 leak", "warn",
                "Your history shows the disposition effect — you hold losers. Honor this "
                "stop mechanically, no 'just one more day'.",
            ))

    # --- Verdict ---
    if any(c.status == "fail" for c in checks):
        verdict = "BLOCK"
    elif any(c.status == "warn" for c in checks):
        verdict = "CAUTION"
    else:
        verdict = "GO"

    safe_qty = (min(trade.qty, (max_risk_pct * equity) / risk_per_share)
                if risk_per_share > 0 else trade.qty)
    return PretradeVerdict(
        verdict=verdict, checks=checks,
        risk_dollars=round(risk_dollars, 2), risk_pct_equity=round(risk_pct, 4),
        notional=round(notional, 2),
        payoff_ratio=round(payoff, 2) if payoff is not None else None,
        disciplined={
            "qty": round(safe_qty, 0),
            "stop": round(stop, 2),
            "target": round(tgt_for, 2),
            "risk_dollars": round(min(risk_dollars, max_risk_pct * equity), 2),
        },
    )
