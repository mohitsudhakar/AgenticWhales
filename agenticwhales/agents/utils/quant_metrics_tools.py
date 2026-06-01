from typing import Annotated

from langchain_core.tools import tool

from agenticwhales.dataflows.quant_metrics import get_risk_metrics as _get_risk_metrics


@tool
def get_risk_metrics(
    symbol: Annotated[str, "ticker symbol of the company"],
    curr_date: Annotated[str, "The current trading date you are trading on, YYYY-mm-dd"],
    look_back_days: Annotated[int, "trailing window of trading days to measure over"] = 252,
) -> str:
    """
    Retrieve REALIZED risk metrics for a ticker: annualized return,
    annualized volatility, Sharpe ratio, and maximum drawdown, computed
    from adjusted-close history up to curr_date.

    Use this to ground volatility and probability-of-profit estimates in
    actual historical statistics rather than intuition. The metrics are
    computed on the same look-ahead-safe price series the other analysts
    see (no future data).

    Args:
        symbol (str): Ticker symbol, e.g. AAPL, TSM
        curr_date (str): Current trading date, YYYY-mm-dd
        look_back_days (int): Trailing window in trading days (default 252 ≈ 1 year)
    Returns:
        str: A markdown block of realized risk metrics for the ticker.
    """
    return _get_risk_metrics(symbol, curr_date, look_back_days)
