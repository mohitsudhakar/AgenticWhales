"""Offline tests for the cheap edge probe.

No LLM, no network: synthetic prices + injected fake invokers. These lock in the
properties that make the probe's verdict trustworthy — above all, that it is
look-ahead-free and that the random baseline is turnover-matched.
"""

import datetime as _dt

import pandas as pd
import pytest

from agenticwhales.agents.schemas import PortfolioDecision, PortfolioRating
from agenticwhales import edge_probe as ep


# --------------------------------------------------------------------------- #
# Fixtures: synthetic price history
# --------------------------------------------------------------------------- #

def _make_history(days: int = 400, start: str = "2024-06-01", trend: float = 0.0005):
    dates = pd.bdate_range(start=start, periods=days)
    price = 100.0
    rows = []
    for i, dt in enumerate(dates):
        # Deterministic wiggle so features are non-trivial but reproducible.
        drift = trend * i
        wig = 0.01 * ((i % 7) - 3)
        close = price * (1 + drift) * (1 + wig)
        rows.append({"Open": close * 0.999, "High": close * 1.01,
                     "Low": close * 0.99, "Close": close, "Volume": 1_000_000})
    return pd.DataFrame(rows, index=dates)


# --------------------------------------------------------------------------- #
# 1. Look-ahead freeness — the load-bearing property
# --------------------------------------------------------------------------- #

def test_features_are_lookahead_free():
    """Features at an as-of point must not change when future rows are appended."""
    hist = _make_history(days=400)
    cutoff = 250
    bounded = hist.iloc[:cutoff]
    full = hist  # has 150 extra future rows

    feats_bounded = ep.build_features(bounded)
    # Reproduce what the simulator hands the generator: slice <= the same as-of date.
    as_of = bounded.index[-1].date()
    feats_from_full = ep.build_features(full.loc[full.index.date <= as_of])

    assert feats_bounded == feats_from_full
    assert feats_bounded is not None


def test_features_none_when_insufficient_history():
    assert ep.build_features(_make_history(days=10)) is None
    assert ep.build_features(None) is None


# --------------------------------------------------------------------------- #
# 2. Metrics math on a known curve
# --------------------------------------------------------------------------- #

def test_equity_metrics_flat_curve():
    res = ep.SimResult("X", "w", "s",
                       equity_curve=[("2025-01-0%d" % i, 100.0) for i in range(1, 6)])
    m = ep.equity_metrics(res)
    assert m["total_return_pct"] == 0.0
    assert m["max_drawdown_pct"] == 0.0
    assert m["sharpe"] == 0.0


def test_equity_metrics_drawdown_and_return():
    res = ep.SimResult("X", "w", "s", equity_curve=[
        ("d1", 100.0), ("d2", 120.0), ("d3", 60.0), ("d4", 90.0)])
    m = ep.equity_metrics(res)
    assert m["total_return_pct"] == pytest.approx(-10.0, abs=1e-6)
    # Peak 120 -> trough 60 = 50% drawdown.
    assert m["max_drawdown_pct"] == pytest.approx(50.0, abs=1e-6)


# --------------------------------------------------------------------------- #
# 3. Random baseline is turnover- and size-matched
# --------------------------------------------------------------------------- #

def test_random_sign_weights_match_turnover_and_magnitude():
    d1, d2, d3 = _dt.date(2025, 1, 2), _dt.date(2025, 2, 3), _dt.date(2025, 3, 3)
    llm_w = {d1: 1.0, d2: 0.0, d3: -0.5}
    rand = ep.random_sign_weights(llm_w, seed=7)

    # Same active dates (non-zero where LLM is non-zero, flat where LLM is flat).
    assert (rand[d2] == 0.0)
    assert abs(rand[d1]) == 1.0 and abs(rand[d3]) == 0.5
    # Same set of active dates -> identical position count -> matched turnover.
    assert {d for d, w in rand.items() if w != 0} == {d1, d3}


def test_random_sign_weights_seed_deterministic():
    llm_w = {_dt.date(2025, 1, 2): 1.0, _dt.date(2025, 2, 3): -0.5}
    assert ep.random_sign_weights(llm_w, 1) == ep.random_sign_weights(llm_w, 1)


# --------------------------------------------------------------------------- #
# 4. Simulator: cost monotonically reduces return; causal execution
# --------------------------------------------------------------------------- #

def test_cost_reduces_return():
    hist = _make_history(days=300, start="2024-09-01", trend=0.0008)
    window = ep.Window("t", hist.index[60].date().isoformat(),
                       hist.index[-1].date().isoformat())
    sched = ep.monthly_schedule(hist, window)
    weights = {d: 1.0 for d in sched}  # always long -> trades each month

    free = ep.equity_metrics(ep.simulate("X", window, hist, weights,
                                         strategy="s", cost_bps=0.0))
    costed = ep.equity_metrics(ep.simulate("X", window, hist, weights,
                                           strategy="s", cost_bps=50.0))
    assert costed["total_return_pct"] < free["total_return_pct"]


def test_flat_weights_give_zero_return():
    hist = _make_history(days=300)
    window = ep.Window("t", hist.index[60].date().isoformat(),
                       hist.index[-1].date().isoformat())
    sched = ep.monthly_schedule(hist, window)
    res = ep.simulate("X", window, hist, {d: 0.0 for d in sched},
                      strategy="flat", cost_bps=10.0)
    m = ep.equity_metrics(res)
    assert m["total_return_pct"] == pytest.approx(0.0, abs=1e-6)


# --------------------------------------------------------------------------- #
# 5. Debate wiring with injected fake invokers (no LLM)
# --------------------------------------------------------------------------- #

def _fake_invokers(rating=PortfolioRating.OVERWEIGHT):
    def invoke_text(msgs):
        return "fake case citing ret_20d and rsi_14."

    def invoke_structured(msgs):
        return PortfolioDecision(
            rating=rating,
            executive_summary="fake",
            investment_thesis="fake",
            expected_return_pct=5.0,
            expected_volatility_pct=20.0,
            prob_of_profit=0.55,
            expected_hold_days=21,
        )
    return invoke_text, invoke_structured


def test_force_commit_coerces_hold():
    """With force_commit, a Hold from the judge is coerced to a directional call."""
    hist = _make_history(days=200)
    feats = ep.build_features(hist)

    def hold_text(msgs):
        return "x"

    def hold_structured(msgs):
        return PortfolioDecision(
            rating=PortfolioRating.HOLD, executive_summary="x", investment_thesis="x",
            expected_return_pct=-3.0, prob_of_profit=0.4)

    dec = ep.run_debate("AAPL", hist.index[-1].date(), feats,
                        invoke_text=hold_text, invoke_structured=hold_structured,
                        force_commit=True)
    # Negative expected return -> coerced short.
    assert dec.rating == PortfolioRating.UNDERWEIGHT


def test_force_commit_respects_lean_direction():
    dec = PortfolioDecision(rating=PortfolioRating.HOLD, executive_summary="x",
                            investment_thesis="x", expected_return_pct=4.0)
    assert ep._coerce_directional(dec).rating == PortfolioRating.OVERWEIGHT


def test_coerce_leaves_directional_untouched():
    dec = PortfolioDecision(rating=PortfolioRating.SELL, executive_summary="x",
                            investment_thesis="x")
    assert ep._coerce_directional(dec).rating == PortfolioRating.SELL


def test_force_commit_cache_variant_isolation(tmp_path):
    """The same (symbol,date,features) caches separately per variant."""
    base = ep.DecisionCache(tmp_path / "c.jsonl", model="m", variant="")
    forced = ep.DecisionCache(tmp_path / "c.jsonl", model="m", variant="forced-commit")
    hist = _make_history(days=200)
    feats = ep.build_features(hist)
    as_of = hist.index[-1].date()
    dec = PortfolioDecision(rating=PortfolioRating.HOLD, executive_summary="x", investment_thesis="x")
    base.put("AAPL", as_of, feats, dec)
    # Forced variant must NOT see the baseline-variant entry.
    assert forced.get("AAPL", as_of, feats) is None
    assert base.get("AAPL", as_of, feats) is not None


def test_run_debate_returns_decision():
    hist = _make_history(days=200)
    feats = ep.build_features(hist)
    it, isr = _fake_invokers()
    dec = ep.run_debate("AAPL", hist.index[-1].date(), feats,
                        invoke_text=it, invoke_structured=isr)
    assert isinstance(dec, PortfolioDecision)
    assert dec.rating == PortfolioRating.OVERWEIGHT


def test_generate_llm_decisions_uses_cache(tmp_path):
    hist = _make_history(days=400)
    window = ep.Window("t", hist.index[120].date().isoformat(),
                       hist.index[-1].date().isoformat())
    sched = ep.monthly_schedule(hist, window)
    cache = ep.DecisionCache(tmp_path / "cache.jsonl", model="fake")

    calls = {"n": 0}

    def counting_text(msgs):
        calls["n"] += 1
        return "x"

    _, isr = _fake_invokers()
    d1 = ep.generate_llm_decisions("AAPL", hist, sched,
                                   invoke_text=counting_text, invoke_structured=isr, cache=cache)
    first = calls["n"]
    assert first > 0
    # Second run with a fresh cache object reading the same file -> no new calls.
    cache2 = ep.DecisionCache(tmp_path / "cache.jsonl", model="fake")
    d2 = ep.generate_llm_decisions("AAPL", hist, sched,
                                   invoke_text=counting_text, invoke_structured=isr, cache=cache2)
    assert calls["n"] == first  # served entirely from cache
    assert set(d1) == set(d2)


# --------------------------------------------------------------------------- #
# 6. Verdict logic (pre-registered rule)
# --------------------------------------------------------------------------- #

def _wr(name, llm, rand, classical):
    return {"window": name, "median_sharpe": {
        "llm": llm, "random": rand, "classical": classical, "buy_hold": 0.5}}


def test_verdict_kill_when_loses_to_random():
    v = ep.evaluate_verdict([_wr("w1", 0.3, 0.5, 0.2), _wr("w2", 0.8, 0.4, 0.3)])
    assert v["verdict"] == "KILL"


def test_verdict_green_when_beats_both_everywhere():
    v = ep.evaluate_verdict([_wr("w1", 0.9, 0.2, 0.4), _wr("w2", 1.1, 0.3, 0.5)])
    assert v["verdict"] == "GREEN"


def test_verdict_amber_when_ties_classical():
    v = ep.evaluate_verdict([_wr("w1", 0.9, 0.2, 0.95), _wr("w2", 1.1, 0.3, 0.5)])
    assert v["verdict"] == "AMBER"


# --------------------------------------------------------------------------- #
# 7. End-to-end run_symbol with fake invokers (no network)
# --------------------------------------------------------------------------- #

def test_run_symbol_offline():
    hist = _make_history(days=400, trend=0.0008)
    window = ep.Window("t", hist.index[200].date().isoformat(),
                       hist.index[-1].date().isoformat())
    it, isr = _fake_invokers()
    out = ep.run_symbol("AAPL", window, hist,
                        invoke_text=it, invoke_structured=isr,
                        random_seeds=(1, 2, 3))
    assert out["symbol"] == "AAPL"
    for strat in ("llm", "random", "classical", "buy_hold"):
        assert strat in out
        assert "sharpe" in out[strat]
    assert out["n_decisions"] >= 1
