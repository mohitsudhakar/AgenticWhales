"""Coverage for agenticwhales/paper.py — the fill engine (long/short/cover),
NAV math, prompt block, and legacy-portfolio import. Offline: auth memstore +
tmp portfolio.json. No broker, no LLM.
"""

from __future__ import annotations

import pytest

from agenticwhales import paper, portfolio
from agenticwhales.agents.schemas import (
    OrderSide, PaperAccount, PaperPosition,
    PortfolioDecision, PortfolioRating,
)
from agenticwhales.audit import impersonate


@pytest.fixture(autouse=True)
def _offline(tmp_path, monkeypatch):
    from web import auth
    monkeypatch.setattr(auth, "_db_writable", lambda: False)
    auth._reset_memstore_for_tests()
    # Redirect the legacy JSON portfolio to a tmp file.
    monkeypatch.setattr(portfolio, "_PATH", tmp_path / "portfolio.json")
    yield
    auth._reset_memstore_for_tests()


def _acct(user="u1"):
    from web import auth
    return auth.load_paper_account(user)


def _pos(user, ticker):
    from web import auth
    return auth.load_paper_position(user, ticker)


# ---------------------------------------------------------------------------
# _apply_fill_python — long lifecycle
# ---------------------------------------------------------------------------

def test_buy_opens_and_averages_long():
    paper._apply_fill_python("u1", "AAPL", OrderSide.BUY, 10, 100.0)
    p = _pos("u1", "AAPL")
    assert p["qty"] == 10 and p["avg_cost"] == 100.0
    # add more at a higher price → averaged
    paper._apply_fill_python("u1", "AAPL", OrderSide.BUY, 10, 200.0)
    p = _pos("u1", "AAPL")
    assert p["qty"] == 20 and p["avg_cost"] == 150.0
    # cash debited by 10*100 + 10*200 from default starting cash
    assert _acct("u1")["cash"] == pytest.approx(paper.DEFAULT_STARTING_CASH - 3000.0)


def test_sell_realizes_pnl_and_closes():
    paper._apply_fill_python("u1", "AAPL", OrderSide.BUY, 10, 100.0)
    paper._apply_fill_python("u1", "AAPL", OrderSide.SELL, 10, 120.0)
    assert _pos("u1", "AAPL") is None          # fully closed → row deleted
    assert _acct("u1")["realized_pnl"] == pytest.approx(200.0)  # (120-100)*10


def test_sell_on_flat_is_noop():
    paper._apply_fill_python("u1", "AAPL", OrderSide.SELL, 5, 100.0)
    assert _pos("u1", "AAPL") is None
    assert _acct("u1") is None or _acct("u1")["realized_pnl"] == 0.0


# ---------------------------------------------------------------------------
# short lifecycle
# ---------------------------------------------------------------------------

def test_short_opens_negative_with_collateral():
    paper._apply_fill_python("u1", "AAPL", OrderSide.SHORT, 10, 100.0)
    p = _pos("u1", "AAPL")
    assert p["qty"] == -10 and p["avg_cost"] == 100.0
    acct = _acct("u1")
    assert acct["short_collateral_reserved"] == pytest.approx(1000.0)


def test_cover_realizes_short_pnl():
    paper._apply_fill_python("u1", "AAPL", OrderSide.SHORT, 10, 100.0)
    paper._apply_fill_python("u1", "AAPL", OrderSide.COVER, 10, 80.0)  # price fell → profit
    assert _pos("u1", "AAPL") is None
    assert _acct("u1")["realized_pnl"] == pytest.approx(200.0)  # (100-80)*10


def test_cover_on_flat_is_noop():
    paper._apply_fill_python("u1", "AAPL", OrderSide.COVER, 5, 100.0)
    assert _pos("u1", "AAPL") is None


def test_buy_through_short_covers_then_flips_long():
    paper._apply_fill_python("u1", "AAPL", OrderSide.SHORT, 10, 100.0)
    paper._apply_fill_python("u1", "AAPL", OrderSide.BUY, 15, 90.0)  # cover 10, open 5 long
    p = _pos("u1", "AAPL")
    assert p["qty"] == pytest.approx(5.0)
    assert p["avg_cost"] == pytest.approx(90.0)


def test_unknown_side_is_noop():
    # Construct via the enum but pass an unexpected value through the else branch.
    paper._apply_fill_python("u1", "AAPL", "weird-side", 5, 100.0)
    assert _pos("u1", "AAPL") is None


# ---------------------------------------------------------------------------
# NAV
# ---------------------------------------------------------------------------

def test_compute_nav_from_rows_long_and_short():
    nav, unreal = paper.compute_nav_from_rows(
        {"cash": 10000.0},
        [
            {"qty": 10, "avg_cost": 100.0, "last_price": 120.0},   # long +200
            {"qty": -5, "avg_cost": 200.0, "last_price": 150.0},   # short +250
            {"qty": 3, "avg_cost": 50.0, "last_price": None},      # skipped (no price)
        ],
    )
    # nav = 10000 + 10*120 + (200-150)*5 = 10000 + 1200 + 250
    assert nav == pytest.approx(11450.0)
    assert unreal == pytest.approx((120 - 100) * 10 + (200 - 150) * 5)


def test_compute_nav_typed_wrapper():
    acct = PaperAccount(user_id="u1", cash=5000.0)
    pos = [PaperPosition(user_id="u1", ticker="AAPL", qty=10, avg_cost=100.0, last_price=110.0)]
    nav, unreal = paper.compute_nav(acct, pos)
    assert nav == pytest.approx(6100.0)
    assert unreal == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# positions_for_prompt
# ---------------------------------------------------------------------------

def test_positions_for_prompt_from_paper_long():
    paper._apply_fill_python("u1", "AAPL", OrderSide.BUY, 10, 100.0)
    block = paper.positions_for_prompt("u1", "aapl")
    assert "USER'S CURRENT POSITION (paper)" in block
    assert "LONG 10 units of AAPL" in block


def test_positions_for_prompt_falls_back_to_legacy():
    # No paper position → legacy JSON path; seed legacy with a short.
    portfolio.save_all({"AAPL": {"qty": -5}})
    block = paper.positions_for_prompt("u1", "AAPL")
    assert "**SHORT**" in block


def test_positions_for_prompt_empty_when_nothing():
    assert paper.positions_for_prompt("u1", "ZZZZ") == ""


# ---------------------------------------------------------------------------
# import_legacy_portfolio
# ---------------------------------------------------------------------------

def test_import_legacy_noop_when_no_file():
    with impersonate("u1") as tok:
        assert paper.import_legacy_portfolio(tok) == 0


def test_import_legacy_imports_positions(tmp_path, monkeypatch):
    # import_legacy_portfolio reads ~/.agenticwhales/portfolio.json via its own
    # hardcoded Path, so point HOME at a tmp dir and write the file there.
    home = tmp_path / "home"
    (home / ".agenticwhales").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(portfolio, "_PATH", home / ".agenticwhales" / "portfolio.json")
    portfolio.save_all({"AAPL": {"qty": 10, "avg_cost": 100.0},
                        "NVDA": {"qty": -2, "avg_cost": 800.0}})
    with impersonate("u1") as tok:
        n = paper.import_legacy_portfolio(tok)
    assert n == 2
    assert _pos("u1", "AAPL")["qty"] == 10
    assert _pos("u1", "NVDA")["qty"] == -2
    # second run is a no-op (file renamed)
    with impersonate("u1") as tok:
        assert paper.import_legacy_portfolio(tok) == 0


# ---------------------------------------------------------------------------
# kelly edge: malformed stop falls through to vol; zero edge → no bet
# ---------------------------------------------------------------------------

def test_kelly_zero_edge_no_bet():
    d = PortfolioDecision(rating=PortfolioRating.OVERWEIGHT, executive_summary="s",
                          investment_thesis="t", prob_of_profit=0.3,
                          expected_return_pct=1.0, expected_volatility_pct=20.0)
    res = paper.kelly_sizing(d, nav=100000.0, last_price=100.0,
                             kelly_fraction_cap=0.1, user_id="u1")
    assert res.qty == 0.0 and res.fraction == 0.0


def test_kelly_positive_edge_sizes_long():
    d = PortfolioDecision(rating=PortfolioRating.OVERWEIGHT, executive_summary="s",
                          investment_thesis="t", prob_of_profit=0.7,
                          expected_return_pct=10.0, expected_volatility_pct=10.0,
                          stop_loss=90.0)
    res = paper.kelly_sizing(d, nav=100000.0, last_price=100.0,
                             kelly_fraction_cap=0.5, user_id="u1")
    assert res.qty > 0 and res.direction > 0
