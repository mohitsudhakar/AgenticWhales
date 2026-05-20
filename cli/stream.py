"""`agenticwhales stream ...` — live Alpaca streaming dev aid.

Wraps the same `AlpacaEquityClient` / `AlpacaCryptoClient` used by the
production streaming worker so what works here works in the worker.
Useful for smoke-testing creds + verifying live data flow without
spinning up the full web server.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import List, Sequence

import typer
from rich.console import Console
from rich.live import Live
from rich.table import Table

app = typer.Typer(name="stream", help="Live Alpaca streaming dev aid")
console = Console()


@app.command("test")
def test_cmd(
    ticker: str = typer.Option("AAPL", "--ticker", "-t", help="Symbol(s), comma-separated"),
    seconds: int = typer.Option(30, "--seconds", "-s", min=1, max=600),
    channels: str = typer.Option("trades,bars", "--channels", "-c",
                                  help="Comma-separated: quotes,trades,bars,news"),
    crypto: bool = typer.Option(False, "--crypto", help="Use the crypto WS endpoint instead of equity IEX"),
    max_events: int = typer.Option(100, "--max-events", help="Stop after N events"),
) -> None:
    """Open a live WS connection, print events as they arrive.

    Exits with status 0 if the connection succeeded (even with zero events
    delivered — common when markets are closed or the symbol is quiet).
    Exits 1 on auth failure, 2 on missing creds.
    """
    if not os.getenv("ALPACA_API_KEY_ID") or not os.getenv("ALPACA_API_SECRET_KEY"):
        console.print("[red]ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY not set in env[/]")
        raise typer.Exit(2)

    syms = [s.strip().upper() for s in ticker.split(",") if s.strip()]
    chan_set = tuple(c.strip() for c in channels.split(",") if c.strip())
    asyncio.run(_run(syms, seconds, chan_set, crypto=crypto, max_events=max_events))


async def _run(symbols: Sequence[str], seconds: int, channels: Sequence[str], *,
                crypto: bool, max_events: int) -> None:
    from agenticwhales.streaming import (
        AlpacaAuthError, AlpacaCryptoClient, AlpacaEquityClient, StreamingEvent,
    )

    ClientClass = AlpacaCryptoClient if crypto else AlpacaEquityClient
    client = ClientClass(symbols, channels=channels)
    queue: asyncio.Queue[StreamingEvent] = asyncio.Queue()
    task = asyncio.create_task(client.run(queue))

    started = time.time()
    seen = 0
    table = _make_table(symbols, channels, crypto)
    rows: List[StreamingEvent] = []

    try:
        with Live(_render_table(table, rows, seen, started, seconds), console=console, refresh_per_second=4) as live:
            while seen < max_events and (time.time() - started) < seconds:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=min(1.0, seconds))
                except asyncio.TimeoutError:
                    live.update(_render_table(table, rows, seen, started, seconds))
                    continue
                seen += 1
                rows.append(ev)
                if len(rows) > 20:
                    rows.pop(0)
                live.update(_render_table(table, rows, seen, started, seconds))
    finally:
        client.stop()
        task.cancel()
        try:
            await task
        except AlpacaAuthError as exc:
            console.print(f"[red]Auth failure: {exc}[/]")
            raise typer.Exit(1)
        except (asyncio.CancelledError, Exception):
            pass

    console.print(f"\n[green]Received {seen} event(s) in {int(time.time() - started)}s.[/]")
    if seen == 0:
        console.print("[yellow]No events. Common causes:[/]")
        console.print("  - Market closed for the symbol")
        console.print("  - Paper-tier subscription doesn't grant live IEX data for this symbol")
        console.print("  - Quiet window")


def _make_table(symbols: Sequence[str], channels: Sequence[str], crypto: bool) -> Table:
    t = Table(title=(f"Alpaca {'crypto' if crypto else 'IEX'} stream — "
                     f"{','.join(symbols)} · {','.join(channels)}"))
    t.add_column("#", justify="right", style="dim")
    t.add_column("Kind", style="cyan")
    t.add_column("Symbol")
    t.add_column("Payload")
    return t


def _render_table(_template: Table, rows: List, seen: int, started: float, deadline: int):
    t = Table(title=(f"Streamed {seen} event(s) in {int(time.time() - started)}/{deadline}s"))
    t.add_column("#", justify="right", style="dim")
    t.add_column("Kind", style="cyan")
    t.add_column("Symbol")
    t.add_column("Payload", overflow="fold")
    for i, ev in enumerate(rows, 1):
        payload = ", ".join(f"{k}={v}" for k, v in ev.payload.items())
        t.add_row(str(i), ev.kind.value, ev.symbol, payload)
    return t


if __name__ == "__main__":  # pragma: no cover
    app()
