"""Unit tests for the NL→strategy compiler + backtest decision generator.

No network / no real LLM: the chat model is injected as a fake returning
canned JSON, and market history is a small synthetic DataFrame.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from agenticwhales import strategy
from agenticwhales.agents.schemas import PortfolioRating


class _FakeLLM:
    """Minimal stand-in for a LangChain chat model: .invoke -> obj with .content."""

    def __init__(self, content: str):
        self._content = content

    def invoke(self, messages):
        class _R:
            content = self._content
        return _R()


def _llm(json_str: str) -> _FakeLLM:
    return _FakeLLM(json_str)


# ---------------------------------------------------------------------------
# compile_strategy
# ---------------------------------------------------------------------------

def test_compile_indicator_cross_long():
    spec = strategy.compile_strategy(
        "go long when it reclaims the 50-day average",
        llm=_llm('{"name":"50DMA reclaim","direction":"long",'
                 '"entry":{"kind":"indicator_cross","fast":"close","slow":"sma_50","direction":"above"},'
                 '"stop_loss_pct":0.06,"hold_days":15,"rationale":"reclaim 50dma"}'),
    )
    assert spec.name == "50DMA reclaim"
    assert spec.direction == "long"
    assert spec.entry_raw["kind"] == "indicator_cross"
    assert spec.stop_loss_pct == 0.06
    assert spec.hold_days == 15
    assert spec.source_text.startswith("go long")


def test_compile_fade_maps_to_underweight_direction():
    spec = strategy.compile_strategy(
        "fade it",
        llm=_llm('{"name":"x","direction":"fade",'
                 '"entry":{"kind":"price_move","threshold_pct":0.05,"window_minutes":1440,"direction":"up"},'
                 '"stop_loss_pct":0.05,"hold_days":10,"rationale":"r"}'),
    )
    assert spec.direction == "fade"


def test_compile_unknown_direction_defaults_long():
    spec = strategy.compile_strategy(
        "whatever",
        llm=_llm('{"name":"x","direction":"sideways",'
                 '"entry":{"kind":"volume_spike","multiplier":2.0,"avg_window_days":20},'
                 '"stop_loss_pct":0.05,"hold_days":10,"rationale":"r"}'),
    )
    assert spec.direction == "long"


def test_compile_clamps_out_of_range_stop_and_hold():
    spec = strategy.compile_strategy(
        "x",
        llm=_llm('{"name":"x","direction":"long",'
                 '"entry":{"kind":"volume_spike","multiplier":3.0,"avg_window_days":20},'
                 '"stop_loss_pct":5,"hold_days":99999,"rationale":"r"}'),
    )
    assert 0.005 <= spec.stop_loss_pct <= 0.5
    assert 1 <= spec.hold_days <= 252


def test_compile_garbage_stop_uses_default():
    spec = strategy.compile_strategy(
        "x",
        llm=_llm('{"name":"x","direction":"long",'
                 '"entry":{"kind":"volume_spike","multiplier":3.0,"avg_window_days":20},'
                 '"stop_loss_pct":"abc","hold_days":"xyz","rationale":"r"}'),
    )
    assert spec.stop_loss_pct == 0.05
    assert spec.hold_days == 20


def test_compile_empty_thesis_raises():
    with pytest.raises(strategy.StrategyError):
        strategy.compile_strategy("   ", llm=_llm("{}"))


def test_compile_non_object_response_raises():
    with pytest.raises(strategy.StrategyError):
        strategy.compile_strategy("x", llm=_llm("[1,2,3]"))


def test_compile_missing_entry_raises():
    with pytest.raises(strategy.StrategyError):
        strategy.compile_strategy(
            "x", llm=_llm('{"name":"x","direction":"long","stop_loss_pct":0.05,"hold_days":10}'))


def test_compile_invalid_json_raises():
    with pytest.raises(strategy.StrategyError):
        strategy.compile_strategy("x", llm=_llm("not json at all"))


def test_compile_price_level_entry():
    spec = strategy.compile_strategy(
        "break 1200 fade",
        llm=_llm('{"name":"x","direction":"fade",'
                 '"entry":{"kind":"price_level","level":1200,"direction":"above"},'
                 '"stop_loss_pct":0.05,"hold_days":10,"rationale":"r"}'),
    )
    assert spec.entry_raw["kind"] == "price_level"
    assert spec.entry_raw["level"] == 1200


def test_compile_price_level_missing_level_raises():
    with pytest.raises(strategy.StrategyError):
        strategy.compile_strategy(
            "x", llm=_llm('{"name":"x","direction":"long",'
                          '"entry":{"kind":"price_level","direction":"above"},'
                          '"stop_loss_pct":0.05,"hold_days":10}'))


def test_compile_composite_empty_children_raises():
    with pytest.raises(strategy.StrategyError):
        strategy.compile_strategy(
            "x", llm=_llm('{"name":"x","direction":"long",'
                          '"entry":{"kind":"and","children":[]},'
                          '"stop_loss_pct":0.05,"hold_days":10}'))


# ---------------------------------------------------------------------------
# to_dict / to_trigger_conditions (price_level lowering)
# ---------------------------------------------------------------------------

def _spec(entry, direction="long"):
    return strategy.StrategySpec(
        name="t", direction=direction, entry_raw=entry,
        stop_loss_pct=0.05, hold_days=10, rationale="r", source_text="src",
    )


def test_to_dict_roundtrip_keys():
    d = _spec({"kind": "volume_spike", "multiplier": 2.0, "avg_window_days": 20}).to_dict()
    assert set(d) == {"name", "direction", "entry", "stop_loss_pct",
                      "hold_days", "rationale", "source_text"}


def test_trigger_conditions_passthrough_for_real_kinds():
    entry = {"kind": "volume_spike", "multiplier": 2.0, "avg_window_days": 20}
    assert _spec(entry).to_trigger_conditions() == entry


def test_trigger_conditions_lowers_price_level_to_price_move():
    out = _spec({"kind": "price_level", "level": 1200, "direction": "above"}).to_trigger_conditions()
    assert out["kind"] == "price_move"
    assert out["direction"] == "up"


def test_trigger_conditions_lowers_price_level_below_to_down():
    out = _spec({"kind": "price_level", "level": 50, "direction": "below"}).to_trigger_conditions()
    assert out["direction"] == "down"


def test_trigger_conditions_composite_lowers_children():
    entry = {"kind": "and", "children": [
        {"kind": "price_level", "level": 1200, "direction": "above"},
        {"kind": "volume_spike", "multiplier": 2.0, "avg_window_days": 20},
    ]}
    out = _spec(entry).to_trigger_conditions()
    assert out["kind"] == "and"
    kinds = {c["kind"] for c in out["children"]}
    assert kinds == {"price_move", "volume_spike"}


def test_trigger_conditions_non_dict_returns_none():
    assert _spec("nope").to_trigger_conditions() is None


# ---------------------------------------------------------------------------
# decision generator + entry evaluation
# ---------------------------------------------------------------------------

def _history(closes, volumes=None):
    n = len(closes)
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    vol = volumes if volumes is not None else [1_000_000] * n
    return pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes, "Volume": vol},
        index=idx,
    )


def test_generator_returns_none_on_short_history():
    spec = _spec({"kind": "price_level", "level": 100, "direction": "above"})
    gen = strategy.strategy_decision_generator(spec)
    assert gen("AAPL", dt.date(2024, 1, 2), _history([100, 101])) is None


def test_generator_long_fires_overweight_on_price_level():
    spec = _spec({"kind": "price_level", "level": 100, "direction": "above"}, direction="long")
    gen = strategy.strategy_decision_generator(spec)
    hist = _history([90] * 24 + [150])  # last close 150 ≥ 100
    dec = gen("AAPL", dt.date(2024, 1, 25), hist)
    assert dec is not None
    assert dec.rating == PortfolioRating.OVERWEIGHT
    assert dec.stop_loss < 150  # long stop below entry


def test_generator_fade_fires_underweight_with_stop_above():
    spec = _spec({"kind": "price_level", "level": 100, "direction": "above"}, direction="fade")
    gen = strategy.strategy_decision_generator(spec)
    hist = _history([90] * 24 + [150])
    dec = gen("AAPL", dt.date(2024, 1, 25), hist)
    assert dec.rating == PortfolioRating.UNDERWEIGHT
    assert dec.stop_loss > 150  # fade stop above entry


def test_generator_no_fire_when_condition_unmet():
    spec = _spec({"kind": "price_level", "level": 1000, "direction": "above"})
    gen = strategy.strategy_decision_generator(spec)
    hist = _history([90] * 25)  # never reaches 1000
    assert gen("AAPL", dt.date(2024, 1, 25), hist) is None


def test_eval_entry_price_level_below():
    snap = strategy.MarketSnapshot(symbol="X", last_price=40.0)
    assert strategy._eval_entry({"kind": "price_level", "level": 50, "direction": "below"}, snap) is True
    assert strategy._eval_entry({"kind": "price_level", "level": 30, "direction": "below"}, snap) is False


def test_eval_entry_and_or_composites():
    snap = strategy.MarketSnapshot(symbol="X", last_price=120.0)
    above = {"kind": "price_level", "level": 100, "direction": "above"}
    below = {"kind": "price_level", "level": 50, "direction": "below"}
    assert strategy._eval_entry({"kind": "and", "children": [above, below]}, snap) is False
    assert strategy._eval_entry({"kind": "or", "children": [above, below]}, snap) is True


def test_eval_entry_price_level_missing_price_false():
    snap = strategy.MarketSnapshot(symbol="X", last_price=None)
    assert strategy._eval_entry({"kind": "price_level", "level": 50, "direction": "above"}, snap) is False


def test_indicator_series_sma_ema_and_close():
    closes = pd.Series([float(i) for i in range(1, 31)])
    assert strategy._indicator_series("close", closes) is closes
    assert strategy._indicator_series("sma_10", closes).notna().sum() > 0
    assert strategy._indicator_series("ema_10", closes).notna().sum() > 0
    assert strategy._indicator_series("bogus", closes) is None
    assert strategy._indicator_series("sma_x", closes) is None


def test_snapshot_builds_volume_and_indicators():
    hist = _history([float(i) for i in range(1, 31)],
                    volumes=[1000] * 29 + [9000])
    entry = {"kind": "indicator_cross", "fast": "close", "slow": "sma_5", "direction": "above"}
    snap = strategy._snapshot_from_history("X", hist, entry)
    assert snap.last_price == 30.0
    assert snap.volume_now == 9000
    assert snap.indicators is not None
    assert "sma_5" in snap.indicators


def test_eval_entry_real_trigger_kind_volume_spike():
    # Exercises the parse_condition + evaluate path for a non-synthetic kind.
    snap = strategy.MarketSnapshot(symbol="X", volume_now=5_000_000, avg_volume=1_000_000)
    assert strategy._eval_entry(
        {"kind": "volume_spike", "multiplier": 2.0, "avg_window_days": 20}, snap) is True
    snap2 = strategy.MarketSnapshot(symbol="X", volume_now=1_100_000, avg_volume=1_000_000)
    assert strategy._eval_entry(
        {"kind": "volume_spike", "multiplier": 2.0, "avg_window_days": 20}, snap2) is False


def test_eval_entry_unparseable_kind_returns_false():
    snap = strategy.MarketSnapshot(symbol="X", last_price=10.0)
    assert strategy._eval_entry({"kind": "totally_unknown"}, snap) is False


def test_compile_uses_factory_when_no_llm(monkeypatch):
    # Cover the `llm is None` branch: patch the factory so no network happens.
    canned = ('{"name":"f","direction":"long",'
              '"entry":{"kind":"volume_spike","multiplier":2.0,"avg_window_days":20},'
              '"stop_loss_pct":0.05,"hold_days":10,"rationale":"r"}')

    class _Client:
        def get_llm(self):
            return _FakeLLM(canned)

    import agenticwhales.llm_clients as llm_mod
    monkeypatch.setattr(llm_mod, "create_llm_client", lambda **kw: _Client())
    spec = strategy.compile_strategy("buy on volume", provider="google", model="x")
    assert spec.name == "f"


def test_generator_indicator_cross_fires():
    # Flat then a jump on the final bar so close crosses above sma_5:
    # before the jump close (10) == sma_5 (10); on the last bar close (50)
    # is well above sma_5 (~18) → a genuine upward cross on that bar.
    closes = [10.0] * 25 + [50.0]
    hist = _history(closes)
    spec = _spec({"kind": "indicator_cross", "fast": "close", "slow": "sma_5", "direction": "above"})
    gen = strategy.strategy_decision_generator(spec)
    dec = gen("X", dt.date(2024, 2, 1), hist)
    assert dec is not None
    assert dec.rating == PortfolioRating.OVERWEIGHT
