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


@tool
def get_congress_trades(
    ticker: Annotated[str, "Ticker symbol"],
    limit: Annotated[int, "Max number of disclosed trades to return"] = 50,
) -> str:
    """
    Retrieve disclosed U.S. congressional (House/Senate) stock trades for a
    ticker. Uses the configured political_data vendor. Output is wrapped in
    <external_data> tags so the analyst prompt treats the body as untrusted
    data, not instructions (see agenticwhales.provenance).
    Args:
        ticker (str): Ticker symbol of the company
        limit (int): Max number of disclosed trades to return (default 50)
    Returns:
        str: A report of disclosed congressional trades, provenance-wrapped
    """
    raw = route_to_vendor("get_congress_trades", ticker, limit)
    return wrap_external(
        raw,
        source="congress_vendor",
        kind="congress_trades",
        ticker=ticker,
        limit=limit,
    )


@tool
def get_x_trade_recs(
    username: Annotated[str, "X (Twitter) handle, with or without a leading @"],
    max_results: Annotated[int, "Max recent tweets to scan"] = 50,
) -> str:
    """
    Retrieve structured trade recommendations extracted from an X (Twitter)
    account's recent posts. Fetches the user's recent tweets via the official
    X API v2, then uses an LLM to extract (ticker, action buy/sell/hold,
    conviction 0-1, rationale, timeframe). Uses the configured x_social vendor.
    Output is wrapped in <external_data> tags so the analyst prompt treats the
    body as untrusted data, not instructions (see agenticwhales.provenance) —
    social posts are self-reported opinion, not advice.
    Args:
        username (str): X handle (with or without a leading @)
        max_results (int): Max recent tweets to scan (default 50)
    Returns:
        str: A report of extracted trade recommendations, provenance-wrapped
    """
    raw = route_to_vendor("get_x_trade_recs", username, max_results)
    return wrap_external(
        raw,
        source="x_vendor",
        kind="x_trade_recs",
        username=username,
        max_results=max_results,
    )
