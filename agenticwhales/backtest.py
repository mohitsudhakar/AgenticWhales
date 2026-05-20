"""Backtest replay loop for Phase 3.

Given a recipe (or just a ticker + decision generator), step through history one
trading day at a time, generate a `PortfolioDecision` with a *bounded view* of
the past, simulate paper-trading, and report equity curve + hit rate + Brier.

The replay deliberately separates three concerns:

  1. Data access — `_load_history` fetches a DataFrame up front and is wrapped
     by `as_of_date` for every per-day call to defend against look-ahead bugs
     in the decision generator.
  2. Decision generation — `DecisionGenerator` is a callable taking
     (symbol, as_of, history_so_far) → PortfolioDecision. Two built-ins:
       * `momentum_stub_generator` — deterministic, no LLM cost. Useful for
         wiring tests and quick sanity checks.
       * (live mode — supply your own callable that hits the real LangGraph
         runner; deferred — wire after the streaming worker lands.)
  3. Simulation — track an `Account` (cash + positions), fill at next-bar open,
     mark realized PnL on stop-loss hit or hold-days expiry.

The output is a `BacktestResult` with the metrics the /fund UI will chart.
"""

from __future__ import annotations

import datetime as _dt
import logging
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Protocol

import pandas as pd

from .agents.schemas import PortfolioDecision, PortfolioRating
from .asof import LookAheadViolation, as_of_date, assert_as_of
from .paper import kelly_sizing

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Decision generator protocol
# ---------------------------------------------------------------------------

class DecisionGenerator(Protocol):
    def __call__(
        self,
        symbol: str,
        as_of: _dt.date,
        history: pd.DataFrame,
    ) -> Optional[PortfolioDecision]: ...


def momentum_stub_generator(
    symbol: str,
    as_of: _dt.date,
    history: pd.DataFrame,
) -> Optional[PortfolioDecision]:
    """Deterministic stub: rating from short-vs-long SMA, scalars from realized vol.

    Used for wiring tests and as the default `--stub` backtest mode. Never returns
    None — the loop always gets a decision, but `Hold` skips order placement.
    """
    if len(history) < 50:
        return None
    close = history["Close"].astype(float)
    sma_short = close.rolling(20).mean().iloc[-1]
    sma_long = close.rolling(50).mean().iloc[-1]
    last = float(close.iloc[-1])

    # Annualized realized vol from last 20 daily returns.
    daily_ret = close.pct_change().dropna().tail(20)
    realized_vol = float(daily_ret.std() * math.sqrt(252) * 100) if len(daily_ret) > 1 else 20.0

    if not math.isfinite(sma_short) or not math.isfinite(sma_long):
        return None

    if sma_short > sma_long * 1.02:
        rating = PortfolioRating.OVERWEIGHT
        expected_ret = 8.0
        prob = 0.58
        stop = last * 0.95
    elif sma_short < sma_long * 0.98:
        rating = PortfolioRating.UNDERWEIGHT
        expected_ret = -7.0
        prob = 0.55
        stop = last * 1.05
    else:
        rating = PortfolioRating.HOLD
        expected_ret = 0.0
        prob = 0.5
        stop = last * 0.95

    return PortfolioDecision(
        rating=rating,
        stop_loss=stop,
        expected_return_pct=expected_ret,
        expected_volatility_pct=max(realized_vol, 5.0),
        prob_of_profit=prob,
        expected_hold_days=20,
        executive_summary=(
            f"Stub backtest signal for {symbol}: SMA-20={sma_short:.2f} vs "
            f"SMA-50={sma_long:.2f} → {rating.value}."
        ),
        investment_thesis=(
            f"Momentum stub for {symbol}. Used by the backtest replay loop only — "
            f"not a real PM decision. sma_20={sma_short:.2f} sma_50={sma_long:.2f}"
        ),
    )


# ---------------------------------------------------------------------------
# Account + simulation
# ---------------------------------------------------------------------------

@dataclass
class _OpenTrade:
    symbol: str
    entry_date: _dt.date
    entry_price: float
    qty: float                    # signed
    stop: float
    hold_days_remaining: int
    predicted_return_pct: float
    predicted_prob: float


@dataclass
class _ClosedTrade:
    symbol: str
    entry_date: _dt.date
    exit_date: _dt.date
    entry_price: float
    exit_price: float
    qty: float
    realized_return_pct: float    # in trade-PnL terms (positive = profit, regardless of side)
    predicted_return_pct: float
    predicted_prob: float
    reason: str                   # 'stop' | 'time' | 'eof'


@dataclass
class BacktestResult:
    symbol: str
    from_date: _dt.date
    to_date: _dt.date
    starting_cash: float
    final_nav: float
    total_decisions: int
    closed_trades: int
    hit_rate: float
    brier: float
    max_drawdown_pct: float
    equity_curve: List[Dict] = field(default_factory=list)  # [{date, nav}]
    trades: List[Dict] = field(default_factory=list)


def _load_history(symbol: str, start: _dt.date, end: _dt.date) -> pd.DataFrame:
    """Load OHLCV via yfinance, bounded by the current as-of date if set.

    Returns a DataFrame indexed by date. Columns: Open, High, Low, Close, Volume.
    Raises if the requested window is empty.
    """
    import yfinance as yf  # lazy

    assert_as_of(end)
    ticker = yf.Ticker(symbol.upper())
    df = ticker.history(start=start.isoformat(), end=(end + _dt.timedelta(days=1)).isoformat())
    if df.empty:
        raise LookAheadViolation(f"no OHLCV data for {symbol} in {start}..{end}")
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    return df


def run_backtest(
    symbol: str,
    from_date,
    to_date,
    *,
    decision_fn: Optional[DecisionGenerator] = None,
    starting_cash: float = 100_000.0,
    kelly_cap: float = 0.10,
    history: Optional[pd.DataFrame] = None,
    warmup_days: int = 60,
) -> BacktestResult:
    """Replay a backtest for one symbol.

    `history` may be passed in directly (tests / fixtures); otherwise loaded
    from yfinance. `warmup_days` extends the loaded window backward so the
    decision generator has indicator history on day 1.

    The decision is computed using only history up to and including the as-of
    date. The fill (if any) happens at the *next* trading day's open — never on
    the as-of bar itself. This is the strict-causal convention.
    """
    fd = _coerce_date(from_date)
    td = _coerce_date(to_date)
    if td < fd:
        raise ValueError("to_date must be >= from_date")
    decision_fn = decision_fn or momentum_stub_generator

    if history is None:
        history = _load_history(symbol, fd - _dt.timedelta(days=warmup_days), td)

    trading_days = [d.date() for d in history.index if fd <= d.date() <= td]
    if not trading_days:
        raise ValueError(f"no trading days in {fd}..{td} for {symbol}")

    cash = float(starting_cash)
    open_trade: Optional[_OpenTrade] = None
    closed: List[_ClosedTrade] = []
    equity_curve: List[Dict] = []
    decisions_made = 0
    peak_nav = starting_cash
    max_dd_pct = 0.0

    for i, day in enumerate(trading_days):
        bar = history.loc[history.index.date == day]
        if bar.empty:
            continue
        bar_open = float(bar["Open"].iloc[0])
        bar_high = float(bar["High"].iloc[0])
        bar_low = float(bar["Low"].iloc[0])
        bar_close = float(bar["Close"].iloc[0])

        # 1) On-open: close any expired trade (time-based exit) BEFORE generating
        #    a new one — keeps decisions independent of in-flight position state.
        if open_trade and open_trade.hold_days_remaining <= 0:
            closed.append(_close_trade(open_trade, day, bar_open, "time"))
            cash += open_trade.qty * bar_open
            open_trade = None

        # 2) Intraday: check stop. Strict-causal — if the high/low touched the
        #    stop, we get out at the stop price (slight optimism; ignores gaps).
        if open_trade is not None:
            triggered, fill_px = _check_stop(open_trade, bar_low, bar_high)
            if triggered:
                closed.append(_close_trade(open_trade, day, fill_px, "stop"))
                cash += open_trade.qty * fill_px
                open_trade = None

        # 3) On-close: generate a decision using history up through `day` only.
        if open_trade is None:
            history_slice = history.loc[history.index.date <= day]
            with as_of_date(day):
                decision = decision_fn(symbol, day, history_slice)
            if decision is not None:
                decisions_made += 1
                if decision.rating not in (PortfolioRating.HOLD,) and i + 1 < len(trading_days):
                    # Fill at next bar's open.
                    next_day = trading_days[i + 1]
                    next_bar = history.loc[history.index.date == next_day]
                    if not next_bar.empty:
                        fill_px = float(next_bar["Open"].iloc[0])
                        nav = cash  # flat between trades
                        sizing = kelly_sizing(
                            decision, nav=nav, last_price=fill_px,
                            kelly_fraction_cap=kelly_cap,
                        )
                        if sizing.qty != 0:
                            cash -= sizing.qty * fill_px
                            open_trade = _OpenTrade(
                                symbol=symbol,
                                entry_date=next_day,
                                entry_price=fill_px,
                                qty=sizing.qty,
                                stop=decision.stop_loss or fill_px * 0.95,
                                hold_days_remaining=decision.expected_hold_days or 20,
                                predicted_return_pct=decision.expected_return_pct or 0.0,
                                predicted_prob=decision.prob_of_profit or 0.5,
                            )

        # 4) End-of-day mark-to-market.
        position_value = open_trade.qty * bar_close if open_trade else 0.0
        nav_today = cash + position_value
        equity_curve.append({"date": day.isoformat(), "nav": round(nav_today, 2)})
        peak_nav = max(peak_nav, nav_today)
        dd = (peak_nav - nav_today) / peak_nav if peak_nav > 0 else 0.0
        max_dd_pct = max(max_dd_pct, dd)

        if open_trade:
            open_trade.hold_days_remaining -= 1

    # End-of-window: close any remaining trade at last close.
    if open_trade:
        last_close = float(history.loc[history.index.date <= trading_days[-1]]["Close"].iloc[-1])
        closed.append(_close_trade(open_trade, trading_days[-1], last_close, "eof"))
        cash += open_trade.qty * last_close

    hits = sum(1 for t in closed if t.realized_return_pct > 0)
    hit_rate = hits / len(closed) if closed else 0.0
    brier = (
        sum((t.predicted_prob - (1.0 if t.realized_return_pct > 0 else 0.0)) ** 2
            for t in closed) / len(closed)
        if closed else 0.0
    )

    return BacktestResult(
        symbol=symbol,
        from_date=fd,
        to_date=td,
        starting_cash=starting_cash,
        final_nav=cash,
        total_decisions=decisions_made,
        closed_trades=len(closed),
        hit_rate=round(hit_rate, 4),
        brier=round(brier, 6),
        max_drawdown_pct=round(max_dd_pct, 4),
        equity_curve=equity_curve,
        trades=[_trade_to_dict(t) for t in closed],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _coerce_date(value) -> _dt.date:
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    return _dt.date.fromisoformat(str(value))


def _check_stop(trade: _OpenTrade, bar_low: float, bar_high: float):
    """Return (triggered, fill_price). Long: stop below entry triggers when low ≤ stop.
    Short: stop above entry triggers when high ≥ stop."""
    if trade.qty > 0 and bar_low <= trade.stop:
        return True, trade.stop
    if trade.qty < 0 and bar_high >= trade.stop:
        return True, trade.stop
    return False, 0.0


def _close_trade(trade: _OpenTrade, exit_date: _dt.date, exit_price: float, reason: str) -> _ClosedTrade:
    realized_return_pct = (
        (exit_price - trade.entry_price) / trade.entry_price * 100.0
        if trade.qty > 0
        else (trade.entry_price - exit_price) / trade.entry_price * 100.0
    )
    return _ClosedTrade(
        symbol=trade.symbol,
        entry_date=trade.entry_date,
        exit_date=exit_date,
        entry_price=trade.entry_price,
        exit_price=exit_price,
        qty=trade.qty,
        realized_return_pct=realized_return_pct,
        predicted_return_pct=trade.predicted_return_pct,
        predicted_prob=trade.predicted_prob,
        reason=reason,
    )


def _trade_to_dict(t: _ClosedTrade) -> Dict:
    return {
        "symbol": t.symbol,
        "entry_date": t.entry_date.isoformat(),
        "exit_date": t.exit_date.isoformat(),
        "entry_price": round(t.entry_price, 4),
        "exit_price": round(t.exit_price, 4),
        "qty": round(t.qty, 6),
        "realized_return_pct": round(t.realized_return_pct, 4),
        "predicted_return_pct": round(t.predicted_return_pct, 4),
        "predicted_prob": round(t.predicted_prob, 4),
        "reason": t.reason,
    }
