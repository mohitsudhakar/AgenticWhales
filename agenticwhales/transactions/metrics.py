"""Deterministic FIFO PnL + behavioral-flag engine.

Faithful Python port of robinhood-analyzer/lib/metrics.ts. NO LLM is
involved; ``compute_metrics`` is a pure function of the transaction list:
same input -> same output. This is what makes it safe to unit-test
exhaustively with fixtures.

Behavior intentionally mirrors the TypeScript original, including:
- type matching via case-insensitive substring regexes,
- FIFO realized PnL on sells (matched against the oldest open buy lots),
- ``abs(amount)`` aggregation,
- month-span trade-frequency, and the exact behavioral-flag thresholds.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Dict, List, Tuple

from .models import (
    DateRange,
    LargestTrade,
    Metrics,
    MonthlyActivity,
    SymbolStat,
    TopConcentration,
    Transaction,
    TypeBreakdown,
)

_BUY = re.compile(r"buy", re.IGNORECASE)
_SELL = re.compile(r"sell", re.IGNORECASE)
_DIV = re.compile(r"div", re.IGNORECASE)
_DEPOSIT = re.compile(r"deposit", re.IGNORECASE)
_WITHDRAW = re.compile(r"withdraw", re.IGNORECASE)
_INTEREST = re.compile(r"interest", re.IGNORECASE)
_FEE = re.compile(r"fee", re.IGNORECASE)
_OPTION = re.compile(r"option", re.IGNORECASE)

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_MONTH = re.compile(r"^\d{4}-\d{2}")


def _round(n: float) -> float:
    """Match the TS round(): two decimals, round-half-away-from-zero-ish.

    Python's built-in round() uses banker's rounding; the TS original uses
    Math.round (round half up for positives). We replicate Math.round on the
    scaled value to keep parity with the source.
    """
    import math

    return math.floor(n * 100 + 0.5) / 100 if n >= 0 else -math.floor(-n * 100 + 0.5) / 100


def _month_span(start: str, end: str) -> int:
    """Inclusive month span between two ISO dates, min 1. Mirrors monthSpan()."""
    if not start or not end:
        return 0
    try:
        s = date.fromisoformat(start[:10])
        e = date.fromisoformat(end[:10])
    except ValueError:
        return 0
    return max(1, (e.year - s.year) * 12 + (e.month - s.month) + 1)


def compute_metrics(txns: List[Transaction]) -> Metrics:
    """Compute deterministic portfolio metrics from a transaction list."""
    dates = sorted(t.date for t in txns if _ISO_DATE.match(t.date or ""))
    start = dates[0] if dates else ""
    end = dates[-1] if dates else ""

    symbol_map: Dict[str, SymbolStat] = {}
    # FIFO open lots per symbol: list of [qty, price].
    fifo: Dict[str, List[List[float]]] = {}

    total_buys = 0
    total_sells = 0
    total_invested = 0.0
    total_proceeds = 0.0
    total_dividends = 0.0
    total_deposits = 0.0
    total_withdrawals = 0.0
    total_fees = 0.0
    option_trades = 0
    net_realized_pnl = 0.0
    largest_symbol = ""
    largest_amount = 0.0

    monthly: Dict[str, List[float]] = {}  # month -> [buys, sells, net]
    type_map: Dict[str, List[float]] = {}  # type -> [count, amount]

    def get_stat(sym: str) -> SymbolStat:
        if sym not in symbol_map:
            symbol_map[sym] = SymbolStat(symbol=sym)
        return symbol_map[sym]

    for t in txns:
        ttype = t.type or "Other"
        tb = type_map.setdefault(ttype, [0.0, 0.0])
        tb[0] += 1
        tb[1] += t.amount

        if _OPTION.search(ttype) or _OPTION.search(t.description or ""):
            option_trades += 1

        abs_amt = abs(t.amount)
        if abs_amt > abs(largest_amount):
            largest_symbol = t.symbol or ttype
            largest_amount = t.amount

        month = t.date[:7] if _ISO_MONTH.match(t.date or "") else "unknown"
        m = monthly.setdefault(month, [0.0, 0.0, 0.0])  # buys, sells, net

        if _BUY.search(ttype):
            total_buys += 1
            total_invested += abs_amt
            m[0] += 1
            m[2] -= abs_amt
            if t.symbol:
                s = get_stat(t.symbol)
                s.buys += 1
                s.trade_count += 1
                s.invested += abs_amt
                s.net_shares += t.quantity
                price = t.price or (abs_amt / t.quantity if t.quantity else 0.0)
                fifo.setdefault(t.symbol, []).append([t.quantity or 0.0, price])
        elif _SELL.search(ttype):
            total_sells += 1
            total_proceeds += abs_amt
            m[1] += 1
            m[2] += abs_amt
            if t.symbol:
                s = get_stat(t.symbol)
                s.sells += 1
                s.trade_count += 1
                s.proceeds += abs_amt
                s.net_shares -= t.quantity
                # FIFO realized PnL.
                sell_qty = t.quantity or 0.0
                sell_price = t.price or (abs_amt / t.quantity if t.quantity else 0.0)
                lots = fifo.setdefault(t.symbol, [])
                cost_basis = 0.0
                matched = 0.0
                while sell_qty > 0 and lots:
                    lot = lots[0]
                    take = min(sell_qty, lot[0])
                    cost_basis += take * lot[1]
                    matched += take
                    lot[0] -= take
                    sell_qty -= take
                    if lot[0] <= 0.000001:
                        lots.pop(0)
                if matched > 0:
                    realized = matched * sell_price - cost_basis
                    s.realized_pnl += realized
                    net_realized_pnl += realized
        elif _DIV.search(ttype):
            total_dividends += abs_amt
            if t.symbol:
                get_stat(t.symbol).dividends += abs_amt
        elif _DEPOSIT.search(ttype):
            total_deposits += abs_amt
        elif _WITHDRAW.search(ttype):
            total_withdrawals += abs_amt
        elif _INTEREST.search(ttype):
            total_dividends += 0  # tracked under type breakdown only
        elif _FEE.search(ttype):
            total_fees += abs_amt

    symbol_stats = sorted(symbol_map.values(), key=lambda s: s.invested, reverse=True)
    unique_symbols = len(symbol_stats)
    winning_symbols = sum(1 for s in symbol_stats if s.realized_pnl > 0)
    losing_symbols = sum(1 for s in symbol_stats if s.realized_pnl < 0)

    top = symbol_stats[0] if symbol_stats else None
    top_pct = (top.invested / total_invested) * 100 if (total_invested > 0 and top) else 0.0
    top_symbol = top.symbol if top else ""

    trade_count = total_buys + total_sells
    months_span = _month_span(start, end)
    trade_freq = trade_count / months_span if months_span > 0 else float(trade_count)
    avg_trade_size = (total_invested + total_proceeds) / trade_count if trade_count > 0 else 0.0

    monthly_activity = [
        MonthlyActivity(month=k, buys=int(v[0]), sells=int(v[1]), net=_round(v[2]))
        for k, v in sorted(monthly.items())
        if k != "unknown"
    ]

    type_breakdown = sorted(
        (TypeBreakdown(type=k, count=int(v[0]), amount=_round(v[1])) for k, v in type_map.items()),
        key=lambda tb: tb.count,
        reverse=True,
    )

    behavioral_flags = _derive_flags(
        trade_freq=trade_freq,
        top_symbol=top_symbol,
        top_pct=top_pct,
        option_trades=option_trades,
        trade_count=trade_count,
        unique_symbols=unique_symbols,
        winning_symbols=winning_symbols,
        losing_symbols=losing_symbols,
        symbol_stats=symbol_stats,
    )

    # Round symbol stats for output (mutate copies to keep parity with TS).
    rounded_stats = [
        SymbolStat(
            symbol=s.symbol,
            buys=s.buys,
            sells=s.sells,
            net_shares=_round(s.net_shares),
            invested=_round(s.invested),
            proceeds=_round(s.proceeds),
            realized_pnl=_round(s.realized_pnl),
            dividends=_round(s.dividends),
            trade_count=s.trade_count,
        )
        for s in symbol_stats
    ]

    return Metrics(
        total_transactions=len(txns),
        date_range=DateRange(start=start, end=end),
        total_buys=total_buys,
        total_sells=total_sells,
        total_invested=_round(total_invested),
        total_proceeds=_round(total_proceeds),
        total_dividends=_round(total_dividends),
        total_deposits=_round(total_deposits),
        total_withdrawals=_round(total_withdrawals),
        total_fees=_round(total_fees),
        net_realized_pnl=_round(net_realized_pnl),
        unique_symbols=unique_symbols,
        trade_frequency_per_month=_round(trade_freq),
        avg_trade_size=_round(avg_trade_size),
        largest_trade=LargestTrade(symbol=largest_symbol, amount=_round(largest_amount)),
        top_concentration=TopConcentration(symbol=top_symbol, pct_of_invested=_round(top_pct)),
        option_trades=option_trades,
        winning_symbols=winning_symbols,
        losing_symbols=losing_symbols,
        symbol_stats=rounded_stats,
        monthly_activity=monthly_activity,
        type_breakdown=type_breakdown,
        behavioral_flags=behavioral_flags,
    )


def _derive_flags(
    *,
    trade_freq: float,
    top_symbol: str,
    top_pct: float,
    option_trades: int,
    trade_count: int,
    unique_symbols: int,
    winning_symbols: int,
    losing_symbols: int,
    symbol_stats: List[SymbolStat],
) -> List[str]:
    """Port of deriveFlags() — identical thresholds and wording."""
    flags: List[str] = []
    if trade_freq > 20:
        flags.append("Very high trade frequency (>20/month) — possible overtrading.")
    elif trade_freq > 8:
        flags.append("Elevated trade frequency — active trading style.")
    if top_pct > 40:
        flags.append(
            f"High concentration: {top_symbol} is {top_pct:.0f}% of capital deployed."
        )
    if option_trades > 0 and option_trades / max(trade_count, 1) > 0.25:
        flags.append("Significant options activity — higher leverage/risk exposure.")
    if 0 < unique_symbols < 5:
        flags.append("Low diversification — fewer than 5 distinct positions.")
    churners = sum(1 for s in symbol_stats if s.buys >= 2 and s.sells >= 2)
    if churners >= 3:
        flags.append("Round-trip churning on multiple names — chasing/timing pattern.")
    losers = [s for s in symbol_stats if s.realized_pnl < 0]
    if losing_symbols > winning_symbols and len(losers) >= 3:
        flags.append("More losing than winning realized positions — review exit discipline.")
    return flags
