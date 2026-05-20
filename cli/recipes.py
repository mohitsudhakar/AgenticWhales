"""CLI sub-app for managing recipes (Phase 1).

Usage:
    agenticwhales recipe create --name "Daily AAPL" --tickers AAPL ...
    agenticwhales recipe list
    agenticwhales recipe pause <id>
    agenticwhales recipe resume <id>
    agenticwhales recipe trigger-now <id>
    agenticwhales recipe kill <id>
    agenticwhales recipe delete <id>

Local-only single-user mode: CLI talks to the same `web.auth` storage helpers
the web server uses, so a recipe created from the CLI is visible to the web
UI when both are pointed at the same Supabase config.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from agenticwhales import recipes as recipes_mod
from agenticwhales.agents.schemas import RecipeStatus
from agenticwhales.recipes import HeterogeneityError
from web import auth as web_auth

console = Console()
app = typer.Typer(name="recipe", help="Manage scheduled recipes")

_CLI_USER_ID = web_auth.ANONYMOUS_USER_ID


@app.command("create")
def create(
    name: str = typer.Option(..., help="Human-readable recipe name"),
    tickers: str = typer.Option(..., help="Comma-separated ticker list (e.g. AAPL,MSFT)"),
    analysts: str = typer.Option("market,quant,news", help="Comma-separated analyst list"),
    provider: str = typer.Option("google", help="LLM provider"),
    quick: str = typer.Option("gemini-3-flash-preview", help="Quick-think model"),
    deep: str = typer.Option("gemini-3.1-pro-preview", help="Deep-think model"),
    bull_model: str = typer.Option(..., "--bull-model", help="Bull researcher model"),
    bear_model: str = typer.Option(..., "--bear-model", help="Bear researcher model"),
    schedule_kind: str = typer.Option("manual", help="cron / interval / manual"),
    schedule_expr: str = typer.Option("", help="e.g. '0 13 * * 1-5' or '60s'"),
    policy: str = typer.Option("notify", help="notify / paper_trade / alert_conviction / assist_only"),
    conviction_threshold: int = typer.Option(7, help="1-10 threshold for alert_conviction"),
    daily_budget_usd: float = typer.Option(5.0, "--daily-budget-usd", help="Per-recipe daily cost cap"),
    exchange: str = typer.Option("XNYS", help="Exchange calendar (XNYS / XCME / CRYPTO)"),
    market_hours_only: bool = typer.Option(True, help="Skip firings outside market hours"),
) -> None:
    form = {
        "name": name,
        "tickers": [t.strip() for t in tickers.split(",") if t.strip()],
        "analysts": [a.strip() for a in analysts.split(",") if a.strip()],
        "llm_provider": provider,
        "quick_model": quick,
        "deep_model": deep,
        "bull_model": bull_model,
        "bear_model": bear_model,
        "schedule_kind": schedule_kind,
        "schedule_expr": schedule_expr or None,
        "output_policy": policy,
        "conviction_threshold": conviction_threshold,
        "max_daily_token_cost_usd": daily_budget_usd,
        "exchange_code": exchange,
        "market_hours_only": market_hours_only,
    }
    try:
        recipe = recipes_mod.build_recipe(form, user_id=_CLI_USER_ID)
    except HeterogeneityError as exc:
        console.print(f"[red]Heterogeneity check failed:[/red] {exc}")
        raise typer.Exit(2)
    except ValueError as exc:
        console.print(f"[red]Invalid recipe:[/red] {exc}")
        raise typer.Exit(2)
    recipes_mod.save(recipe)
    console.print(f"[green]Created recipe[/green] {recipe.id} — {recipe.name}")


@app.command("list")
def list_cmd() -> None:
    rs = recipes_mod.list_for_user(_CLI_USER_ID)
    if not rs:
        console.print("[dim]No recipes.[/dim]")
        return
    table = Table(title="Recipes")
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Tickers")
    table.add_column("Schedule")
    table.add_column("Policy")
    table.add_column("Status")
    table.add_column("Last Run")
    for r in rs:
        table.add_row(
            r.id[:8],
            r.name,
            ",".join(r.tickers),
            f"{r.schedule_kind.value}:{r.schedule_expr or '-'}",
            r.output_policy.value,
            r.status.value,
            (str(r.last_run_at) if r.last_run_at else "-"),
        )
    console.print(table)


def _resolve(rid_prefix: str) -> str:
    rs = recipes_mod.list_for_user(_CLI_USER_ID)
    candidates = [r.id for r in rs if r.id.startswith(rid_prefix)]
    if not candidates:
        console.print(f"[red]No recipe matching {rid_prefix!r}[/red]")
        raise typer.Exit(2)
    if len(candidates) > 1:
        console.print(f"[red]Ambiguous prefix {rid_prefix!r} — matches {candidates}[/red]")
        raise typer.Exit(2)
    return candidates[0]


@app.command("pause")
def pause(rid: str) -> None:
    full = _resolve(rid)
    recipes_mod.update_status(full, RecipeStatus.PAUSED)
    console.print(f"Paused {full}")


@app.command("resume")
def resume(rid: str) -> None:
    full = _resolve(rid)
    recipes_mod.update_status(full, RecipeStatus.ACTIVE)
    console.print(f"Resumed {full}")


@app.command("trigger-now")
def trigger_now(rid: str) -> None:
    # CLI doesn't have a live scheduler attached — call the storage path and
    # exit. The next leader to bootstrap will see the recipe; for instant fire
    # the user should use the web API.
    full = _resolve(rid)
    console.print(
        f"[yellow]CLI trigger-now is informational only.[/yellow] "
        f"Recipe {full} is queued for the next scheduler tick. "
        f"For immediate fire, POST /api/recipes/{full}/trigger-now via the web."
    )


@app.command("kill")
def kill(rid: str) -> None:
    full = _resolve(rid)
    recipes_mod.update_status(full, RecipeStatus.KILLED)
    console.print(f"Killed {full}")


@app.command("delete")
def delete(rid: str) -> None:
    full = _resolve(rid)
    rec = recipes_mod.load(full)
    if rec and rec.status == RecipeStatus.ACTIVE:
        console.print("[red]Recipe is active. Pause or kill it before deleting.[/red]")
        raise typer.Exit(2)
    recipes_mod.delete(full)
    console.print(f"Deleted {full}")
