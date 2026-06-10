import logging
from typing import Annotated

from agenticwhales.asof import LookAheadViolation, _parse_date, current_as_of, is_strict

log = logging.getLogger(__name__)

# Position of the date argument in the *args passed to route_to_vendor()
# (i.e. the positional args *after* `method`). 0-indexed.
_DATE_ARG_POS: dict[str, dict] = {
    "get_stock_data":      {"param": "end_date",  "pos": 2},   # (symbol, start_date, end_date)
    "get_indicators":      {"param": "curr_date", "pos": 2},   # (symbol, indicator, curr_date, look_back_days)
    "get_fundamentals":    {"param": "curr_date", "pos": 1},   # (ticker, curr_date)
    "get_balance_sheet":   {"param": "curr_date", "pos": 2},   # (ticker, freq, curr_date)
    "get_cashflow":        {"param": "curr_date", "pos": 2},   # (ticker, freq, curr_date)
    "get_income_statement":{"param": "curr_date", "pos": 2},   # (ticker, freq, curr_date)
    "get_news":            {"param": "end_date",  "pos": 2},   # (ticker, start_date, end_date)
    "get_global_news":     {"param": "curr_date", "pos": 0},   # (curr_date, look_back_days, limit)
}

# Import from vendor-specific modules
from .y_finance import (
    get_YFin_data_online,
    get_stock_stats_indicators_window,
    get_fundamentals as get_yfinance_fundamentals,
    get_balance_sheet as get_yfinance_balance_sheet,
    get_cashflow as get_yfinance_cashflow,
    get_income_statement as get_yfinance_income_statement,
    get_insider_transactions as get_yfinance_insider_transactions,
)
from .yfinance_news import get_news_yfinance, get_global_news_yfinance
from .alpha_vantage import (
    get_stock as get_alpha_vantage_stock,
    get_indicator as get_alpha_vantage_indicator,
    get_fundamentals as get_alpha_vantage_fundamentals,
    get_balance_sheet as get_alpha_vantage_balance_sheet,
    get_cashflow as get_alpha_vantage_cashflow,
    get_income_statement as get_alpha_vantage_income_statement,
    get_insider_transactions as get_alpha_vantage_insider_transactions,
    get_news as get_alpha_vantage_news,
    get_global_news as get_alpha_vantage_global_news,
)
from .alpha_vantage_common import AlphaVantageRateLimitError
from .congress_trades import get_congress_trades
from .x_trades import get_x_trade_recs

# Configuration and routing logic
from .config import get_config

# Tools organized by category
TOOLS_CATEGORIES = {
    "core_stock_apis": {
        "description": "OHLCV stock price data",
        "tools": [
            "get_stock_data"
        ]
    },
    "technical_indicators": {
        "description": "Technical analysis indicators",
        "tools": [
            "get_indicators"
        ]
    },
    "fundamental_data": {
        "description": "Company fundamentals",
        "tools": [
            "get_fundamentals",
            "get_balance_sheet",
            "get_cashflow",
            "get_income_statement"
        ]
    },
    "news_data": {
        "description": "News and insider data",
        "tools": [
            "get_news",
            "get_global_news",
            "get_insider_transactions",
        ]
    },
    "political_data": {
        "description": "Congressional / politician disclosed trades",
        "tools": [
            "get_congress_trades",
        ]
    },
    "x_social": {
        "description": "X (Twitter) user trade recommendations / sentiment",
        "tools": [
            "get_x_trade_recs",
        ]
    }
}

VENDOR_LIST = [
    "yfinance",
    "alpha_vantage",
    "quiverquant",
    "x_api",
]

# Mapping of methods to their vendor-specific implementations
VENDOR_METHODS = {
    # core_stock_apis
    "get_stock_data": {
        "alpha_vantage": get_alpha_vantage_stock,
        "yfinance": get_YFin_data_online,
    },
    # technical_indicators
    "get_indicators": {
        "alpha_vantage": get_alpha_vantage_indicator,
        "yfinance": get_stock_stats_indicators_window,
    },
    # fundamental_data
    "get_fundamentals": {
        "alpha_vantage": get_alpha_vantage_fundamentals,
        "yfinance": get_yfinance_fundamentals,
    },
    "get_balance_sheet": {
        "alpha_vantage": get_alpha_vantage_balance_sheet,
        "yfinance": get_yfinance_balance_sheet,
    },
    "get_cashflow": {
        "alpha_vantage": get_alpha_vantage_cashflow,
        "yfinance": get_yfinance_cashflow,
    },
    "get_income_statement": {
        "alpha_vantage": get_alpha_vantage_income_statement,
        "yfinance": get_yfinance_income_statement,
    },
    # news_data
    "get_news": {
        "alpha_vantage": get_alpha_vantage_news,
        "yfinance": get_news_yfinance,
    },
    "get_global_news": {
        "yfinance": get_global_news_yfinance,
        "alpha_vantage": get_alpha_vantage_global_news,
    },
    "get_insider_transactions": {
        "alpha_vantage": get_alpha_vantage_insider_transactions,
        "yfinance": get_yfinance_insider_transactions,
    },
    # political_data
    "get_congress_trades": {
        "quiverquant": get_congress_trades,
    },
    # x_social
    "get_x_trade_recs": {
        "x_api": get_x_trade_recs,
    },
}

def get_category_for_method(method: str) -> str:
    """Get the category that contains the specified method."""
    for category, info in TOOLS_CATEGORIES.items():
        if method in info["tools"]:
            return category
    raise ValueError(f"Method '{method}' not found in any category")

def get_vendor(category: str, method: str = None) -> str:
    """Get the configured vendor for a data category or specific tool method.
    Tool-level configuration takes precedence over category-level.
    """
    config = get_config()

    # Check tool-level configuration first (if method provided)
    if method:
        tool_vendors = config.get("tool_vendors", {})
        if method in tool_vendors:
            return tool_vendors[method]

    # Fall back to category-level configuration
    return config.get("data_vendors", {}).get(category, "default")

def route_to_vendor(method: str, *args, **kwargs):
    """Route method calls to appropriate vendor implementation with fallback support.

    As-of guard: when called inside a ``with as_of_date(...)`` block, any
    future-dated ``end_date`` / ``curr_date`` argument is silently truncated
    to the as-of date.  In strict mode the requested date must not exceed
    as-of — a ``LookAheadViolation`` is raised instead, surfacing look-ahead
    bugs in the backtest harness rather than silently hiding them.
    """
    # ── as-of guard ──────────────────────────────────────────────────────
    bound = current_as_of()
    if bound is not None and method in _DATE_ARG_POS:
        spec = _DATE_ARG_POS[method]
        param = spec["param"]
        pos = spec["pos"]
        date_raw = (
            kwargs.get(param)
            if param in kwargs
            else (args[pos] if len(args) > pos else None)
        )
        if date_raw is not None:
            date_val = _parse_date(date_raw)
            if date_val is not None and date_val > bound:
                if is_strict():
                    raise LookAheadViolation(
                        f"{method}: {param} {date_val} > as_of {bound} in strict mode "
                        f"(replay acceptance tests require zero look-ahead)"
                    )
                log.debug(
                    "as_of_truncate method=%s %s=%s truncated_to=%s",
                    method, param, date_val, bound,
                )
                args = list(args)
                args[pos] = bound.isoformat()
                args = tuple(args)
    # ── end as-of guard ──────────────────────────────────────────────────

    category = get_category_for_method(method)
    vendor_config = get_vendor(category, method)
    primary_vendors = [v.strip() for v in vendor_config.split(',')]

    if method not in VENDOR_METHODS:
        raise ValueError(f"Method '{method}' not supported")

    # Build fallback chain: primary vendors first, then remaining available vendors
    all_available_vendors = list(VENDOR_METHODS[method].keys())
    fallback_vendors = primary_vendors.copy()
    for vendor in all_available_vendors:
        if vendor not in fallback_vendors:
            fallback_vendors.append(vendor)

    for vendor in fallback_vendors:
        if vendor not in VENDOR_METHODS[method]:
            continue

        vendor_impl = VENDOR_METHODS[method][vendor]
        impl_func = vendor_impl[0] if isinstance(vendor_impl, list) else vendor_impl

        try:
            return impl_func(*args, **kwargs)
        except AlphaVantageRateLimitError:
            continue  # Only rate limits trigger fallback

    raise RuntimeError(f"No available vendor for '{method}'")