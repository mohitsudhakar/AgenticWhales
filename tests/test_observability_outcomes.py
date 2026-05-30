"""Coverage for agenticwhales/observability.py (metrics + logging shim) and
agenticwhales/outcomes.py (decision-outcome resolver). All offline: the
market-snapshot fetch is monkeypatched; paper orders live in the memstore.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from agenticwhales import observability as obs
from agenticwhales import outcomes as oc
from agenticwhales.agents.schemas import OrderSide


# ===========================================================================
# observability
# ===========================================================================

def test_metrics_scrape_and_content_type():
    obs.METRICS.recipe_fire.labels(status="ok").inc()
    body = obs.METRICS.scrape()
    assert isinstance(body, bytes)
    assert isinstance(obs.METRICS.scrape_content_type(), str)


@pytest.mark.parametrize("fmt", ["json", "kv", "auto"])
def test_configure_logging_variants(fmt):
    obs.configure_logging("INFO", fmt)  # no raise


def test_correlation_processor_mixes_context():
    tok_c = obs.correlation_id.set("cid-1")
    tok_u = obs.user_context.set("u-1")
    try:
        out = obs._correlation_processor(None, "info", {})
        assert out["correlation_id"] == "cid-1" and out["user_id"] == "u-1"
    finally:
        obs.correlation_id.reset(tok_c)
        obs.user_context.reset(tok_u)


def test_correlation_processor_no_context():
    out = obs._correlation_processor(None, "info", {"x": 1})
    assert "correlation_id" not in out and out["x"] == 1


def test_get_logger_returns_logger():
    assert obs.get_logger("test") is not None


def test_stdlib_logger_shim_formats_and_logs():
    shim = obs._StdlibLoggerShim(logging.getLogger("shim-test"))
    assert shim._format("event", {}) == "event"
    assert shim._format("event", {"k": "v"}) == "event k=v"
    shim.debug("d", a=1)
    shim.info("i")
    shim.warning("w", b=2)
    shim.error("e")
    try:
        raise ValueError("boom")
    except ValueError:
        shim.exception("ex", c=3)
    assert shim.bind(x=1) is shim


def test_high_card_enabled(monkeypatch):
    monkeypatch.setenv("AGENTICWHALES_HIGH_CARD_METRICS", "1")
    assert obs.high_card_enabled() is True
    monkeypatch.setenv("AGENTICWHALES_HIGH_CARD_METRICS", "no")
    assert obs.high_card_enabled() is False


# ===========================================================================
# outcomes — price parsing
# ===========================================================================

@pytest.mark.parametrize("block,expected", [
    ("Header\nLatest close: $123.45 on 2024-01-02", 123.45),
    ("Latest Close: 250.0", 250.0),
    ("no relevant line", None),
    ("", None),
])
def test_parse_snapshot_close(block, expected):
    assert oc._parse_snapshot_close(block) == expected


def test_latest_close_success(monkeypatch):
    monkeypatch.setattr(oc, "fetch_snapshot_block",
                        lambda t, d: "Latest close: $100.0")
    assert oc._latest_close("AAPL", "2024-01-02") == 100.0


def test_latest_close_error_returns_none(monkeypatch):
    def _boom(t, d):
        raise RuntimeError("net")
    monkeypatch.setattr(oc, "fetch_snapshot_block", _boom)
    assert oc._latest_close("AAPL", "2024-01-02") is None


# ===========================================================================
# outcomes — scoring helpers
# ===========================================================================

def test_is_hit_and_brier():
    assert oc._is_hit(1.0) is True and oc._is_hit(-0.1) is False
    assert oc._brier(None, True) is None
    assert oc._brier(0.7, True) == pytest.approx((0.7 - 1.0) ** 2)
    assert oc._brier(0.7, False) == pytest.approx(0.49)


def test_order_due_variants():
    now = datetime(2024, 3, 1, tzinfo=timezone.utc)
    due = {"expected_hold_days": 10, "created_at": "2024-01-01T00:00:00+00:00"}
    not_due = {"expected_hold_days": 10, "created_at": "2024-02-28T00:00:00+00:00"}
    assert oc._order_due(due, now) is True
    assert oc._order_due(not_due, now) is False
    # missing hold_days defaults to 30
    assert oc._order_due({"created_at": "2024-01-01T00:00:00+00:00"}, now) is True
    # bad created_at → False
    assert oc._order_due({"created_at": "garbage"}, now) is False
    # naive datetime is coerced to UTC
    assert oc._order_due({"expected_hold_days": 1, "created_at": "2024-01-01T00:00:00"}, now) is True


# ===========================================================================
# outcomes — _resolve_one
# ===========================================================================

NOW = datetime(2024, 6, 1, tzinfo=timezone.utc)


def _order(**over):
    o = {"id": "o1", "user_id": "u1", "ticker": "AAPL", "status": "filled",
         "fill_price": 100.0, "side": OrderSide.BUY.value,
         "created_at": "2024-01-01T00:00:00+00:00", "expected_hold_days": 10,
         "prob_of_profit": 0.6, "expected_return_pct": 5.0,
         "expected_volatility_pct": 12.0}
    o.update(over)
    return o


def test_resolve_one_long_profit(monkeypatch):
    monkeypatch.setattr(oc, "_latest_close", lambda t, d: 120.0)
    row = oc._resolve_one(_order(), NOW)
    assert row is not None and row.hit is True
    assert row.realized_return_pct == pytest.approx(20.0)


def test_resolve_one_short_flips_sign(monkeypatch):
    monkeypatch.setattr(oc, "_latest_close", lambda t, d: 80.0)
    row = oc._resolve_one(_order(side=OrderSide.SHORT.value), NOW)
    # price fell → short profits → positive return, hit
    assert row.realized_return_pct == pytest.approx(20.0) and row.hit is True


def test_resolve_one_skips_blocked():
    assert oc._resolve_one(_order(status="blocked"), NOW) is None


def test_resolve_one_clamped_resolves(monkeypatch):
    monkeypatch.setattr(oc, "_latest_close", lambda t, d: 110.0)
    assert oc._resolve_one(_order(status="clamped"), NOW) is not None


def test_resolve_one_not_due():
    assert oc._resolve_one(_order(created_at="2024-05-31T00:00:00+00:00"), NOW) is None


def test_resolve_one_no_close(monkeypatch):
    monkeypatch.setattr(oc, "_latest_close", lambda t, d: None)
    assert oc._resolve_one(_order(), NOW) is None


def test_resolve_one_bad_fill_price(monkeypatch):
    monkeypatch.setattr(oc, "_latest_close", lambda t, d: 110.0)
    assert oc._resolve_one(_order(fill_price=0.0), NOW) is None


# ===========================================================================
# outcomes — resolve_outcomes_for_user + readers
# ===========================================================================

@pytest.fixture(autouse=True)
def _wipe():
    from web import auth
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


def test_resolve_for_user_writes_and_is_idempotent(monkeypatch):
    from web import auth
    monkeypatch.setattr(oc, "_latest_close", lambda t, d: 120.0)
    auth._memstore[("paper_orders", "o1")] = _order()
    written = oc.resolve_outcomes_for_user("u1", now=NOW)
    assert len(written) == 1 and written[0].paper_order_id == "o1"
    # second run skips the already-resolved order
    assert oc.resolve_outcomes_for_user("u1", now=NOW) == []


def test_resolve_for_user_no_orders():
    assert oc.resolve_outcomes_for_user("nobody", now=NOW) == []


def test_list_outcomes_and_brier(monkeypatch):
    from web import auth
    monkeypatch.setattr(oc, "_latest_close", lambda t, d: 120.0)
    auth._memstore[("paper_orders", "o1")] = _order()
    oc.resolve_outcomes_for_user("u1", now=NOW)
    rows = oc.list_outcomes_for_user("u1")
    assert len(rows) == 1
    score = oc.brier_score("u1")
    assert score is not None and 0.0 <= score <= 1.0


def test_brier_score_none_when_empty():
    assert oc.brier_score("nobody") is None
