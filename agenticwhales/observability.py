"""Lightweight observability primitives for Phase 1.

Three things:
  - Prometheus metric registry — counters, histograms, gauges that scheduler
    + runner + LLM client increment. Designed for the eventual `/metrics`
    endpoint in `web/server.py`.
  - Structured logging via `structlog` when available; falls back to stdlib
    `logging` with a JSON formatter otherwise.
  - A `correlation_id` context var so log lines and metric labels can carry
    the fire_id / session_id without threading them through every call site.

Why first-class in Phase 1, not a Phase 4 follow-up: 24/7 autonomous fires
need observability from day one. Adding metrics after the fact means
recompiling the mental model every time you hit a production bug.

Cardinality budget: `user_id`-keyed gauges are guarded by an env flag
(`AGENTICWHALES_HIGH_CARD_METRICS=1`); off by default so Prometheus doesn't
blow up at scale.
"""

from __future__ import annotations

import logging
import os
import sys
from contextvars import ContextVar
from typing import Any, Optional

try:
    from prometheus_client import (  # type: ignore
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )
    _HAS_PROM = True
except ImportError:  # pragma: no cover
    _HAS_PROM = False

try:
    import structlog  # type: ignore
    _HAS_STRUCTLOG = True
except ImportError:  # pragma: no cover
    _HAS_STRUCTLOG = False


# ---------------------------------------------------------------------------
# Correlation context — populate around every recipe-fire / runner cycle
# ---------------------------------------------------------------------------

correlation_id: ContextVar[Optional[str]] = ContextVar("correlation_id", default=None)
user_context: ContextVar[Optional[str]] = ContextVar("user_context", default=None)


# ---------------------------------------------------------------------------
# Prometheus registry — shared singleton
# ---------------------------------------------------------------------------

class _Metrics:
    """Container for all process-wide Prometheus metrics.

    Held off the module top-level so import-time doesn't fail when
    prometheus-client isn't installed (e.g. minimal CLI invocations).
    """

    def __init__(self) -> None:
        if not _HAS_PROM:
            self.enabled = False
            return
        self.enabled = True
        self.registry = CollectorRegistry()
        self.llm_call_latency = Histogram(
            "aw_llm_call_seconds",
            "End-to-end LLM call latency, including retries",
            ["provider", "model", "agent"],
            buckets=(0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 45.0, 90.0, 180.0),
            registry=self.registry,
        )
        self.llm_call_cost = Histogram(
            "aw_llm_call_cost_usd",
            "Cost per LLM call in USD",
            ["provider", "model"],
            buckets=(0.0001, 0.001, 0.01, 0.05, 0.10, 0.50, 1.0, 5.0),
            registry=self.registry,
        )
        self.recipe_fire = Counter(
            "aw_recipe_fire_total",
            "Recipe fire attempts by terminal status",
            ["status"],   # ok|skipped|failed|breaker|budget|kill|drawdown
            registry=self.registry,
        )
        self.scheduler_lag = Gauge(
            "aw_scheduler_lag_seconds",
            "Seconds between planned next_run and now() over active recipes",
            registry=self.registry,
        )
        self.paper_order = Counter(
            "aw_paper_order_total",
            "Paper orders by side and status",
            ["side", "status"],
            registry=self.registry,
        )
        self.risk_event = Counter(
            "aw_risk_event_total",
            "Risk events by rule",
            ["rule"],
            registry=self.registry,
        )
        self.post_decision_queue = Gauge(
            "aw_post_decision_queue_size",
            "Current size of the post-decision hook queue",
            registry=self.registry,
        )
        self.post_decision_drops = Counter(
            "aw_post_decision_drops_total",
            "Post-decision hook drops due to queue full",
            registry=self.registry,
        )
        self.scheduler_is_leader = Gauge(
            "aw_scheduler_leader",
            "1 if this worker holds the scheduler leader lock; 0 otherwise",
            registry=self.registry,
        )

    def scrape(self) -> bytes:
        """Return the Prometheus text exposition. Empty bytes when disabled."""
        if not self.enabled:
            return b""
        return generate_latest(self.registry)

    def scrape_content_type(self) -> str:
        return CONTENT_TYPE_LATEST if self.enabled else "text/plain"


METRICS = _Metrics()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def configure_logging(level: str = "INFO", fmt: str = "auto") -> None:
    """Initialize logging.

    `fmt` controls output shape:
      - "json"   → structured JSON (best for prod log aggregation)
      - "kv"     → human-friendly key=value (best for local dev)
      - "auto"   → "kv" when stderr is a TTY, "json" otherwise
    """
    if fmt == "auto":
        fmt = "kv" if sys.stderr.isatty() else "json"

    level_val = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level_val)

    if _HAS_STRUCTLOG:
        processors = [
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _correlation_processor,
        ]
        renderer = (
            structlog.processors.JSONRenderer()
            if fmt == "json"
            else structlog.dev.ConsoleRenderer(colors=False)
        )
        structlog.configure(
            processors=processors + [renderer],
            wrapper_class=structlog.make_filtering_bound_logger(level_val),
            logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
            cache_logger_on_first_use=True,
        )
    else:  # pragma: no cover
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
        ))
        if not root.handlers:
            root.addHandler(handler)


def _correlation_processor(logger: Any, method_name: str, event_dict: dict) -> dict:
    """structlog processor that mixes in correlation_id + user_context."""
    cid = correlation_id.get()
    if cid:
        event_dict.setdefault("correlation_id", cid)
    uid = user_context.get()
    if uid:
        event_dict.setdefault("user_id", uid)
    return event_dict


def get_logger(name: str = "agenticwhales") -> Any:
    """Return a structured logger when available, falling back to a stdlib shim.

    The shim accepts the same `event, **kwargs` shape so callers don't have to
    branch on whether structlog is installed. The kwargs are folded into the
    message text in `key=value` form.
    """
    if _HAS_STRUCTLOG:
        return structlog.get_logger(name)
    return _StdlibLoggerShim(logging.getLogger(name))


class _StdlibLoggerShim:
    """Tiny adapter that turns `logger.info("event", k=v)` calls into a single
    formatted string compatible with `logging.Logger.info(...)`.

    Used when structlog isn't installed (e.g. test environments). Lets the
    rest of the codebase use the structlog calling convention uniformly.
    """

    __slots__ = ("_log",)

    def __init__(self, log: logging.Logger) -> None:
        self._log = log

    def _format(self, event: str, kwargs: dict) -> str:
        if not kwargs:
            return event
        suffix = " ".join(f"{k}={v}" for k, v in kwargs.items())
        return f"{event} {suffix}"

    def debug(self, event: str, **kwargs: Any) -> None:
        self._log.debug(self._format(event, kwargs))

    def info(self, event: str, **kwargs: Any) -> None:
        self._log.info(self._format(event, kwargs))

    def warning(self, event: str, **kwargs: Any) -> None:
        self._log.warning(self._format(event, kwargs))

    def error(self, event: str, **kwargs: Any) -> None:
        self._log.error(self._format(event, kwargs))

    def exception(self, event: str, **kwargs: Any) -> None:
        # Always include traceback for exception-level logs.
        self._log.exception(self._format(event, kwargs))

    # `log.bind(...)` is a no-op in the shim; structlog uses it for ctx vars.
    def bind(self, **_kwargs: Any) -> "_StdlibLoggerShim":
        return self


# ---------------------------------------------------------------------------
# High-cardinality opt-in (P6 / cardinality budget)
# ---------------------------------------------------------------------------

def high_card_enabled() -> bool:
    return os.getenv("AGENTICWHALES_HIGH_CARD_METRICS", "").lower() in ("1", "true", "yes")
