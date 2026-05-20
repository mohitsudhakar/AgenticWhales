"""`agenticwhales backtest ...` — replay a ticker through the stub decision engine.

Phase 3 v1: stub mode only. Live mode (real LLM calls) is wired later once the
streaming worker lands — that's the natural place to share the runner code.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from agenticwhales.backtest import BacktestResult, run_backtest

app = typer.Typer(name="backtest", help="Replay a ticker through the backtest engine")
console = Console()


def _parse_date(value: str) -> _dt.date:
    try:
        return _dt.date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"date must be YYYY-MM-DD, got '{value}'") from exc


@app.command("run")
def run_cmd(
    ticker: str = typer.Argument(..., help="Ticker symbol, e.g. AAPL"),
    from_date: str = typer.Option(..., "--from", help="Start date YYYY-MM-DD"),
    to_date: str = typer.Option(..., "--to", help="End date YYYY-MM-DD"),
    starting_cash: float = typer.Option(100_000.0, "--cash"),
    kelly_cap: float = typer.Option(0.10, "--kelly-cap"),
    output_json: Optional[Path] = typer.Option(
        None, "--out", help="Write full result JSON to this path"
    ),
    mode: str = typer.Option("stub", "--mode", help="stub (default) | live (deferred)"),
) -> None:
    """Run a backtest for one symbol over the given window."""
    if mode != "stub":
        console.print(f"[yellow]mode={mode!r} not yet implemented; falling back to 'stub'.[/]")
    fd = _parse_date(from_date)
    td = _parse_date(to_date)

    with console.status(f"[bold green]Replaying {ticker} {fd}..{td}…"):
        try:
            result = run_backtest(
                ticker.upper(), fd, td,
                starting_cash=starting_cash,
                kelly_cap=kelly_cap,
            )
        except Exception as exc:
            console.print(f"[red]backtest failed: {exc}[/]")
            raise typer.Exit(1)

    _print_summary(result)
    if output_json:
        _write_json(result, output_json)
        console.print(f"[dim]Wrote full result → {output_json}[/]")


def _print_summary(result: BacktestResult) -> None:
    growth_pct = (result.final_nav - result.starting_cash) / result.starting_cash * 100.0
    t = Table(title=f"Backtest: {result.symbol} ({result.from_date} → {result.to_date})")
    t.add_column("Metric", style="cyan")
    t.add_column("Value", justify="right")
    t.add_row("Starting cash", f"${result.starting_cash:,.2f}")
    t.add_row("Final NAV", f"${result.final_nav:,.2f}")
    t.add_row("Growth", f"{growth_pct:+.2f}%")
    t.add_row("Decisions made", str(result.total_decisions))
    t.add_row("Trades closed", str(result.closed_trades))
    t.add_row("Hit rate", f"{result.hit_rate:.2%}")
    t.add_row("Brier", f"{result.brier:.4f}")
    t.add_row("Max drawdown", f"{result.max_drawdown_pct:.2%}")
    console.print(t)

    if result.trades:
        tt = Table(title="Trades (most recent 10)")
        for col in ["entry", "exit", "qty", "entry $", "exit $", "ret %", "reason"]:
            tt.add_column(col)
        for trade in result.trades[-10:]:
            tt.add_row(
                trade["entry_date"], trade["exit_date"],
                f"{trade['qty']:.2f}",
                f"{trade['entry_price']:.2f}", f"{trade['exit_price']:.2f}",
                f"{trade['realized_return_pct']:+.2f}",
                trade["reason"],
            )
        console.print(tt)


def _write_json(result: BacktestResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "symbol": result.symbol,
        "from_date": result.from_date.isoformat(),
        "to_date": result.to_date.isoformat(),
        "starting_cash": result.starting_cash,
        "final_nav": result.final_nav,
        "total_decisions": result.total_decisions,
        "closed_trades": result.closed_trades,
        "hit_rate": result.hit_rate,
        "brier": result.brier,
        "max_drawdown_pct": result.max_drawdown_pct,
        "equity_curve": result.equity_curve,
        "trades": result.trades,
    }, indent=2))


if __name__ == "__main__":  # pragma: no cover
    app()
