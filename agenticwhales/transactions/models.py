"""Typed data shapes for the transactions analyzer.

Ported from robinhood-analyzer/lib/types.ts. These are the canonical
structures shared by the deterministic metrics engine, the CSV parser, and
the LLM extraction / analysis paths.

Everything here is PAPER / analysis-only: we never submit orders. We only
read a transaction history (CSV or extracted text) and describe it.
"""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field


class Transaction(BaseModel):
    """A single brokerage activity row.

    ``amount`` is the SIGNED cash flow from the account holder's perspective:
    money LEAVING the account (buys, withdrawals, fees) is NEGATIVE; money
    ENTERING (sells, dividends, deposits, interest) is POSITIVE. The metrics
    engine works on ``abs(amount)`` per the original metrics.ts logic, so the
    sign is informational for downstream consumers.
    """

    date: str = Field("", description="ISO yyyy-mm-dd if parseable, else raw")
    type: str = Field("Other", description="Buy, Sell, Dividend, Deposit, ...")
    symbol: str = Field("", description="ticker (uppercase) or '' for cash events")
    description: str = ""
    quantity: float = Field(0.0, description="shares/contracts; 0 if N/A")
    price: float = Field(0.0, description="per-unit price; 0 if N/A")
    amount: float = Field(0.0, description="signed cash flow")


class SymbolStat(BaseModel):
    symbol: str
    buys: int = 0
    sells: int = 0
    net_shares: float = 0.0
    invested: float = 0.0  # total cash spent buying
    proceeds: float = 0.0  # total cash received selling
    realized_pnl: float = 0.0  # proceeds - cost of sold shares (FIFO)
    dividends: float = 0.0
    trade_count: int = 0


class DateRange(BaseModel):
    start: str = ""
    end: str = ""


class LargestTrade(BaseModel):
    symbol: str = ""
    amount: float = 0.0


class TopConcentration(BaseModel):
    symbol: str = ""
    pct_of_invested: float = 0.0


class MonthlyActivity(BaseModel):
    month: str
    buys: int = 0
    sells: int = 0
    net: float = 0.0


class TypeBreakdown(BaseModel):
    type: str
    count: int = 0
    amount: float = 0.0


class Metrics(BaseModel):
    """Deterministic portfolio metrics. No LLM involved."""

    total_transactions: int = 0
    date_range: DateRange = Field(default_factory=DateRange)
    total_buys: int = 0
    total_sells: int = 0
    total_invested: float = 0.0
    total_proceeds: float = 0.0
    total_dividends: float = 0.0
    total_deposits: float = 0.0
    total_withdrawals: float = 0.0
    total_fees: float = 0.0
    net_realized_pnl: float = 0.0
    unique_symbols: int = 0
    trade_frequency_per_month: float = 0.0
    avg_trade_size: float = 0.0
    largest_trade: LargestTrade = Field(default_factory=LargestTrade)
    top_concentration: TopConcentration = Field(default_factory=TopConcentration)
    option_trades: int = 0
    winning_symbols: int = 0
    losing_symbols: int = 0
    symbol_stats: List[SymbolStat] = Field(default_factory=list)
    monthly_activity: List[MonthlyActivity] = Field(default_factory=list)
    type_breakdown: List[TypeBreakdown] = Field(default_factory=list)
    behavioral_flags: List[str] = Field(default_factory=list)


class AnalysisSection(BaseModel):
    title: str
    summary: str = ""
    points: List[str] = Field(default_factory=list)


class Analysis(BaseModel):
    """LLM 4-lens behavioral review. Educational, not fiduciary advice."""

    headline: str = "Analysis complete."
    investor_archetype: str = "Investor"
    risk_score: int = 50  # 0-100
    discipline_score: int = 50  # 0-100
    diversification_score: int = 50  # 0-100
    sections: List[AnalysisSection] = Field(default_factory=list)
    suggestions: List[str] = Field(default_factory=list)
    habits_to_keep: List[str] = Field(default_factory=list)
    habits_to_change: List[str] = Field(default_factory=list)
    closing_reflection: str = ""


class AnalyzeResult(BaseModel):
    transactions: List[Transaction] = Field(default_factory=list)
    metrics: Metrics = Field(default_factory=Metrics)
    analysis: Analysis = Field(default_factory=Analysis)
    warnings: List[str] = Field(default_factory=list)
