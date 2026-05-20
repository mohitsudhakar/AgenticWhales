"""CLI sub-app for the paper-trading account (Phase 1)."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from agenticwhales import paper as paper_mod
from web import auth as web_auth

console = Console()
app = typer.Typer(name="paper", help="Inspect the paper-trading account")

_CLI_USER_ID = web_auth.ANONYMOUS_USER_ID


def _account_dict() -> dict:
    row = web_auth.load_paper_account(_CLI_USER_ID) or {
        "cash": paper_mod.DEFAULT_STARTING_CASH,
        "starting_cash": paper_mod.DEFAULT_STARTING_CASH,
        "realized_pnl": 0.0,
        "short_collateral_reserved": 0.0,
    }
    return row


def _nav_unrealized(positions, cash: float) -> tuple[float, float]:
    nav = cash
    unrealized = 0.0
    for p in positions:
        qty = float(p["qty"])
        last = float(p["last_price"]) if p.get("last_price") is not None else float(p["avg_cost"])
        avg = float(p["avg_cost"])
        if qty > 0:
            nav += qty * last
            unrealized += (last - avg) * qty
        else:
            nav += (avg - last) * abs(qty) - abs(qty) * avg
            unrealized += (avg - last) * abs(qty)
    return nav, unrealized


@app.command("status")
def status() -> None:
    acct = _account_dict()
    positions = web_auth.list_paper_positions(_CLI_USER_ID)
    nav, unreal = _nav_unrealized(positions, float(acct["cash"]))
    console.print(f"[bold]Paper Account[/bold]")
    console.print(f"  Cash:               ${float(acct['cash']):,.2f}")
    console.print(f"  Starting cash:      ${float(acct.get('starting_cash', 100_000)):,.2f}")
    console.print(f"  Realized PnL:       ${float(acct.get('realized_pnl', 0)):,.2f}")
    console.print(f"  Short collateral:   ${float(acct.get('short_collateral_reserved', 0)):,.2f}")
    console.print(f"  NAV:                ${nav:,.2f}")
    console.print(f"  Unrealized PnL:     ${unreal:,.2f}")
    console.print(f"  Open positions:     {len(positions)}")


@app.command("positions")
def positions() -> None:
    rows = web_auth.list_paper_positions(_CLI_USER_ID)
    if not rows:
        console.print("[dim]No positions.[/dim]")
        return
    table = Table(title="Paper Positions")
    table.add_column("Ticker", style="cyan")
    table.add_column("Qty", justify="right")
    table.add_column("Avg Cost", justify="right")
    table.add_column("Last Price", justify="right")
    table.add_column("MTM PnL", justify="right")
    for p in rows:
        qty = float(p["qty"])
        avg = float(p["avg_cost"])
        last = float(p["last_price"]) if p.get("last_price") is not None else None
        mtm = (last - avg) * qty if last is not None and qty > 0 else (
            (avg - last) * abs(qty) if last is not None else 0.0
        )
        table.add_row(
            p["ticker"],
            f"{qty:g}",
            f"${avg:,.2f}",
            f"${last:,.2f}" if last is not None else "-",
            f"${mtm:,.2f}" if last is not None else "-",
        )
    console.print(table)


@app.command("orders")
def orders(limit: int = typer.Option(20, "--limit", "-n", help="Max orders to show")) -> None:
    rows = web_auth.list_paper_orders(_CLI_USER_ID, limit=limit)
    if not rows:
        console.print("[dim]No orders.[/dim]")
        return
    table = Table(title=f"Last {len(rows)} paper orders")
    table.add_column("Time")
    table.add_column("Ticker", style="cyan")
    table.add_column("Side")
    table.add_column("Qty", justify="right")
    table.add_column("Fill", justify="right")
    table.add_column("Rating")
    table.add_column("Conviction", justify="right")
    table.add_column("Status")
    for r in rows:
        table.add_row(
            str(r.get("created_at", ""))[:19],
            r["ticker"],
            r["side"],
            f"{float(r['qty']):g}",
            f"${float(r['fill_price']):,.2f}",
            r.get("pm_rating", "-"),
            str(r.get("conviction_score") or "-"),
            r["status"],
        )
    console.print(table)


@app.command("risk-events")
def risk_events(limit: int = typer.Option(20, "--limit", "-n", help="Max events to show")) -> None:
    rows = web_auth.list_risk_events(_CLI_USER_ID, limit=limit)
    if not rows:
        console.print("[dim]No risk events.[/dim]")
        return
    table = Table(title=f"Last {len(rows)} risk events")
    table.add_column("Time")
    table.add_column("Rule", style="yellow")
    table.add_column("Ticker")
    table.add_column("Details")
    for r in rows:
        table.add_row(
            str(r.get("created_at", ""))[:19],
            r["rule"],
            r.get("ticker") or "-",
            str(r.get("details", "")),
        )
    console.print(table)


@app.command("kill-switch")
def kill_switch(state: str = typer.Argument(..., help="on or off")) -> None:
    enabled = state.lower() in ("on", "true", "1", "yes")
    web_auth.upsert_risk_limits(_CLI_USER_ID, global_kill_switch=enabled)
    console.print(f"Global kill switch: [{'red' if enabled else 'green'}]{'ON' if enabled else 'OFF'}[/]")
