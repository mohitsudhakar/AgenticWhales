"""Streaming worker — connects to Alpaca, evaluates trigger conditions, fires recipes.

Lives alongside `RecipeScheduler` and runs only inside the leader worker (the
worker re-uses the scheduler's leadership signal rather than holding its own
advisory lock — single source of truth keeps mental model simple).

Responsibilities:

  1. Hold the union of tickers across active recipes whose `trigger_conditions`
     are non-null. Split into equity vs crypto symbol sets.
  2. Spawn one `StreamClient` per asset class (`AlpacaEquityClient`,
     `AlpacaCryptoClient`), each pumping into a shared `asyncio.Queue`.
  3. Pull events off the queue. Maintain a small per-symbol ring buffer of
     recent quotes/trades/news so we can evaluate conditions that need a
     reference price or recent volume.
  4. For every recipe whose condition fires, call the injected `fire_recipe`
     callback — typically `RecipeScheduler._fire`.
  5. Per-recipe rate-limit so a noisy symbol can't burn a user's daily
     LLM budget (`max_fires_per_hour` from the recipe; default 6).

Test surface: the worker takes its client factory as a parameter so tests can
inject `InMemoryStreamClient` and verify the dispatcher logic without touching
Alpaca.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Deque, Dict, Iterable, List, Optional, Sequence, Tuple

from agenticwhales.agents.schemas import Recipe
from agenticwhales.streaming import (
    AlpacaCryptoClient,
    AlpacaEquityClient,
    EventKind,
    StreamClient,
    StreamingEvent,
)
from agenticwhales.triggers import (
    MarketSnapshot,
    TriggerCondition,
    evaluate,
    parse_condition,
    required_history_days,
)

log = logging.getLogger(__name__)


# Default rate limit, applied per recipe when not overridden on the Recipe row.
# Conservative; an event-driven recipe firing 6×/hour at average ~$0.05/fire
# stays inside a $5/day budget.
DEFAULT_MAX_FIRES_PER_HOUR = 6
SECONDS_PER_HOUR = 3600.0


# ---------------------------------------------------------------------------
# Symbol routing
# ---------------------------------------------------------------------------

def _is_crypto_symbol(symbol: str) -> bool:
    """Crude split — anything matching `*-USD` / `*USD` / `*USDT` etc. goes to
    the crypto feed. Refine later when we have a real instrument registry."""
    s = symbol.upper().strip()
    if not s:
        return False
    return any(s.endswith(suffix) for suffix in ("-USD", "USD", "USDT", "USDC", "BTC", "ETH"))


def _split_symbols(symbols: Iterable[str]) -> Tuple[List[str], List[str]]:
    eq: List[str] = []
    cr: List[str] = []
    for s in symbols:
        (cr if _is_crypto_symbol(s) else eq).append(s.upper())
    return sorted(set(eq)), sorted(set(cr))


# ---------------------------------------------------------------------------
# Per-symbol ring buffer
# ---------------------------------------------------------------------------

@dataclass
class _SymbolState:
    last_quote: Optional[Dict[str, float]] = None
    last_trade: Optional[Dict[str, float]] = None
    last_news: Optional[Dict[str, Any]] = None
    # Window of recent trade prices: (ts, price)
    recent_prices: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=512))


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

ClientFactory = Callable[[Sequence[str]], Optional[StreamClient]]
FireCallback = Callable[[Recipe, str, str], Awaitable[None]]
LeaderCheck = Callable[[], bool]


@dataclass
class _RecipeBinding:
    recipe: Recipe
    condition: TriggerCondition
    fire_history: Deque[float] = field(default_factory=lambda: deque(maxlen=64))


class StreamingWorker:
    """The streaming evaluator. Constructed once per worker process; starts only
    on the leader (`is_leader_fn` returns True)."""

    def __init__(
        self,
        *,
        fire_recipe: FireCallback,
        is_leader_fn: LeaderCheck,
        equity_client_factory: Optional[ClientFactory] = None,
        crypto_client_factory: Optional[ClientFactory] = None,
        max_fires_per_hour: int = DEFAULT_MAX_FIRES_PER_HOUR,
    ) -> None:
        self._fire_recipe = fire_recipe
        self._is_leader_fn = is_leader_fn
        self._eq_factory = equity_client_factory or (lambda syms: AlpacaEquityClient(syms))
        self._cr_factory = crypto_client_factory or (lambda syms: AlpacaCryptoClient(syms))
        self._max_fph_default = max_fires_per_hour

        self._bindings: Dict[str, _RecipeBinding] = {}        # recipe_id → binding
        self._state: Dict[str, _SymbolState] = {}             # symbol → state
        self._eq_client: Optional[StreamClient] = None
        self._cr_client: Optional[StreamClient] = None
        self._queue: Optional[asyncio.Queue] = None
        self._tasks: List[asyncio.Task] = []
        self._stop = asyncio.Event()

    # ---- lifecycle ----

    async def start(self, recipes: Sequence[Recipe]) -> None:
        if self._queue is not None:
            return  # already running
        self._queue = asyncio.Queue(maxsize=8192)
        self._stop.clear()
        self._load_bindings(recipes)
        symbols = self._all_symbols()
        eq, cr = _split_symbols(symbols)
        if eq:
            self._eq_client = self._eq_factory(eq)
            if self._eq_client is not None:
                self._tasks.append(asyncio.create_task(self._eq_client.run(self._queue)))
        if cr:
            self._cr_client = self._cr_factory(cr)
            if self._cr_client is not None:
                self._tasks.append(asyncio.create_task(self._cr_client.run(self._queue)))
        self._tasks.append(asyncio.create_task(self._consume_loop()))
        log.info("streaming worker started: %d equity + %d crypto symbols, %d recipes",
                 len(eq), len(cr), len(self._bindings))

    async def stop(self) -> None:
        self._stop.set()
        for client in (self._eq_client, self._cr_client):
            if client is not None and hasattr(client, "stop"):
                try:
                    client.stop()
                except Exception:
                    pass
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        self._eq_client = None
        self._cr_client = None
        self._queue = None

    async def update_recipes(self, recipes: Sequence[Recipe]) -> None:
        """Reload bindings and resubscribe clients to the new symbol set."""
        self._load_bindings(recipes)
        symbols = self._all_symbols()
        eq, cr = _split_symbols(symbols)
        if self._eq_client is not None:
            await self._eq_client.update_subscriptions(eq)
        if self._cr_client is not None:
            await self._cr_client.update_subscriptions(cr)

    # ---- internals ----

    def _load_bindings(self, recipes: Sequence[Recipe]) -> None:
        new: Dict[str, _RecipeBinding] = {}
        for r in recipes:
            if r.status != "active":
                continue
            if not r.trigger_conditions:
                continue
            try:
                condition = parse_condition(r.trigger_conditions)
            except Exception as exc:
                log.warning("streaming: bad trigger_conditions for recipe=%s: %s", r.id, exc)
                continue
            if condition is None:
                continue
            new[r.id] = _RecipeBinding(recipe=r, condition=condition)
        self._bindings = new

    def _all_symbols(self) -> List[str]:
        out: set = set()
        for b in self._bindings.values():
            out.update(b.recipe.tickers)
        return sorted(out)

    async def _consume_loop(self) -> None:
        assert self._queue is not None
        while not self._stop.is_set():
            try:
                event: StreamingEvent = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            self._update_state(event)
            if not self._is_leader_fn():
                # Drain quietly on non-leader (shouldn't happen, defensive).
                continue
            await self._dispatch(event)

    def _update_state(self, event: StreamingEvent) -> None:
        st = self._state.setdefault(event.symbol, _SymbolState())
        if event.kind == EventKind.QUOTE:
            st.last_quote = dict(event.payload)
        elif event.kind == EventKind.TRADE:
            st.last_trade = dict(event.payload)
            price = event.payload.get("price")
            if isinstance(price, (int, float)):
                st.recent_prices.append((event.received_at, float(price)))
        elif event.kind == EventKind.NEWS:
            st.last_news = dict(event.payload)

    def _snapshot_for(self, symbol: str, event: StreamingEvent,
                      lookback_seconds: float = 3600.0) -> MarketSnapshot:
        st = self._state.get(symbol) or _SymbolState()
        last_price: Optional[float] = None
        ref_price: Optional[float] = None
        if st.recent_prices:
            last_price = st.recent_prices[-1][1]
            cutoff = st.recent_prices[-1][0] - lookback_seconds
            for ts, px in st.recent_prices:
                if ts >= cutoff:
                    ref_price = px
                    break
        headline = None
        body = None
        if event.kind == EventKind.NEWS:
            headline = event.payload.get("headline")
            body = event.payload.get("summary")
        elif st.last_news:
            headline = st.last_news.get("headline")
            body = st.last_news.get("summary")
        return MarketSnapshot(
            symbol=symbol,
            last_price=last_price,
            ref_price=ref_price,
            headline=headline,
            body=body,
        )

    async def _dispatch(self, event: StreamingEvent) -> None:
        now = time.time()
        for binding in list(self._bindings.values()):
            recipe = binding.recipe
            if event.symbol not in {t.upper() for t in recipe.tickers}:
                continue
            if not self._under_rate_limit(binding, now):
                continue
            snap = self._snapshot_for(event.symbol, event)
            result = evaluate(binding.condition, snap)
            if not result.matched:
                continue
            binding.fire_history.append(now)
            log.info("streaming fire recipe=%s symbol=%s reason=%s",
                     recipe.id, event.symbol, result.reason)
            try:
                await self._fire_recipe(recipe, event.symbol, result.reason)
            except Exception as exc:
                log.warning("streaming fire callback failed: recipe=%s err=%s",
                            recipe.id, exc)

    def _under_rate_limit(self, binding: _RecipeBinding, now: float) -> bool:
        # Honor per-recipe cap when set (Phase 3 schema column), fall back to
        # the worker-wide default. Cast through getattr so older Recipe rows
        # missing the column don't break the path.
        cap = getattr(binding.recipe, "streaming_max_fires_per_hour", None) or self._max_fph_default
        cutoff = now - SECONDS_PER_HOUR
        while binding.fire_history and binding.fire_history[0] < cutoff:
            binding.fire_history.popleft()
        return len(binding.fire_history) < cap
