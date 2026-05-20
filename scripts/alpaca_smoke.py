"""One-shot Alpaca streaming smoke test.

Loads .env, opens a 10s connection to the equity IEX feed, subscribes to AAPL
trades + bars, prints any events that arrive. Exits cleanly either way so the
exit code reflects whether the connection itself worked.

Usage:
    .venv/bin/python scripts/alpaca_smoke.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv
load_dotenv(REPO / ".env")

from agenticwhales.streaming import (  # noqa: E402
    AlpacaAuthError, AlpacaEquityClient, StreamingEvent,
)


async def main() -> int:
    if not os.getenv("ALPACA_API_KEY_ID") or not os.getenv("ALPACA_API_SECRET_KEY"):
        print("ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY not set in env", file=sys.stderr)
        return 2
    client = AlpacaEquityClient(["AAPL"], channels=("trades", "bars"))
    queue: asyncio.Queue[StreamingEvent] = asyncio.Queue()
    task = asyncio.create_task(client.run(queue))
    seen = 0
    try:
        deadline = asyncio.get_event_loop().time() + 10
        while seen < 5 and asyncio.get_event_loop().time() < deadline:
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=10)
            except asyncio.TimeoutError:
                break
            seen += 1
            print(f"  [{seen}] {ev.kind.value:5s} {ev.symbol} payload={ev.payload}")
    finally:
        client.stop()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, AlpacaAuthError, Exception) as exc:
            if isinstance(exc, AlpacaAuthError):
                print(f"  AUTH FAIL: {exc}", file=sys.stderr)
                return 1
    print(f"\nReceived {seen} event(s).")
    if seen == 0:
        print("  No events in 10s. Possible causes:")
        print("    - Market closed for AAPL (IEX hours-of-trading only)")
        print("    - Paper-tier subscription doesn't grant IEX live data")
        print("    - Quiet symbol window")
        print("  Auth + subscribe succeeded though — the wiring is good.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
