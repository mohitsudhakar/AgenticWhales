"""Tests for streaming module — message normalization + in-memory client.

Live Alpaca WS connect/auth/reconnect logic is not unit-tested here. That
behavior is verified via the streaming-worker integration test once the worker
lands. Here we cover the deterministic pieces."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from agenticwhales.streaming import (
    AlpacaAuthError,
    AlpacaEquityClient,
    EventKind,
    InMemoryStreamClient,
    StreamingEvent,
    _alpaca_status_ok,
    _normalize_alpaca_message,
)


class TestNormalization:
    def test_empty_payload(self):
        assert _normalize_alpaca_message("", "alpaca-equity") == []
        assert _normalize_alpaca_message("not-json", "alpaca-equity") == []

    def test_quote(self):
        raw = json.dumps([{
            "T": "q", "S": "AAPL", "bp": 180.5, "ap": 180.6, "bs": 100, "as": 200,
        }])
        events = _normalize_alpaca_message(raw, "alpaca-equity")
        assert len(events) == 1
        assert events[0].kind == EventKind.QUOTE
        assert events[0].symbol == "AAPL"
        assert events[0].payload["bid"] == 180.5
        assert events[0].payload["ask"] == 180.6

    def test_trade(self):
        raw = json.dumps([{"T": "t", "S": "AAPL", "p": 180.55, "s": 50}])
        events = _normalize_alpaca_message(raw, "alpaca-equity")
        assert events[0].kind == EventKind.TRADE
        assert events[0].payload["price"] == 180.55

    def test_bar(self):
        raw = json.dumps([{"T": "b", "S": "AAPL", "o": 180, "h": 182, "l": 179, "c": 181, "v": 1000}])
        events = _normalize_alpaca_message(raw, "alpaca-equity")
        assert events[0].kind == EventKind.BAR
        assert events[0].payload["close"] == 181

    def test_news(self):
        raw = json.dumps([{"T": "n", "S": "AAPL", "headline": "Apple raises guidance",
                            "summary": "...", "url": "https://x"}])
        events = _normalize_alpaca_message(raw, "alpaca-equity")
        assert events[0].kind == EventKind.NEWS
        assert events[0].payload["headline"].startswith("Apple")

    def test_mixed_payload(self):
        raw = json.dumps([
            {"T": "q", "S": "AAPL", "bp": 1, "ap": 2},
            {"T": "t", "S": "MSFT", "p": 400, "s": 5},
            {"T": "subscription", "trades": ["AAPL"]},  # control msg → dropped
        ])
        events = _normalize_alpaca_message(raw, "alpaca-equity")
        kinds = [(e.symbol, e.kind) for e in events]
        assert ("AAPL", EventKind.QUOTE) in kinds
        assert ("MSFT", EventKind.TRADE) in kinds
        assert len(events) == 2

    def test_drops_messages_without_symbol(self):
        raw = json.dumps([{"T": "q"}])  # no S/symbol
        assert _normalize_alpaca_message(raw, "alpaca-equity") == []

    def test_to_jsonable(self):
        ev = StreamingEvent(source="alpaca-equity", kind=EventKind.QUOTE,
                            symbol="AAPL", received_at=1234.0, payload={"bid": 1, "ask": 2})
        d = ev.to_jsonable()
        assert d["kind"] == "quote"
        assert d["symbol"] == "AAPL"


class TestStatusOK:
    def test_dict_no_error(self):
        assert _alpaca_status_ok({"T": "success", "msg": "authenticated"})

    def test_list_with_success(self):
        assert _alpaca_status_ok([{"T": "success", "msg": "authenticated"}])

    def test_list_with_error(self):
        assert not _alpaca_status_ok([{"T": "error", "code": 402, "msg": "auth failed"}])


class TestInMemoryClient:
    def test_plays_events_into_queue(self):
        events = [
            StreamingEvent("stub", EventKind.QUOTE, "AAPL", time.time(), {"bid": 1, "ask": 2}),
            StreamingEvent("stub", EventKind.TRADE, "AAPL", time.time(), {"price": 1.5}),
        ]

        async def runner():
            client = InMemoryStreamClient(events)
            q: asyncio.Queue = asyncio.Queue()
            task = asyncio.create_task(client.run(q))
            await asyncio.sleep(0.05)
            client.stop()
            await asyncio.wait_for(task, timeout=1.0)
            received = []
            while not q.empty():
                received.append(q.get_nowait())
            return received

        received = asyncio.run(runner())
        assert len(received) == 2
        assert received[0].kind == EventKind.QUOTE
        assert received[1].kind == EventKind.TRADE

    def test_update_subscriptions(self):
        async def runner():
            client = InMemoryStreamClient([])
            await client.update_subscriptions(["aapl", "msft"])
            return client._subscriptions

        assert asyncio.run(runner()) == ["aapl", "msft"]


class TestAlpacaClientConfig:
    def test_missing_keys_raises(self, monkeypatch):
        monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
        monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)
        client = AlpacaEquityClient(["AAPL"])
        q: asyncio.Queue = asyncio.Queue()
        with pytest.raises(AlpacaAuthError, match="not set"):
            asyncio.run(client.run(q))

    def test_symbols_normalized(self, monkeypatch):
        monkeypatch.setenv("ALPACA_API_KEY_ID", "dummy")
        monkeypatch.setenv("ALPACA_API_SECRET_KEY", "dummy")
        client = AlpacaEquityClient(["aapl", " MSFT ", "", "aapl"])
        assert client._symbols == ["AAPL", "MSFT"]
