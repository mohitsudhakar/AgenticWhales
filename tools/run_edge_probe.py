#!/usr/bin/env python
"""Run the cheap edge probe against a real LLM (DeepSeek by default).

The look-ahead-proof reduced-debate backtest: see ``agenticwhales/edge_probe.py``.
Decisions are cached per (symbol, date, features, model), so the run is resumable
— re-invoking picks up where it left off and re-pays nothing.

Examples
--------
    # 1-symbol smoke against the real LLM (fast sanity check before the panel):
    python tools/run_edge_probe.py --smoke

    # full panel, both regime windows:
    python tools/run_edge_probe.py

    # subset:
    python tools/run_edge_probe.py --symbols AAPL,GLD,SPY --windows w1
"""

from __future__ import annotations

import argparse
import datetime as _dt
import logging
import sys
from pathlib import Path

import pandas as pd

# Load .env so DEEPSEEK_API_KEY (and friends) are available.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # noqa: BLE001
    pass

from agenticwhales import edge_probe as ep

log = logging.getLogger("edge_probe.run")

WINDOWS = {"w1": ep.W1_DRAWDOWN, "w2": ep.W2_BULL_OOS}


def load_history(symbol: str, window: ep.Window) -> pd.DataFrame:
    """Fetch OHLCV with ~400 calendar days of warmup before the window start.

    The warmup gives the feature builder a full year of history (52w range,
    SMA200) on the very first schedule date.
    """
    import yfinance as yf

    start = _dt.date.fromisoformat(window.start) - _dt.timedelta(days=400)
    end = _dt.date.fromisoformat(window.end)
    df = yf.Ticker(symbol.upper()).history(
        start=start.isoformat(), end=(end + _dt.timedelta(days=1)).isoformat())
    if df.empty:
        raise RuntimeError(f"no OHLCV for {symbol} in {start}..{end}")
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    return df


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbols", default=",".join(ep.DEFAULT_PANEL),
                   help="comma-separated tickers (default: the 10-name panel)")
    p.add_argument("--windows", default="w1,w2", help="w1, w2, or w1,w2")
    p.add_argument("--model", default="deepseek-v4-pro")
    p.add_argument("--provider", default="deepseek")
    p.add_argument("--smoke", action="store_true",
                   help="1 symbol (AAPL), 1 window (w1) — quick real-LLM sanity check")
    p.add_argument("--cache", default=str(Path.home() / ".tradingagents" / "edge_probe_cache.jsonl"))
    p.add_argument("--out", default=None, help="report path (default: docs/reviews/<date>-edge-probe.md)")
    p.add_argument("--random-seeds", type=int, default=20)
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.smoke:
        symbols = ["AAPL"]
        windows = [WINDOWS["w1"]]
    else:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        windows = [WINDOWS[w.strip()] for w in args.windows.split(",") if w.strip()]

    log.info("edge probe: %d symbols × %d windows · model=%s",
             len(symbols), len(windows), args.model)

    invoke_text, invoke_structured = ep.default_invokers(
        provider=args.provider, model=args.model)
    cache = ep.DecisionCache(Path(args.cache), model=args.model)
    seeds = tuple(range(args.random_seeds))

    window_results = []
    for window in windows:
        log.info("=== window %s (%s..%s) ===", window.name, window.start, window.end)
        per_symbol = []
        for symbol in symbols:
            try:
                history = load_history(symbol, window)
            except Exception as exc:  # noqa: BLE001
                log.warning("skip %s/%s: %s", symbol, window.name, exc)
                continue
            log.info("  %s …", symbol)
            r = ep.run_symbol(
                symbol, window, history,
                invoke_text=invoke_text, invoke_structured=invoke_structured,
                cache=cache, random_seeds=seeds)
            per_symbol.append(r)
            if "llm" in r:
                log.info("    LLM Sharpe=%s  random=%s  classical=%s  buy&hold=%s  (n=%s)",
                         r["llm"]["sharpe"], r["random"]["sharpe"],
                         r["classical"]["sharpe"], r["buy_hold"]["sharpe"], r["n_decisions"])

        def med(strat, metric="sharpe"):
            xs = [r[strat][metric] for r in per_symbol if strat in r]
            return round(ep._median(xs), 3)

        window_results.append({
            "window": window.name, "window_note": window.note,
            "n_symbols": len(per_symbol),
            "median_sharpe": {s: med(s) for s in ("llm", "random", "classical", "buy_hold")},
            "median_total_return_pct": {
                s: med(s, "total_return_pct") for s in ("llm", "random", "classical", "buy_hold")},
            "per_symbol": per_symbol,
        })

    verdict = ep.evaluate_verdict(window_results)
    log.info("VERDICT: %s — %s", verdict["verdict"], verdict["reason"])

    generated = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    out = Path(args.out) if args.out else Path("docs/reviews") / (
        f"{_dt.date.today().isoformat()}-edge-probe{'-smoke' if args.smoke else ''}.md")
    path = ep.write_report(window_results, verdict, out, model=args.model, generated=generated)
    log.info("report → %s", path)
    print(f"\nVERDICT: {verdict['verdict']}\n{verdict['reason']}\nreport: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
