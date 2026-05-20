"""Retry + reliability wrappers for LLM Runnables.

P2.5 (from docs/review_fix_plan.md): a single 429 or transient network
failure from one provider should not kill a session — retry transparently
first, then surface the failure to the orchestrator which already has a
fallback path via the Phase 1 diversification machinery.

The retry policy is intentionally conservative for two reasons:

1. The graph already has a fallback path (Phase 1 diversification): if a
   provider is truly down, ``_create_provider_llm`` returns None and the
   system falls back to upstream with a WARN. Retry is for *transient*
   failures (429, 503, connection reset), not for "provider has been
   down for hours".
2. Retrying too aggressively on the LLM-call path multiplies cost. The
   defaults below give one or two genuine retries with exponential
   jitter, then surface the failure to LangGraph (which will then bubble
   up to the SessionRunner's failure handler).

This module also exposes a lightweight per-provider failure counter that
the diversification status surface (P1.3) can read for diagnostic display.
A full circuit breaker (open / half-open / closed states with cooldown)
is a follow-up; the counter is the data layer it would need.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import defaultdict
from typing import Any, Dict

logger = logging.getLogger(__name__)


# Defaults are conservative; override via env. Setting attempts to 1
# effectively disables retry (used in tests that want to assert error
# propagation without delay).
def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %d", name, raw, default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


DEFAULT_MAX_ATTEMPTS = _env_int("AGENTICWHALES_LLM_RETRY_ATTEMPTS", 3)
DEFAULT_WAIT_EXPONENTIAL_JITTER = _env_bool(
    "AGENTICWHALES_LLM_RETRY_JITTER", True
)


# Per-provider failure counter. Module-global on purpose so the
# diversification surface can read it without threading a registry
# through every call site. Reset between sessions if you want clean
# stats — see ``reset_failure_counts()``.
_failure_counts: Dict[str, int] = defaultdict(int)
_failure_counts_lock = threading.Lock()


def record_provider_failure(provider: str) -> int:
    """Increment the failure counter for ``provider`` and return the new count.

    Intended to be called from a langchain callback when an LLM call
    raises after all retries are exhausted. Returns the post-increment
    count so callers can log "Nth consecutive failure".
    """
    provider = provider.lower()
    with _failure_counts_lock:
        _failure_counts[provider] += 1
        return _failure_counts[provider]


def record_provider_success(provider: str) -> None:
    """Reset the failure counter for ``provider`` after a successful call."""
    provider = provider.lower()
    with _failure_counts_lock:
        if _failure_counts.get(provider, 0) != 0:
            _failure_counts[provider] = 0


def get_failure_counts() -> Dict[str, int]:
    """Return a snapshot copy of the per-provider failure counter."""
    with _failure_counts_lock:
        return dict(_failure_counts)


def reset_failure_counts() -> None:
    """Clear the per-provider failure counter. Used by tests."""
    with _failure_counts_lock:
        _failure_counts.clear()


def apply_retry(
    llm: Any,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    wait_exponential_jitter: bool = DEFAULT_WAIT_EXPONENTIAL_JITTER,
) -> Any:
    """Wrap a langchain LLM Runnable in a retry policy.

    Returns a new Runnable whose ``.invoke`` / ``.bind_tools`` /
    ``.with_structured_output`` calls retry transient failures with
    exponential backoff and jitter. If the underlying LLM doesn't expose
    ``with_retry`` (e.g. a MagicMock from a test, or a non-langchain
    object), the original LLM is returned unchanged — tests don't have
    to special-case the wrapper, and non-langchain backends still work.

    Args:
        llm: the langchain Runnable (typically a ChatModel) to wrap.
        max_attempts: total attempts including the first call. Default
            comes from ``AGENTICWHALES_LLM_RETRY_ATTEMPTS`` env var (3).
            Set to 1 to disable retry entirely.
        wait_exponential_jitter: when True, use exponential backoff with
            jitter between retries. Recommended to avoid thundering-herd
            on a recovering upstream.

    Returns:
        The wrapped Runnable, or the original ``llm`` if wrapping was
        skipped.
    """
    if not hasattr(llm, "with_retry"):
        logger.debug(
            "apply_retry: %r has no .with_retry; returning unchanged",
            type(llm).__name__,
        )
        return llm
    if max_attempts <= 1:
        return llm
    return llm.with_retry(
        stop_after_attempt=max_attempts,
        wait_exponential_jitter=wait_exponential_jitter,
    )
