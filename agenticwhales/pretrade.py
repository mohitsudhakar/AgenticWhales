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

import re
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .coach import CoachReport, RoundTrip

# Advice posture: this is a DISCIPLINE CHECKLIST, not a recommendation engine.
# Every GENERATED user-facing string (check messages, leak evidence/fixes, rule
# copy, digest lines, benchmark labels) must be process guidance — never a
# directional directive. `contains_directive` is the tripwire; tests assert it
# over every generator so a directive can't silently leak into product copy.
#
# Scope note: this is calibrated for short generated strings, NOT whole HTML
# pages — mandatory disclaimer copy legitimately contains the words "buy" and
# "sell" ("never says buy or sell"). Page-level checks use an explicit
# forbidden-phrases list in tests instead.
_DIRECTIVE_RE = re.compile(
    r"\b("
    # hard directives
    r"buy|sell|go long|go short|enter now|exit now|take this trade|"
    r"recommend(?:ed|ation)?|"
    # advice constructions ("should exit", "consider selling", "worth adding")
    r"should (?:buy|sell|short|exit|enter|add|trim|hold)|"
    r"(?:consider|start|try|worth) (?:buying|selling|shorting|entering|exiting|adding|trimming)|"
    r"time to (?:buy|sell|exit|enter)|"
    # object-directed position verbs ("exit your position", "add to the trade")
    r"(?:add to|trim|exit|dump|unload) (?:the|your|this) (?:position|stake|trade|shares)"
    r")\b",
    re.IGNORECASE,
)


def contains_directive(text: str) -> bool:
    """True if a user-facing string reads as a buy/sell directive."""
    return bool(_DIRECTIVE_RE.search(text or ""))


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
    size_cap_x_median: float = 2.0,
    stop_required: bool = False,
    cooldown_hours: Optional[float] = None,
    today: Optional["_dt.date"] = None,
) -> PretradeVerdict:
    """The personalization kwargs (size_cap_x_median, stop_required,
    cooldown_hours) come from the user's ADOPTED rule book — they tighten the
    process checklist; nothing here is directional. `today` is injectable so
    cooldown checks are deterministic in tests."""
    import datetime as _dt
    long = trade.side == "long"
    entry = trade.entry_price
    notional = abs(trade.qty * entry)
    checks: List[Check] = []

    # --- Structure: stop present? (escalates to fail under the user's rule) ---
    stop = trade.stop_price
    if stop is None:
        stop = entry * (1 - default_stop_pct) if long else entry * (1 + default_stop_pct)
        checks.append(Check(
            "Stop-loss", "fail" if stop_required else "warn",
            ("Your adopted rule requires a stop before entry — none is set."
             if stop_required else
             "No stop set — you'd be trading without a defined exit."),
            f"Set a stop near ${stop:,.2f} (a {default_stop_pct:.0%} move).",
        ))
    risk_per_share = abs(entry - stop)

    # --- Behavior: the user's own cooldown rule (date-granular fills) ---
    if cooldown_hours and recent_trades:
        last = recent_trades[-1]
        try:
            last_exit = _dt.date.fromisoformat(str(last.exit_date)[:10])
        except ValueError:
            last_exit = None
        now_d = today or _dt.date.today()
        window_days = max(1, int(float(cooldown_hours) / 24 + 0.999))
        if last_exit and not last.is_win and 0 <= (now_d - last_exit).days < window_days:
            checks.append(Check(
                "Cooldown rule", "fail",
                f"Your last trade ({last.symbol}) closed at a loss "
                f"{(now_d - last_exit).days} day(s) ago — inside your "
                f"{float(cooldown_hours):.0f}h cooldown (fill timestamps are date-only).",
                "The rule you adopted: no new position until the cooldown passes.",
            ))

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
        elif med and notional > size_cap_x_median * med:
            checks.append(Check(
                "Oversizing vs your norm", "warn",
                f"This is {notional/med:.1f}x your median position size "
                f"(your cap: {size_cap_x_median:.1f}x).",
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
