"""Translate the Portfolio Manager's qualitative rating into a target position size.

The PortfolioDecision schema carries a 5-tier rating but no quantity. The
sizing policy maps that rating to a target *weight* (fraction of account
equity), and the Executor combines that weight with the live price + equity
to get a concrete share count.

Two sizing knobs that matter in practice:

- ``allow_short``: if False (default), Underweight/Sell collapse to "exit
  to flat" rather than opening a short.
- ``min_order_value``: orders whose notional is below this threshold are
  skipped, so a 0.4-share rebalance doesn't churn the account.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Optional

from tradingagents.agents.schemas import PortfolioRating


DEFAULT_TARGET_WEIGHTS: Dict[PortfolioRating, Optional[float]] = {
    PortfolioRating.BUY: 0.10,
    PortfolioRating.OVERWEIGHT: 0.05,
    PortfolioRating.HOLD: None,            # None = "do not change target"
    PortfolioRating.UNDERWEIGHT: -0.025,
    PortfolioRating.SELL: -0.05,
}


@dataclass
class SizingPolicy:
    """How a PortfolioRating becomes a target share count for one ticker."""

    target_weights: Dict[PortfolioRating, Optional[float]] = field(
        default_factory=lambda: dict(DEFAULT_TARGET_WEIGHTS)
    )
    allow_short: bool = False
    fractional: bool = False
    max_position_weight: float = 0.20
    min_order_value: float = 1.0  # dollars; orders smaller than this are skipped

    def target_weight_for(self, rating: PortfolioRating) -> Optional[float]:
        """Return the target weight for a rating.

        ``None`` is a signal that the executor should leave the position
        unchanged (semantically: Hold).
        """
        weight = self.target_weights.get(rating)
        if weight is None:
            return None
        if not self.allow_short and weight < 0:
            weight = 0.0
        if weight > self.max_position_weight:
            weight = self.max_position_weight
        if weight < -self.max_position_weight:
            weight = -self.max_position_weight
        return weight

    def target_qty(self, weight: float, equity: float, price: float) -> float:
        """Convert a target weight + account equity + price into target shares.

        Rounds toward zero for integer modes so we never accidentally exceed
        the requested weight by a fractional share.
        """
        if equity <= 0 or price <= 0:
            return 0.0
        raw = (weight * equity) / price
        if self.fractional:
            return round(raw, 4)
        # truncate toward zero so we never exceed target weight
        return float(math.trunc(raw))
