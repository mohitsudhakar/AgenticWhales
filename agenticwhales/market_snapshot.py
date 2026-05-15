"""Authoritative latest-price snapshot used to anchor agent prompts.

Why this exists: the LLM occasionally hallucinates the current price of an
instrument despite receiving correct OHLCV data via tools. Injecting a
pre-formatted snapshot block at the top of each position-aware prompt gives
the model a hard fact it must cite verbatim and cannot drift away from.

The snapshot is computed once per run and lives on the graph state so every
downstream agent sees the same anchor.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import yfinance as yf


def fetch_snapshot_block(symbol: str, trade_date: Optional[str] = None) -> str:
    """Return a directive prompt block with the latest close + recent range.

    Empty string when no data is available (so callers can do a single
    truthiness check before splicing the block in).
    """
    sym = (symbol or "").strip()
    if not sym:
        return ""
    try:
        end = datetime.strptime(trade_date, "%Y-%m-%d") if trade_date else datetime.utcnow()
    except (TypeError, ValueError):
        end = datetime.utcnow()
    # yfinance uses an exclusive end; bump by a day so the trade date itself
    # is included if it has data.
    end_excl = end + timedelta(days=1)
    start = end - timedelta(days=15)

    try:
        df = yf.Ticker(sym.upper()).history(
            start=start.strftime("%Y-%m-%d"),
            end=end_excl.strftime("%Y-%m-%d"),
            auto_adjust=False,
        )
    except Exception:
        return ""
    if df is None or df.empty:
        return ""

    df = df.dropna(subset=["Close"])
    if df.empty:
        return ""

    last_row = df.iloc[-1]
    last_date = df.index[-1].strftime("%Y-%m-%d")
    last_close = float(last_row["Close"])
    last_open = float(last_row.get("Open", last_close))
    last_high = float(last_row.get("High", last_close))
    last_low = float(last_row.get("Low", last_close))

    window = df.tail(5)
    win_high = float(window["High"].max())
    win_low = float(window["Low"].min())

    # Decimal precision: more for sub-$1, normal for typical equities.
    fmt = "{:.4f}" if last_close < 1 else "{:.2f}"

    lines = [
        "════════ LATEST MARKET DATA (authoritative) ════════",
        f"Symbol: {sym.upper()}    Last close date: {last_date}",
        f"Last close: {fmt.format(last_close)}",
        f"Last day OHLC: O {fmt.format(last_open)} / H {fmt.format(last_high)} / L {fmt.format(last_low)} / C {fmt.format(last_close)}",
        f"5-day range: {fmt.format(win_low)} – {fmt.format(win_high)}",
        "",
        "Cite the **Last close** above when stating the current price. Do NOT invent a different price. ",
        "If your reasoning depends on a price level not consistent with this data, flag the discrepancy explicitly.",
        "════════════════════════════════════════════════════",
    ]
    return "\n".join(lines)
