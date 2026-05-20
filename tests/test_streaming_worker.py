"""Tests for the streaming worker — symbol routing, dispatch, rate limit."""

from __future__ import annotations

import asyncio
import time
from typing import List, Tuple

import pytest

from agenticwhales.agents.schemas import Recipe, RecipeStatus
from agenticwhales.streaming import (
    EventKind,
    InMemoryStreamClient,
    StreamingEvent,
)
from web.streaming_worker import StreamingWorker, _is_crypto_symbol, _split_symbols


def _build_recipe(*, rid: str = "r1", tickers=("AAPL",), condition=None) -> Recipe:
    return Recipe(
        id=rid,
        user_id="u-1",
        name=f"recipe {rid}",
        tickers=list(tickers),
        analysts=["market"],
        llm_provider="google",
        quick_model="gemini-3-flash-preview",
        deep_model="gemini-3.1-pro-preview",
        bull_model="deepseek-v4",
        bear_model="gemini-3.1-pro-preview",
        trigger_conditions=condition,
        status=RecipeStatus.ACTIVE,
    )


class TestSymbolRouting:
    def test_is_crypto_symbol(self):
        assert _is_crypto_symbol("BTC-USD")
        assert _is_crypto_symbol("ETHUSD")
        assert _is_crypto_symbol("SOL-USDC")
        assert not _is_crypto_symbol("AAPL")
        assert not _is_crypto_symbol("SPY")

    def test_split_symbols(self):
        eq, cr = _split_symbols(["AAPL", "BTC-USD", "msft", "ETHUSD", "AAPL"])
        assert eq == ["AAPL", "MSFT"]
        assert cr == ["BTC-USD", "ETHUSD"]


class TestDispatch:
    def test_fires_on_price_move(self):
        """Trade #1 sets the reference; trade #2 is a >3% move → recipe fires."""
        condition = {"kind": "price_move", "threshold_pct": 0.02, "direction": "either"}
        recipe = _build_recipe(condition=condition)
        events = [
            StreamingEvent("stub", EventKind.TRADE, "AAPL", time.time() - 60,
                           {"price": 100.0, "size": 100}),
            StreamingEvent("stub", EventKind.TRADE, "AAPL", time.time(),
                           {"price": 103.0, "size": 100}),
        ]
        fired: List[Tuple[str, str, str]] = []

        async def fake_fire(r, sym, reason):
            fired.append((r.id, sym, reason))

        async def runner():
            client = InMemoryStreamClient(events)
            worker = StreamingWorker(
                fire_recipe=fake_fire,
                is_leader_fn=lambda: True,
                equity_client_factory=lambda syms: client,
                crypto_client_factory=lambda syms: None,
            )
            await worker.start([recipe])
            await asyncio.sleep(0.1)
            await worker.stop()

        asyncio.run(runner())
        assert len(fired) == 1
        assert fired[0][0] == "r1"
        assert fired[0][1] == "AAPL"
        assert "price moved" in fired[0][2]

    def test_no_fire_when_under_threshold(self):
        condition = {"kind": "price_move", "threshold_pct": 0.05}
        recipe = _build_recipe(condition=condition)
        events = [
            StreamingEvent("stub", EventKind.TRADE, "AAPL", time.time() - 60, {"price": 100.0}),
            StreamingEvent("stub", EventKind.TRADE, "AAPL", time.time(), {"price": 101.0}),
        ]
        fired = []

        async def fake_fire(r, sym, reason):
            fired.append(r.id)

        async def runner():
            client = InMemoryStreamClient(events)
            worker = StreamingWorker(
                fire_recipe=fake_fire, is_leader_fn=lambda: True,
                equity_client_factory=lambda syms: client,
                crypto_client_factory=lambda syms: None,
            )
            await worker.start([recipe])
            await asyncio.sleep(0.1)
            await worker.stop()

        asyncio.run(runner())
        assert fired == []

    def test_non_leader_does_not_fire(self):
        condition = {"kind": "price_move", "threshold_pct": 0.01}
        recipe = _build_recipe(condition=condition)
        events = [
            StreamingEvent("stub", EventKind.TRADE, "AAPL", time.time() - 60, {"price": 100.0}),
            StreamingEvent("stub", EventKind.TRADE, "AAPL", time.time(), {"price": 110.0}),
        ]
        fired = []

        async def fake_fire(r, sym, reason):
            fired.append(r.id)

        async def runner():
            client = InMemoryStreamClient(events)
            worker = StreamingWorker(
                fire_recipe=fake_fire, is_leader_fn=lambda: False,
                equity_client_factory=lambda syms: client,
                crypto_client_factory=lambda syms: None,
            )
            await worker.start([recipe])
            await asyncio.sleep(0.1)
            await worker.stop()

        asyncio.run(runner())
        assert fired == []

    def test_news_keyword_match(self):
        condition = {"kind": "news_keyword", "keywords": ["guidance"]}
        recipe = _build_recipe(condition=condition)
        events = [
            StreamingEvent("stub", EventKind.NEWS, "AAPL", time.time(),
                           {"headline": "Apple raises guidance for FY", "summary": ""}),
        ]
        fired = []

        async def fake_fire(r, sym, reason):
            fired.append((r.id, reason))

        async def runner():
            client = InMemoryStreamClient(events)
            worker = StreamingWorker(
                fire_recipe=fake_fire, is_leader_fn=lambda: True,
                equity_client_factory=lambda syms: client,
                crypto_client_factory=lambda syms: None,
            )
            await worker.start([recipe])
            await asyncio.sleep(0.1)
            await worker.stop()

        asyncio.run(runner())
        assert len(fired) == 1
        assert "guidance" in fired[0][1]

    def test_paused_recipe_skipped(self):
        condition = {"kind": "price_move", "threshold_pct": 0.01}
        recipe = _build_recipe(condition=condition)
        recipe = recipe.model_copy(update={"status": RecipeStatus.PAUSED})
        events = [
            StreamingEvent("stub", EventKind.TRADE, "AAPL", time.time() - 60, {"price": 100.0}),
            StreamingEvent("stub", EventKind.TRADE, "AAPL", time.time(), {"price": 105.0}),
        ]
        fired = []

        async def fake_fire(r, sym, reason):
            fired.append(r.id)

        async def runner():
            client = InMemoryStreamClient(events)
            worker = StreamingWorker(
                fire_recipe=fake_fire, is_leader_fn=lambda: True,
                equity_client_factory=lambda syms: client,
                crypto_client_factory=lambda syms: None,
            )
            await worker.start([recipe])
            await asyncio.sleep(0.1)
            await worker.stop()

        asyncio.run(runner())
        assert fired == []


class TestRateLimit:
    def test_rate_limit_caps_fires(self):
        condition = {"kind": "price_move", "threshold_pct": 0.01}
        # Per-recipe cap (Phase 3) takes precedence over worker default.
        recipe = _build_recipe(condition=condition).model_copy(
            update={"streaming_max_fires_per_hour": 3},
        )
        # Many qualifying events in quick succession.
        now = time.time()
        events = [
            StreamingEvent("stub", EventKind.TRADE, "AAPL", now - 10, {"price": 100.0}),
        ]
        for i in range(20):
            events.append(StreamingEvent(
                "stub", EventKind.TRADE, "AAPL", now + i,
                {"price": 100.0 + (i + 1) * 5},
            ))
        fires = []

        async def fake_fire(r, sym, reason):
            fires.append(time.time())

        async def runner():
            client = InMemoryStreamClient(events)
            worker = StreamingWorker(
                fire_recipe=fake_fire, is_leader_fn=lambda: True,
                equity_client_factory=lambda syms: client,
                crypto_client_factory=lambda syms: None,
                max_fires_per_hour=99,  # worker fallback; recipe cap should dominate
            )
            await worker.start([recipe])
            await asyncio.sleep(0.1)
            await worker.stop()

        asyncio.run(runner())
        assert len(fires) == 3, f"expected 3 fires under cap, got {len(fires)}"


class TestUpdateRecipes:
    def test_resubscribes_clients(self):
        recipe_a = _build_recipe(rid="a", tickers=["AAPL"],
                                 condition={"kind": "price_move", "threshold_pct": 0.01})
        recipe_b = _build_recipe(rid="b", tickers=["TSLA"],
                                 condition={"kind": "price_move", "threshold_pct": 0.01})

        async def fake_fire(r, sym, reason):
            pass

        async def runner():
            client = InMemoryStreamClient([])
            worker = StreamingWorker(
                fire_recipe=fake_fire, is_leader_fn=lambda: True,
                equity_client_factory=lambda syms: client,
                crypto_client_factory=lambda syms: None,
            )
            await worker.start([recipe_a])
            assert "AAPL" in worker._all_symbols()
            await worker.update_recipes([recipe_a, recipe_b])
            assert set(worker._all_symbols()) == {"AAPL", "TSLA"}
            await worker.stop()

        asyncio.run(runner())
