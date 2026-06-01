"""Resolved ticker-date pairs for diversity-engine evaluation.

Each fixture is a known (ticker, decision_date, hold_days, realized_return,
spy_return) tuple. The realized returns are computed from yfinance close
prices at decision_date and decision_date + hold_days. We snapshot them
into this file so the eval is fully offline and deterministic — the
yfinance pull happens once when the fixtures are refreshed, not on every
CI run.

Refresh procedure:
    python -m tests.evals.refresh_fixtures > tests/evals/fixtures.py.new
    mv tests/evals/fixtures.py.new tests/evals/fixtures.py

The fixture set is deliberately small (target N=20) so a full eval run
costs < 60 LLM calls per arm. Larger N can be added later; the scoring
math is sample-size aware (Beta-Binomial shrinkage in the scorer).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResolvedFixture:
    """One resolved (ticker, decision_date) → (realized return) row."""

    ticker: str
    decision_date: str  # YYYY-MM-DD
    hold_days: int
    realized_return: float  # raw return over hold_days
    spy_return: float       # SPY return over the same window
    note: str = ""

    @property
    def alpha(self) -> float:
        return self.realized_return - self.spy_return

    @property
    def profitable(self) -> bool:
        """Hit/miss label for Brier scoring against predicted_prob_of_profit."""
        return self.realized_return > 0.0


# Starter fixture set — placeholders pending the first refresh against
# yfinance. Values chosen to span sectors / regimes (mega-cap tech, energy,
# financials, healthcare, defensive). Refresh script will overwrite this
# block with current numbers.
FIXTURES: list[ResolvedFixture] = [
    # Mega-cap tech
    ResolvedFixture("NVDA", "2025-09-15", 5, 0.043, 0.012, "AI demand cycle"),
    ResolvedFixture("AAPL", "2025-09-15", 5, 0.011, 0.012, "in line with SPY"),
    ResolvedFixture("MSFT", "2025-09-15", 5, 0.022, 0.012, "modest beat"),
    ResolvedFixture("GOOGL", "2025-10-20", 5, -0.018, 0.004, "search ad headwind"),
    # Cyclicals
    ResolvedFixture("XOM",  "2025-10-20", 5, 0.031, 0.004, "oil bid"),
    ResolvedFixture("CAT",  "2025-11-10", 5, -0.024, -0.008, "guidance cut"),
    # Financials
    ResolvedFixture("JPM",  "2025-11-10", 5, 0.014, -0.008, "rate steepening"),
    ResolvedFixture("BAC",  "2025-12-01", 5, -0.009, 0.006, "deposit beta worry"),
    # Healthcare / defensive
    ResolvedFixture("UNH",  "2025-12-01", 5, 0.018, 0.006, "MA reprice"),
    ResolvedFixture("KO",   "2026-01-12", 5, 0.005, 0.011, "lagged SPY"),
    # Index / ETF baselines
    ResolvedFixture("SPY",  "2025-09-15", 5, 0.012, 0.012, "by definition zero alpha"),
    ResolvedFixture("QQQ",  "2025-10-20", 5, 0.019, 0.004, "tech-heavy outperform"),
    # International
    ResolvedFixture("BABA", "2026-01-12", 5, -0.037, 0.011, "China sentiment"),
    ResolvedFixture("TSM",  "2026-02-03", 5, 0.028, 0.009, "node leadership"),
    # Small/mid cap
    ResolvedFixture("IWM",  "2026-02-03", 5, -0.011, 0.009, "small cap drag"),
    # Energy transition
    ResolvedFixture("TSLA", "2026-03-01", 5, 0.055, -0.003, "margin reset"),
    ResolvedFixture("FSLR", "2026-03-01", 5, -0.042, -0.003, "policy noise"),
    # Travel / consumer
    ResolvedFixture("BKNG", "2026-04-10", 5, 0.022, 0.014, "in-line bookings"),
    ResolvedFixture("DIS",  "2026-04-10", 5, -0.015, 0.014, "streaming churn"),
    # Defensive industrial
    ResolvedFixture("LMT",  "2026-05-01", 5, 0.018, 0.007, "defense bid"),
]


def by_ticker(ticker: str) -> ResolvedFixture | None:
    for f in FIXTURES:
        if f.ticker == ticker:
            return f
    return None
