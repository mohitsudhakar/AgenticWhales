"""CLI entry point for ad-hoc backtests.

    python -m tradingagents.backtest.runner --ticker AAPL --start 2024-01-01 --end 2024-12-31 \
        --rating Buy --starting-cash 100000

Uses a fixed-rating decision source by default so the user can sanity-check
the executor pipeline end-to-end against real historical bars before
wiring in the (expensive) agent graph as a DecisionSource.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Dict

import pandas as pd

from tradingagents.agents.schemas import PortfolioRating

from .bars import load_history
from .decision_source import FixedRatingDecisionSource
from .harness import BacktestHarness
from ..execution.sizing import DEFAULT_TARGET_WEIGHTS, SizingPolicy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tradingagents-backtest")
    parser.add_argument("--ticker", required=True, help="Single ticker symbol (e.g. AAPL)")
    parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--rating", default="Buy",
                        choices=[r.value for r in PortfolioRating],
                        help="Fixed rating to apply on every rebalance bar")
    parser.add_argument("--starting-cash", type=float, default=100_000.0)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--commission-per-share", type=float, default=0.0)
    parser.add_argument("--rebalance-every-n-bars", type=int, default=1)
    parser.add_argument("--target-weight", type=float, default=None,
                        help="Override target weight for the chosen rating")
    parser.add_argument("--allow-short", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.verbose:
        logging.basicConfig(level=logging.INFO)

    rating = PortfolioRating(args.rating)
    bars: Dict[str, pd.DataFrame] = {args.ticker.upper(): load_history(args.ticker, args.start, args.end)}
    if bars[args.ticker.upper()].empty:
        print(f"No bars returned for {args.ticker} between {args.start} and {args.end}", file=sys.stderr)
        return 2

    weights = dict(DEFAULT_TARGET_WEIGHTS)
    if args.target_weight is not None:
        weights[rating] = args.target_weight
    sizing = SizingPolicy(target_weights=weights, allow_short=args.allow_short)

    harness = BacktestHarness(
        bars=bars,
        decision_source=FixedRatingDecisionSource(rating),
        sizing=sizing,
        starting_cash=args.starting_cash,
        slippage_bps=args.slippage_bps,
        commission_per_share=args.commission_per_share,
        rebalance_every_n_bars=args.rebalance_every_n_bars,
    )
    result = harness.run()

    print(f"\nBacktest: {args.ticker} {args.start} -> {args.end} (rating={rating.value})\n")
    print(result.summary_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
