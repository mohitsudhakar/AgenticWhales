from langchain_core.tools import tool
from typing import Annotated

from agenticwhales.dataflows.interface import route_to_vendor
from agenticwhales.provenance import wrap_external


@tool
def get_news(
    ticker: Annotated[str, "Ticker symbol"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """
    Retrieve news data for a given ticker symbol.
    Uses the configured news_data vendor. Output is wrapped in
    <external_data> tags so the analyst prompt can treat the body as
    untrusted data rather than instructions (see agenticwhales.provenance).
    Args:
        ticker (str): Ticker symbol
        start_date (str): Start date in yyyy-mm-dd format
        end_date (str): End date in yyyy-mm-dd format
    Returns:
        str: A formatted, provenance-wrapped string containing news data
    """
    raw = route_to_vendor("get_news", ticker, start_date, end_date)
    return wrap_external(
        raw,
        source="news_vendor",
        kind="company_news",
        ticker=ticker,
        start_date=start_date,
        end_date=end_date,
    )


@tool
def get_global_news(
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format"],
    look_back_days: Annotated[int, "Number of days to look back"] = 7,
    limit: Annotated[int, "Maximum number of articles to return"] = 5,
) -> str:
    """
    Retrieve global news data.
    Uses the configured news_data vendor. Output is wrapped in
    <external_data> tags (see agenticwhales.provenance).
    Args:
        curr_date (str): Current date in yyyy-mm-dd format
        look_back_days (int): Number of days to look back (default 7)
        limit (int): Maximum number of articles to return (default 5)
    Returns:
        str: A formatted, provenance-wrapped string containing global news data
    """
    raw = route_to_vendor("get_global_news", curr_date, look_back_days, limit)
    return wrap_external(
        raw,
        source="news_vendor",
        kind="global_news",
        curr_date=curr_date,
        look_back_days=look_back_days,
    )


@tool
def get_insider_transactions(
    ticker: Annotated[str, "ticker symbol"],
) -> str:
    """
    Retrieve insider transaction information about a company.
    Uses the configured news_data vendor. Output is wrapped in
    <external_data> tags (see agenticwhales.provenance).
    Args:
        ticker (str): Ticker symbol of the company
    Returns:
        str: A report of insider transaction data, provenance-wrapped
    """
    raw = route_to_vendor("get_insider_transactions", ticker)
    return wrap_external(
        raw,
        source="news_vendor",
        kind="insider_transactions",
        ticker=ticker,
    )
