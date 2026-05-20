"""CLI sub-app for inspecting LLM spend (Phase 1)."""

from __future__ import annotations

from datetime import datetime, timezone

import typer
from rich.console import Console
from rich.table import Table

from web import auth as web_auth

console = Console()
app = typer.Typer(name="cost", help="Inspect LLM spend totals")

_CLI_USER_ID = web_auth.ANONYMOUS_USER_ID


@app.command("today")
def today() -> None:
    today_str = datetime.now(tz=timezone.utc).date().isoformat()
    total = web_auth.load_user_spend(_CLI_USER_ID, today_str)
    limits = web_auth.load_risk_limits(_CLI_USER_ID) or web_auth._default_risk_limits_row(_CLI_USER_ID)
    cap = float(limits.get("daily_spend_cap_usd", 25.0))
    used_pct = (total / cap * 100) if cap > 0 else 0
    color = "red" if used_pct > 80 else "yellow" if used_pct > 50 else "green"
    console.print(f"[bold]Spend today ({today_str}):[/bold] [{color}]${total:.4f}[/{color}] / ${cap:.2f} cap ({used_pct:.1f}%)")


@app.command("by-recipe")
def by_recipe() -> None:
    """Show per-recipe spend for today."""
    today_str = datetime.now(tz=timezone.utc).date().isoformat()
    # No direct list-by-user; walk the memstore / Postgres view manually.
    rows = []
    if web_auth._db_writable():
        rows = web_auth._select_columns(
            "recipe_usage",
            filters={"user_id": _CLI_USER_ID, "usage_date": today_str},
        )
    else:
        for (table, _), row in web_auth._memstore.items():
            if table != "recipe_usage":
                continue
            if row.get("user_id") != _CLI_USER_ID:
                continue
            if row.get("usage_date") != today_str:
                continue
            rows.append(row)

    if not rows:
        console.print(f"[dim]No spend recorded for {today_str}.[/dim]")
        return

    table = Table(title=f"Recipe spend on {today_str}")
    table.add_column("Recipe ID", style="cyan")
    table.add_column("Runs", justify="right")
    table.add_column("Failures", justify="right")
    table.add_column("Input tokens", justify="right")
    table.add_column("Output tokens", justify="right")
    table.add_column("Cost (USD)", justify="right")
    for r in rows:
        table.add_row(
            (r.get("recipe_id") or "-")[:12],
            str(r.get("run_count", 0)),
            str(r.get("failure_count", 0)),
            f"{int(r.get('input_tokens', 0)):,}",
            f"{int(r.get('output_tokens', 0)):,}",
            f"${float(r.get('token_cost_usd', 0)):.4f}",
        )
    console.print(table)


@app.command("month")
def month() -> None:
    """Aggregate spend for the current UTC month."""
    now = datetime.now(tz=timezone.utc)
    prefix = now.strftime("%Y-%m")
    total = 0.0
    if web_auth._db_writable():
        # PostgREST: filter by usage_date >= first of month.
        first = f"{prefix}-01"
        rows = web_auth._select_columns(
            "user_spend_daily",
            filters={"user_id": _CLI_USER_ID},
        )
        total = sum(float(r.get("total_cost_usd", 0)) for r in rows
                    if str(r.get("usage_date", "")) >= first)
    else:
        for (table, _), row in web_auth._memstore.items():
            if table != "user_spend_daily":
                continue
            if row.get("user_id") != _CLI_USER_ID:
                continue
            if str(row.get("usage_date", "")).startswith(prefix):
                total += float(row.get("total_cost_usd", 0))

    limits = web_auth.load_risk_limits(_CLI_USER_ID) or web_auth._default_risk_limits_row(_CLI_USER_ID)
    cap = float(limits.get("monthly_spend_cap_usd", 500.0))
    used_pct = (total / cap * 100) if cap > 0 else 0
    color = "red" if used_pct > 80 else "yellow" if used_pct > 50 else "green"
    console.print(f"[bold]Spend MTD ({prefix}):[/bold] [{color}]${total:.2f}[/{color}] / ${cap:.2f} cap ({used_pct:.1f}%)")
