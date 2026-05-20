"""Alpaca-backed streaming clients for Phase 3.

Two WebSocket clients on the same protocol shape:
  * `AlpacaEquityClient` — uses Alpaca's IEX feed (free tier; 15-min-delayed
    for unentitled accounts, real-time for SIP entitlement). URL:
    `wss://stream.data.alpaca.markets/v2/iex`
  * `AlpacaCryptoClient` — Alpaca crypto US: `wss://stream.data.alpaca.markets/v1beta3/crypto/us`

Both share `_AlpacaWSClient` which handles:
  - Connect + auth handshake (KEY/SECRET via env)
  - Subscribe to quote/trade/news channels for a symbol set
  - Normalize Alpaca's `T` payload codes (`q`/`t`/`b`/`n` etc.) into typed
    `StreamingEvent`s pushed onto a bounded `asyncio.Queue`
  - Auto-reconnect with exponential backoff (capped at 60s)
  - Heartbeat detection: if no message for `heartbeat_timeout_s` we force a
    reconnect

The trigger engine (`agenticwhales.triggers`) consumes these events from the
queue and decides whether to fire any recipe. That wiring lives in
`web.streaming_worker`.

Test surface: `InMemoryStreamClient` produces a fixed event sequence without
touching the network — used to wire the streaming worker tests deterministically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional, Sequence

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event envelope
# ---------------------------------------------------------------------------


class EventKind(str, Enum):
    QUOTE = "quote"
    TRADE = "trade"
    BAR = "bar"
    NEWS = "news"


@dataclass(frozen=True)
class StreamingEvent:
    source: str                       # "alpaca-equity" | "alpaca-crypto" | "stub"
    kind: EventKind
    symbol: str
    received_at: float                # unix ts
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_jsonable(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "kind": self.kind.value,
            "symbol": self.symbol,
            "received_at": self.received_at,
            "payload": self.payload,
        }


# ---------------------------------------------------------------------------
# Client protocol
# ---------------------------------------------------------------------------


class StreamClient:
    """Minimal interface every streaming client implements."""

    source_name: str = "unknown"

    async def run(self, queue: "asyncio.Queue[StreamingEvent]") -> None:
        raise NotImplementedError

    async def update_subscriptions(self, symbols: Iterable[str]) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# In-memory client — deterministic for tests
# ---------------------------------------------------------------------------


class InMemoryStreamClient(StreamClient):
    """Plays a fixed sequence of events into the queue, then idles.

    Use for streaming-worker tests. `playback_interval_s` lets tests slow down
    or speed up the playback rate.
    """

    source_name = "stub"

    def __init__(self, events: Sequence[StreamingEvent], playback_interval_s: float = 0.0):
        self._events = list(events)
        self._interval = playback_interval_s
        self._subscriptions: List[str] = []
        self._stop = asyncio.Event()

    async def run(self, queue: "asyncio.Queue[StreamingEvent]") -> None:
        for ev in self._events:
            if self._stop.is_set():
                return
            await queue.put(ev)
            if self._interval:
                await asyncio.sleep(self._interval)
        # Idle until stop. Don't busy-loop.
        await self._stop.wait()

    async def update_subscriptions(self, symbols: Iterable[str]) -> None:
        self._subscriptions = list(symbols)

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
# Alpaca WS clients
# ---------------------------------------------------------------------------


_DEFAULT_HEARTBEAT_S = 30.0
_DEFAULT_BACKOFF_S = 1.0
_MAX_BACKOFF_S = 60.0


class AlpacaAuthError(RuntimeError):
    """Raised when Alpaca's auth handshake fails (bad/missing key)."""


class _AlpacaWSClient(StreamClient):
    """Shared WS impl for the equity and crypto endpoints.

    Constructor signature is identical; the subclasses fix `endpoint_url` and
    `source_name`.
    """

    endpoint_url: str = ""

    def __init__(
        self,
        symbols: Iterable[str],
        *,
        key_id: Optional[str] = None,
        secret_key: Optional[str] = None,
        channels: Sequence[str] = ("quotes", "trades"),
        heartbeat_timeout_s: float = _DEFAULT_HEARTBEAT_S,
    ):
        self._symbols = sorted({s.upper().strip() for s in symbols if s})
        self._channels = list(channels)
        self._key_id = key_id or os.getenv("ALPACA_API_KEY_ID") or ""
        self._secret = secret_key or os.getenv("ALPACA_API_SECRET_KEY") or ""
        self._heartbeat = heartbeat_timeout_s
        self._stop = asyncio.Event()
        self._ws = None  # current connection, for resubscribe

    def stop(self) -> None:
        self._stop.set()

    async def update_subscriptions(self, symbols: Iterable[str]) -> None:
        new = sorted({s.upper().strip() for s in symbols if s})
        if new == self._symbols:
            return
        self._symbols = new
        if self._ws is not None:
            await self._send_subscribe(self._ws)

    async def run(self, queue: "asyncio.Queue[StreamingEvent]") -> None:
        if not self._key_id or not self._secret:
            raise AlpacaAuthError(
                "ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY not set; cannot stream"
            )
        try:
            import websockets  # noqa: F401 lazy
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("`websockets` package required for streaming") from exc

        backoff = _DEFAULT_BACKOFF_S
        while not self._stop.is_set():
            try:
                await self._connect_and_consume(queue)
                backoff = _DEFAULT_BACKOFF_S
            except AlpacaAuthError:
                # Auth errors are fatal — don't burn retries.
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("%s ws loop error: %s; reconnecting in %.1fs",
                            self.source_name, exc, backoff)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                    return
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, _MAX_BACKOFF_S)

    async def _connect_and_consume(self, queue: "asyncio.Queue[StreamingEvent]") -> None:
        import websockets

        async with websockets.connect(self.endpoint_url, ping_interval=20) as ws:
            self._ws = ws
            # 1) auth
            await ws.send(json.dumps({"action": "auth", "key": self._key_id, "secret": self._secret}))
            msg = json.loads(await ws.recv())
            if not _alpaca_status_ok(msg):
                raise AlpacaAuthError(f"alpaca auth failed: {msg}")

            # 2) subscribe
            await self._send_subscribe(ws)
            ack = json.loads(await ws.recv())
            log.info("%s subscribed: %s", self.source_name, ack)

            # 3) consume with heartbeat
            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=self._heartbeat)
                except asyncio.TimeoutError:
                    log.info("%s heartbeat expired; forcing reconnect", self.source_name)
                    return
                for event in _normalize_alpaca_message(raw, self.source_name):
                    await queue.put(event)

    async def _send_subscribe(self, ws) -> None:
        payload: Dict[str, List[str]] = {"action": "subscribe"}
        for ch in self._channels:
            payload[ch] = list(self._symbols)
        await ws.send(json.dumps(payload))


class AlpacaEquityClient(_AlpacaWSClient):
    endpoint_url = "wss://stream.data.alpaca.markets/v2/iex"
    source_name = "alpaca-equity"


class AlpacaCryptoClient(_AlpacaWSClient):
    endpoint_url = "wss://stream.data.alpaca.markets/v1beta3/crypto/us"
    source_name = "alpaca-crypto"


# ---------------------------------------------------------------------------
# Message normalization (pure — tested directly)
# ---------------------------------------------------------------------------


def _alpaca_status_ok(msg: Any) -> bool:
    """Alpaca sometimes returns a list, sometimes a dict; auth OK = T:success."""
    items = msg if isinstance(msg, list) else [msg]
    for item in items:
        if isinstance(item, dict) and item.get("T") == "error":
            return False
    return True


def _normalize_alpaca_message(raw: str, source: str) -> List[StreamingEvent]:
    """Parse one Alpaca WS frame and return zero or more typed events.

    Alpaca sends arrays of payloads. Each entry has a `T` (type) discriminator:
      `q` quote, `t` trade, `b` minute-bar, `n` news, `subscription`/`success`/`error` control.
    """
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return []
    items = payload if isinstance(payload, list) else [payload]
    out: List[StreamingEvent] = []
    now = time.time()
    for item in items:
        if not isinstance(item, dict):
            continue
        kind_code = item.get("T")
        sym = item.get("S") or item.get("symbol") or ""
        if not sym:
            continue
        if kind_code == "q":
            out.append(StreamingEvent(source=source, kind=EventKind.QUOTE, symbol=sym,
                                       received_at=now,
                                       payload={"bid": item.get("bp"), "ask": item.get("ap"),
                                                "bid_size": item.get("bs"), "ask_size": item.get("as")}))
        elif kind_code == "t":
            out.append(StreamingEvent(source=source, kind=EventKind.TRADE, symbol=sym,
                                       received_at=now,
                                       payload={"price": item.get("p"), "size": item.get("s")}))
        elif kind_code == "b":
            out.append(StreamingEvent(source=source, kind=EventKind.BAR, symbol=sym,
                                       received_at=now,
                                       payload={"open": item.get("o"), "high": item.get("h"),
                                                "low": item.get("l"), "close": item.get("c"),
                                                "volume": item.get("v")}))
        elif kind_code == "n":
            out.append(StreamingEvent(source=source, kind=EventKind.NEWS, symbol=sym,
                                       received_at=now,
                                       payload={"headline": item.get("headline"),
                                                "summary": item.get("summary"),
                                                "url": item.get("url")}))
    return out
