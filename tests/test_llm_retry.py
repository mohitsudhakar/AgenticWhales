"""Unit tests for the LLM retry wrapper (P2.5).

These tests do not invoke any real LLM. The retry behaviour is tested
against a minimal Runnable stand-in that raises ``count`` times before
returning a value, mirroring what a 429-then-success looks like in
production.
"""

from __future__ import annotations

import pytest

from agenticwhales.llm_clients import retry as retry_mod
from agenticwhales.llm_clients.retry import (
    apply_retry,
    get_failure_counts,
    record_provider_failure,
    record_provider_success,
    reset_failure_counts,
)


pytestmark = pytest.mark.unit


# --------------------------------------------------- apply_retry semantics


class _FlakyRunnable:
    """A minimal Runnable stand-in.

    Exposes ``with_retry`` and ``invoke``. The first ``fail_n`` invocations
    raise the configured exception; subsequent calls return ``success_value``.
    Tracks call count so tests can assert on retry behaviour.

    The ``with_retry`` method mimics langchain's API: it returns a new
    Runnable that wraps invoke() with retry logic. We implement a small
    in-place retry loop so the test doesn't depend on a real backoff
    library being installed.
    """

    def __init__(self, fail_n: int = 0, success_value: str = "ok", exc: type = RuntimeError):
        self.fail_n = fail_n
        self.success_value = success_value
        self.exc = exc
        self.calls = 0

    def invoke(self, payload):
        self.calls += 1
        if self.calls <= self.fail_n:
            raise self.exc(f"flaky failure {self.calls}")
        return self.success_value

    def with_retry(self, *, stop_after_attempt, wait_exponential_jitter):
        parent = self

        class _Wrapper:
            def __init__(self):
                self.stop_after_attempt = stop_after_attempt
                self.wait_exponential_jitter = wait_exponential_jitter

            def invoke(self, payload):
                last_exc = None
                for _ in range(stop_after_attempt):
                    try:
                        return parent.invoke(payload)
                    except Exception as e:  # noqa: BLE001 — test stand-in
                        last_exc = e
                raise last_exc

        return _Wrapper()


def test_apply_retry_wraps_when_with_retry_present():
    """A Runnable exposing with_retry should be wrapped (not returned as-is)."""
    base = _FlakyRunnable(fail_n=0)
    wrapped = apply_retry(base, max_attempts=3)

    assert wrapped is not base
    # Default max_attempts of 3 should be propagated to the wrapper.
    assert wrapped.stop_after_attempt == 3


def test_apply_retry_passes_through_when_no_with_retry():
    """Non-langchain objects (mocks, plain functions) must be returned untouched."""

    class _NoRetry:
        def invoke(self, payload):
            return "raw"

    base = _NoRetry()
    assert apply_retry(base) is base


def test_apply_retry_disabled_when_max_attempts_le_1():
    """max_attempts=1 short-circuits and returns the original runnable."""
    base = _FlakyRunnable()
    assert apply_retry(base, max_attempts=1) is base
    assert apply_retry(base, max_attempts=0) is base


def test_wrapped_runnable_retries_transient_failure():
    """Two failures, third call succeeds → wrapped invoke() returns the value."""
    base = _FlakyRunnable(fail_n=2, success_value="hello")
    wrapped = apply_retry(base, max_attempts=3)

    result = wrapped.invoke({"prompt": "x"})

    assert result == "hello"
    assert base.calls == 3, "expected exactly 3 attempts (2 fail, 1 success)"


def test_wrapped_runnable_surfaces_failure_when_exhausted():
    """When all attempts fail, the underlying exception bubbles up."""
    base = _FlakyRunnable(fail_n=10, exc=ConnectionError)
    wrapped = apply_retry(base, max_attempts=3)

    with pytest.raises(ConnectionError):
        wrapped.invoke({"prompt": "x"})

    assert base.calls == 3, "expected exactly max_attempts attempts before giving up"


# --------------------------------------------------- failure counter API


def test_failure_counter_increments_per_provider():
    """record_provider_failure should be per-provider and increment monotonically."""
    reset_failure_counts()

    assert record_provider_failure("anthropic") == 1
    assert record_provider_failure("anthropic") == 2
    assert record_provider_failure("openai") == 1
    assert record_provider_failure("Anthropic") == 3, (
        "provider name should be normalised to lowercase"
    )

    counts = get_failure_counts()
    assert counts == {"anthropic": 3, "openai": 1}


def test_failure_counter_resets_on_success():
    """A success on a provider should clear that provider's counter only."""
    reset_failure_counts()

    record_provider_failure("anthropic")
    record_provider_failure("anthropic")
    record_provider_failure("openai")

    record_provider_success("anthropic")

    counts = get_failure_counts()
    assert counts.get("anthropic", 0) == 0
    assert counts.get("openai") == 1


def test_get_failure_counts_returns_snapshot_copy():
    """Mutating the returned dict must not affect internal state."""
    reset_failure_counts()
    record_provider_failure("xai")

    snapshot = get_failure_counts()
    snapshot["xai"] = 999

    assert get_failure_counts()["xai"] == 1


# --------------------------------------------------- env-var defaults


def test_env_defaults_respected(monkeypatch):
    """Env vars should override the conservative defaults."""
    monkeypatch.setenv("AGENTICWHALES_LLM_RETRY_ATTEMPTS", "5")
    monkeypatch.setenv("AGENTICWHALES_LLM_RETRY_JITTER", "false")

    # Re-derive defaults using the env helpers (the constants in the
    # module are bound at import; the helpers re-read the env every
    # call, so test those directly).
    assert retry_mod._env_int("AGENTICWHALES_LLM_RETRY_ATTEMPTS", 3) == 5
    assert retry_mod._env_bool("AGENTICWHALES_LLM_RETRY_JITTER", True) is False


def test_env_invalid_int_falls_back_to_default(monkeypatch, caplog):
    monkeypatch.setenv("AGENTICWHALES_LLM_RETRY_ATTEMPTS", "not-a-number")

    import logging

    with caplog.at_level(logging.WARNING, logger="agenticwhales.llm_clients.retry"):
        value = retry_mod._env_int("AGENTICWHALES_LLM_RETRY_ATTEMPTS", 3)

    assert value == 3
    assert any("Invalid" in rec.message for rec in caplog.records)
