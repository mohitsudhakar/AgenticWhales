"""Transactions analyzer.

A self-contained port of the robinhood-analyzer TypeScript library:

- ``metrics``  — deterministic FIFO PnL + behavioral flags (NO LLM).
- ``parser``   — CSV -> typed :class:`Transaction` models (primary input path).
- ``extract``  — LLM extraction of transactions from raw text.
- ``analyze``  — LLM 4-lens behavioral review of computed metrics.

All paper / analysis only — this module never submits orders.
"""

from __future__ import annotations

from typing import List, Optional

from .analyze import generate_analysis
from .extract import extract_transactions
from .metrics import compute_metrics
from .models import (
    Analysis,
    AnalysisSection,
    AnalyzeResult,
    DateRange,
    LargestTrade,
    Metrics,
    MonthlyActivity,
    SymbolStat,
    TopConcentration,
    Transaction,
    TypeBreakdown,
)
from .parser import (
    parse_transactions_csv,
    parse_transactions_csv_file,
)

__all__ = [
    "Transaction",
    "SymbolStat",
    "Metrics",
    "Analysis",
    "AnalysisSection",
    "AnalyzeResult",
    "DateRange",
    "LargestTrade",
    "TopConcentration",
    "MonthlyActivity",
    "TypeBreakdown",
    "compute_metrics",
    "parse_transactions_csv",
    "parse_transactions_csv_file",
    "extract_transactions",
    "generate_analysis",
    "analyze_transactions",
    "analyze_csv_file",
]


def analyze_transactions(
    transactions: List[Transaction],
    *,
    run_llm: bool = False,
    llm=None,
    provider: str = "openai",
    model: str = "gpt-5.4",
    base_url: Optional[str] = None,
) -> AnalyzeResult:
    """Compute metrics and (optionally) the LLM behavioral analysis.

    The deterministic metrics are always computed. The LLM 4-lens analysis is
    only run when ``run_llm`` is True (or an ``llm`` is supplied), so the core
    path requires no API key and no network.
    """
    metrics = compute_metrics(transactions)
    warnings: List[str] = []
    analysis = Analysis()
    if run_llm or llm is not None:
        try:
            analysis = generate_analysis(
                metrics, transactions, llm=llm, provider=provider, model=model, base_url=base_url
            )
        except Exception as e:  # noqa: BLE001
            warnings.append(f"LLM analysis failed: {e}")
    return AnalyzeResult(
        transactions=transactions, metrics=metrics, analysis=analysis, warnings=warnings
    )


def analyze_csv_file(
    path: str,
    *,
    run_llm: bool = False,
    llm=None,
    provider: str = "openai",
    model: str = "gpt-5.4",
    base_url: Optional[str] = None,
) -> AnalyzeResult:
    """Parse a CSV file and analyze it. Convenience wrapper."""
    txns = parse_transactions_csv_file(path)
    return analyze_transactions(
        txns, run_llm=run_llm, llm=llm, provider=provider, model=model, base_url=base_url
    )
