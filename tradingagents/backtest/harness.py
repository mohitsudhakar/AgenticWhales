"""Walk-forward backtest harness.

For each bar in the replay range:
1. Ask each ticker's :class:`DecisionSource` for a PortfolioDecision.
2. Run the :class:`Executor` against a :class:`SimulatedBroker`. The
   executor sizes the trade using the *open* of the next bar so a
   decision made on day N fills on day N+1's open (avoids lookahead).
3. Mark-to-market at day N+1's close and append to the equity curve.

The harness intentionally mirrors the live flow: same Executor, same
sizing policy, same idempotency keys. Only the broker swaps.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Mapping, Optional, Sequence

import pandas as pd

from tradingagents.execution.brokers import SimulatedBroker
from tradingagents.execution.executor import Executor
from tradingagents.execution.schemas import ExecutionResult
from tradingagents.execution.sizing import SizingPolicy

from .decision_source import DecisionSource
from .metrics import equity_metrics

logger = logging.getLogger(__name__)


@dataclass
class EquityPoint:
    date: str
    equity: float
    cash: float
    positions: Dict[str, float]


@dataclass
class BacktestResult:
    equity_curve: List[EquityPoint] = field(default_factory=list)
    trades: List[ExecutionResult] = field(default_factory=list)
    metrics: Dict[str, float] = field(default_factory=dict)

    def equity_series(self) -> pd.Series:
        return pd.Series(
            [p.equity for p in self.equity_curve],
            index=pd.to_datetime([p.date for p in self.equity_curve]),
        )

    def summary_text(self) -> str:
        m = self.metrics
        return (
            f"Equity:   ${m.get('final_equity', 0):,.2f}\n"
            f"Total:    {m.get('total_return', 0) * 100:+.2f}%\n"
            f"CAGR:     {m.get('cagr', 0) * 100:+.2f}%\n"
            f"Sharpe:   {m.get('sharpe', 0):.2f}\n"
            f"Max DD:   {m.get('max_drawdown', 0) * 100:.2f}%\n"
            f"Trades:   {sum(1 for t in self.trades if t.order)}\n"
        )


class BacktestHarness:
    """Run a walk-forward backtest of the agent-driven executor."""

    def __init__(
        self,
        *,
        bars: Mapping[str, pd.DataFrame],
        decision_source: DecisionSource,
        sizing: Optional[SizingPolicy] = None,
        starting_cash: float = 100_000.0,
        slippage_bps: float = 0.0,
        commission_per_share: float = 0.0,
        commission_min: float = 0.0,
        rebalance_every_n_bars: int = 1,
    ) -> None:
        if not bars:
            raise ValueError("at least one symbol's bars are required")
        self.bars = {sym.strip().upper(): df for sym, df in bars.items()}
        self.decision_source = decision_source
        self.sizing = sizing or SizingPolicy()
        self.starting_cash = starting_cash
        self.rebalance_every_n_bars = max(1, rebalance_every_n_bars)
        self.broker = SimulatedBroker(
            starting_cash=starting_cash,
            slippage_bps=slippage_bps,
            commission_per_share=commission_per_share,
            commission_min=commission_min,
            allow_short=self.sizing.allow_short,
        )
        self.executor = Executor(self.broker, self.sizing, wait_for_fill=False)

    def run(self) -> BacktestResult:
        timeline = self._unified_timeline()
        result = BacktestResult()

        for i, ts in enumerate(timeline):
            day_of_run = i  # 0-indexed bar
            todays_prices = self._prices_at(ts, "open")
            closes = self._prices_at(ts, "close")

            # 1. Decide using *yesterday's* close-equivalent state (already in broker).
            #    Place orders that fill at today's open.
            if i > 0 and (i % self.rebalance_every_n_bars == 0):
                # Set broker reference prices to today's open before placing orders
                self.broker.set_reference_prices(todays_prices)
                for symbol in self.bars:
                    open_px = todays_prices.get(symbol)
                    if open_px is None:
                        continue
                    decision = self.decision_source.decide(symbol, ts.date())
                    if decision is None:
                        continue
                    res = self.executor.execute(
                        symbol,
                        decision,
                        trade_date=ts.date().isoformat(),
                        reference_price=open_px,
                    )
                    result.trades.append(res)
                    if res.action == "ERROR":
                        logger.warning(
                            "[%s] %s error: %s", ts.date(), symbol, res.reason,
                        )

            # 2. Mark to market at today's close.
            equity = self.broker.mark_to_market(closes)
            positions = {p.symbol: p.qty for p in self.broker.get_positions()}
            result.equity_curve.append(
                EquityPoint(
                    date=ts.date().isoformat(),
                    equity=equity,
                    cash=self.broker.get_account().cash,
                    positions=positions,
                )
            )

        result.metrics = equity_metrics(
            [p.equity for p in result.equity_curve],
            initial_equity=self.starting_cash,
        )
        return result

    # -------------------------------------------------------------- helpers
    def _unified_timeline(self) -> List[pd.Timestamp]:
        index = None
        for df in self.bars.values():
            if index is None:
                index = df.index
            else:
                index = index.union(df.index)
        return list(index.sort_values()) if index is not None else []

    def _prices_at(self, ts: pd.Timestamp, column: str) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for sym, df in self.bars.items():
            if ts in df.index:
                val = df.loc[ts, column]
                if pd.notna(val):
                    out[sym] = float(val)
        return out
