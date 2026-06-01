"""Coverage for paper.import_legacy skip branches + RPC-success path,
technical_indicators_tools.get_indicators dispatch, and batch_runner edge
branches. All offline."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agenticwhales import paper, portfolio
from agenticwhales.agents.schemas import OrderSide
from agenticwhales.audit import impersonate


@pytest.fixture(autouse=True)
def _offline(tmp_path, monkeypatch):
    from web import auth
    monkeypatch.setattr(auth, "_db_writable", lambda: False)
    auth._reset_memstore_for_tests()
    home = tmp_path / "home"
    (home / ".agenticwhales").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(portfolio, "_PATH", home / ".agenticwhales" / "portfolio.json")
    yield
    auth._reset_memstore_for_tests()


# ===========================================================================
# import_legacy_portfolio skip branches
# ===========================================================================

def test_import_legacy_skips_when_account_has_pnl():
    from web import auth
    portfolio.save_all({"AAPL": {"qty": 10, "avg_cost": 100.0}})
    auth.upsert_paper_account(user_id="u1", cash=100000.0, starting_cash=100000.0,
                              realized_pnl=50.0)
    with impersonate("u1") as tok:
        assert paper.import_legacy_portfolio(tok) == 0


def test_import_legacy_skips_when_cash_differs():
    from web import auth
    portfolio.save_all({"AAPL": {"qty": 10, "avg_cost": 100.0}})
    auth.upsert_paper_account(user_id="u1", cash=90000.0, starting_cash=100000.0,
                              realized_pnl=0.0)
    with impersonate("u1") as tok:
        assert paper.import_legacy_portfolio(tok) == 0


def test_import_legacy_skips_zero_qty_rows():
    import json
    portfolio._PATH.write_text(json.dumps({"AAPL": {"qty": 0}, "NVDA": {"qty": 5, "avg_cost": 800.0}}))
    with impersonate("u1") as tok:
        n = paper.import_legacy_portfolio(tok)
    assert n == 1  # only NVDA imported; zero-qty AAPL skipped


# ===========================================================================
# positions_for_prompt — empty legacy block
# ===========================================================================

def test_positions_for_prompt_empty_legacy_block():
    # legacy file has only flat positions → block is empty string
    portfolio.save_all({})
    assert paper.positions_for_prompt("u1", "ZZZ") == ""


# ===========================================================================
# place_order RPC-success path
# ===========================================================================

def test_place_order_rpc_success(monkeypatch):
    from web import auth
    from agenticwhales.agents.schemas import PortfolioDecision, PortfolioRating

    monkeypatch.setattr(auth, "call_paper_place_order_rpc",
                        lambda payload: {"order_id": "rpc-1", "idempotent": False})
    decision = PortfolioDecision(rating=PortfolioRating.OVERWEIGHT,
                                 executive_summary="s", investment_thesis="t",
                                 prob_of_profit=0.6, expected_return_pct=8.0,
                                 expected_volatility_pct=12.0)
    with impersonate("u1") as tok:
        res = paper.place_order(
            tok, fire_id="f1", session_id="s1", recipe_id=None,
            ticker="AAPL", side=OrderSide.BUY, qty=10.0, market_price=100.0,
            slippage_bps=10, decision=decision, conviction=8, kelly_fraction=0.05,
            guard=SimpleNamespace(allowed=True, allowed_qty=10.0),
        )
    assert res.order_id == "rpc-1"
    # mirrored into memstore
    assert auth.load_paper_position("u1", "AAPL") is not None


# ===========================================================================
# technical_indicators_tools.get_indicators
# ===========================================================================

def test_get_indicators_dispatch(monkeypatch):
    import agenticwhales.agents.utils.technical_indicators_tools as ti
    monkeypatch.setattr(ti, "route_to_vendor",
                        lambda method, sym, ind, date, lb: f"{ind.upper()}=42")
    out = ti.get_indicators.func("AAPL", "rsi, macd", "2024-01-02")
    assert "RSI=42" in out and "MACD=42" in out


def test_get_indicators_value_error(monkeypatch):
    import agenticwhales.agents.utils.technical_indicators_tools as ti
    def _boom(method, sym, ind, date, lb):
        raise ValueError(f"unknown indicator {ind}")
    monkeypatch.setattr(ti, "route_to_vendor", _boom)
    out = ti.get_indicators.func("AAPL", "bogus", "2024-01-02")
    assert "unknown indicator bogus" in out


# ===========================================================================
# batch_runner edge branches
# ===========================================================================

def test_recompute_totals_no_change_early_return():
    from web.batch_runner import BatchRunner, build_batch

    class _Loop:
        def call_soon_threadsafe(self, fn, *a):
            fn(*a)

    b = build_batch({"tickers": ["AAPL"], "analysis_date": "2024-01-02",
                     "llm_provider": "google", "quick_think_llm": "q",
                     "deep_think_llm": "d", "analysts": ["market"]})
    r = BatchRunner(b, _Loop(), register_session=lambda c: None)
    events = []
    r._broadcast = events.append
    r._recompute_totals()
    n_after_first = len(events)
    r._recompute_totals()  # nothing changed → early return, no new broadcast
    assert len(events) == n_after_first
