"""Quantitative risk metrics — Marketto-backed.

AgenticWhales fetches OHLCV and technical indicators, but nothing in the
repo computes *realized* risk statistics — annualized volatility, Sharpe,
or max drawdown. Yet the Portfolio Manager's ``PortfolioDecision`` schema
explicitly asks the LLM to self-report ``expected_volatility_pct``
"anchored in historical realized vol", and the Kelly sizer + calibration
head both depend on that number being grounded rather than hallucinated.

This module closes that gap by bridging the OHLCV DataFrame that
``stockstats_utils.load_ohlcv`` already produces — cached and filtered to
``curr_date`` to prevent look-ahead bias — into `marketto`'s
``risk_summary``. We do **not** add a new market-data dependency or
re-fetch anything: the frame handed to Marketto is the same one the other
analysts see, so no look-ahead is introduced.

`marketto` is an OPTIONAL dependency (the ``quant`` extra). When it is not
installed the public entry point returns an informative message instead of
raising, so the agent graph degrades gracefully.

Design mirrors the rest of ``dataflows/``: a string-returning public
function for LLM consumption, and a separable pure-compute helper
(``compute_risk_metrics``) that takes a DataFrame so tests stay hermetic.
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


_INSTALL_HINT = (
    "Quantitative risk metrics require the optional `marketto` package. "
    "Install with: pip install -e '.[quant]'  (or pip install marketto)."
)


def _marketto_available() -> bool:
    try:
        import marketto  # noqa: F401
        from marketto.models import OHLCVFrame  # noqa: F401
        from marketto.processing import risk_summary  # noqa: F401
    except Exception:  # pragma: no cover - exercised only without marketto
        return False
    return True


def _to_ohlcv_frame(symbol: str, df: pd.DataFrame):
    """Convert an AgenticWhales OHLCV DataFrame to a Marketto ``OHLCVFrame``.

    The AW frame (from ``load_ohlcv``) has columns
    ``Date, Open, High, Low, Close, Volume`` with ``auto_adjust=True`` (so
    ``Close`` is already split/dividend-adjusted). Marketto wants lowercase
    columns and a ``date`` column/index; it fills ``adj_close`` from
    ``close`` when absent, which is exactly right here.
    """
    from marketto.models import OHLCVFrame

    clean = df.rename(columns={c: str(c).lower() for c in df.columns})
    # Marketto detects a lowercase ``date`` column and sets it as the index.
    return OHLCVFrame.from_dataframe(symbol, clean)


def compute_risk_metrics(
    symbol: str,
    df: pd.DataFrame,
    *,
    look_back_days: int = 252,
    risk_free: float = 0.0,
) -> dict:
    """Compute realized risk metrics for ``symbol`` from an OHLCV frame.

    Pure function — no I/O — so it can be unit-tested with synthetic data.
    Returns a dict with the Marketto ``RiskSummary`` fields plus the
    window that was actually used. Raises ``RuntimeError`` if Marketto is
    not importable.
    """
    if not _marketto_available():
        raise RuntimeError(_INSTALL_HINT)

    from marketto.processing import risk_summary

    frame = _to_ohlcv_frame(symbol, df)
    windowed = frame.df
    if look_back_days and len(windowed) > look_back_days:
        windowed = windowed.tail(look_back_days)
        from marketto.models import OHLCVFrame

        frame = OHLCVFrame.from_dataframe(symbol, windowed)

    summary = risk_summary(frame, risk_free=risk_free)
    out = summary.to_dict()
    out["look_back_days"] = int(look_back_days)
    return out


def format_risk_metrics(metrics: dict) -> str:
    """Render a metrics dict as a compact markdown block for the LLM."""
    def pct(x: float) -> str:
        return f"{x * 100:+.2f}%" if x == x else "n/a"  # x==x guards NaN

    def num(x: float) -> str:
        return f"{x:+.3f}" if x == x else "n/a"

    rows = metrics.get("rows", 0)
    lines = [
        f"### Realized risk metrics — {metrics.get('symbol', '?')}",
        f"_Window: {metrics.get('start', '?')} → {metrics.get('end', '?')} "
        f"({rows} trading days, look-back {metrics.get('look_back_days', '?')})_",
        "",
        f"- **Annualized return:** {pct(metrics.get('annual_return', float('nan')))}",
        f"- **Annualized volatility:** {pct(metrics.get('annual_volatility', float('nan')))}",
        f"- **Sharpe ratio:** {num(metrics.get('sharpe', float('nan')))}",
        f"- **Max drawdown:** {pct(metrics.get('max_drawdown', float('nan')))}",
        "",
        "_Use these realized figures to ground volatility / probability "
        "estimates — do not invent values that contradict them._",
    ]
    return "\n".join(lines)


def get_risk_metrics(
    symbol: str,
    curr_date: str,
    look_back_days: int = 252,
    *,
    risk_free: float = 0.0,
    loader: Optional[object] = None,
) -> str:
    """Public entry point: realized risk metrics for ``symbol`` as markdown.

    Loads OHLCV via ``stockstats_utils.load_ohlcv`` (cached, filtered to
    ``curr_date`` for look-ahead safety), computes the metrics with
    Marketto, and returns a formatted block. ``loader`` is injectable for
    testing; defaults to the real ``load_ohlcv``.
    """
    if not _marketto_available():
        return _INSTALL_HINT

    if loader is None:
        from agenticwhales.dataflows.stockstats_utils import load_ohlcv as loader  # type: ignore

    try:
        df = loader(symbol, curr_date)
    except Exception as exc:  # network / data errors shouldn't crash the graph
        logger.warning("get_risk_metrics: failed to load OHLCV for %s: %s", symbol, exc)
        return f"Risk metrics unavailable for {symbol}: could not load price data ({exc})."

    if df is None or len(df) < 2:
        return f"Risk metrics unavailable for {symbol}: insufficient price history."

    try:
        metrics = compute_risk_metrics(
            symbol, df, look_back_days=look_back_days, risk_free=risk_free
        )
    except Exception as exc:
        logger.warning("get_risk_metrics: computation failed for %s: %s", symbol, exc)
        return f"Risk metrics unavailable for {symbol}: {exc}."

    return format_risk_metrics(metrics)
