"""Tests for trigger condition parsing + evaluation."""

from __future__ import annotations

import pytest

from agenticwhales.triggers import (
    CompositeCondition,
    IndicatorCrossCondition,
    MarketSnapshot,
    NewsKeywordCondition,
    PriceMoveCondition,
    TimeCondition,
    TriggerKind,
    VolumeSpikeCondition,
    evaluate,
    parse_condition,
    required_history_days,
)


class TestParse:
    def test_none_returns_none(self):
        assert parse_condition(None) is None
        assert parse_condition({}) is None
        assert parse_condition("") is None

    def test_passthrough_already_typed(self):
        c = PriceMoveCondition(threshold_pct=0.03)
        assert parse_condition(c) is c

    def test_price_move_dict(self):
        c = parse_condition({"kind": "price_move", "threshold_pct": 0.05, "direction": "down"})
        assert isinstance(c, PriceMoveCondition)
        assert c.threshold_pct == 0.05
        assert c.direction == "down"

    def test_volume_spike_dict(self):
        c = parse_condition({"kind": "volume_spike", "multiplier": 3.0})
        assert isinstance(c, VolumeSpikeCondition)
        assert c.avg_window_days == 20  # default

    def test_news_keyword_dict(self):
        c = parse_condition({"kind": "news_keyword", "keywords": ["earnings", "guidance"]})
        assert isinstance(c, NewsKeywordCondition)
        assert c.keywords == ["earnings", "guidance"]

    def test_indicator_cross_dict(self):
        c = parse_condition({"kind": "indicator_cross", "fast": "sma_20", "slow": "sma_50"})
        assert isinstance(c, IndicatorCrossCondition)

    def test_time_dict(self):
        c = parse_condition({"kind": "time", "hour_utc": 13, "minute_utc": 30})
        assert isinstance(c, TimeCondition)

    def test_composite_dict(self):
        c = parse_condition({
            "kind": "and",
            "children": [
                {"kind": "price_move", "threshold_pct": 0.02},
                {"kind": "volume_spike", "multiplier": 2.0},
            ],
        })
        assert isinstance(c, CompositeCondition)
        assert len(c.children) == 2

    def test_unknown_kind_raises(self):
        with pytest.raises(ValueError, match="unknown trigger condition kind"):
            parse_condition({"kind": "ipo_pop"})

    def test_non_dict_raises(self):
        with pytest.raises(ValueError, match="must be a JSON object"):
            parse_condition("price_move")


class TestPriceMove:
    def test_match_up_move(self):
        c = PriceMoveCondition(threshold_pct=0.03, direction="up")
        s = MarketSnapshot(symbol="AAPL", last_price=103.0, ref_price=100.0)
        r = evaluate(c, s)
        assert r.matched
        assert "+3.00%" in r.reason

    def test_reject_wrong_direction(self):
        c = PriceMoveCondition(threshold_pct=0.03, direction="up")
        s = MarketSnapshot(symbol="AAPL", last_price=97.0, ref_price=100.0)
        assert not evaluate(c, s)

    def test_either_direction(self):
        c = PriceMoveCondition(threshold_pct=0.03, direction="either")
        assert evaluate(c, MarketSnapshot(symbol="AAPL", last_price=97.0, ref_price=100.0))
        assert evaluate(c, MarketSnapshot(symbol="AAPL", last_price=103.0, ref_price=100.0))

    def test_below_threshold(self):
        c = PriceMoveCondition(threshold_pct=0.03)
        s = MarketSnapshot(symbol="AAPL", last_price=101.0, ref_price=100.0)
        assert not evaluate(c, s)

    def test_missing_data(self):
        c = PriceMoveCondition(threshold_pct=0.03)
        assert not evaluate(c, MarketSnapshot(symbol="AAPL"))


class TestVolumeSpike:
    def test_match(self):
        c = VolumeSpikeCondition(multiplier=3.0)
        s = MarketSnapshot(symbol="AAPL", volume_now=3_500_000, avg_volume=1_000_000)
        assert evaluate(c, s)

    def test_reject(self):
        c = VolumeSpikeCondition(multiplier=3.0)
        s = MarketSnapshot(symbol="AAPL", volume_now=2_500_000, avg_volume=1_000_000)
        assert not evaluate(c, s)


class TestNewsKeyword:
    def test_match_case_insensitive(self):
        c = NewsKeywordCondition(keywords=["earnings", "guidance"])
        s = MarketSnapshot(symbol="AAPL", headline="Apple beats Q3 earnings")
        assert evaluate(c, s)

    def test_case_sensitive(self):
        c = NewsKeywordCondition(keywords=["FDA"], case_sensitive=True)
        assert not evaluate(c, MarketSnapshot(symbol="X", body="fda approves drug"))
        assert evaluate(c, MarketSnapshot(symbol="X", body="FDA approves drug"))

    def test_no_text(self):
        c = NewsKeywordCondition(keywords=["earnings"])
        assert not evaluate(c, MarketSnapshot(symbol="X"))


class TestIndicatorCross:
    def test_golden_cross(self):
        c = IndicatorCrossCondition(fast="sma_20", slow="sma_50", direction="above")
        s = MarketSnapshot(
            symbol="AAPL",
            indicators={"sma_20": 101.0, "sma_50": 100.0},
            prev_indicators={"sma_20": 99.0, "sma_50": 100.0},
        )
        assert evaluate(c, s)

    def test_death_cross(self):
        c = IndicatorCrossCondition(fast="sma_20", slow="sma_50", direction="below")
        s = MarketSnapshot(
            symbol="AAPL",
            indicators={"sma_20": 99.0, "sma_50": 100.0},
            prev_indicators={"sma_20": 101.0, "sma_50": 100.0},
        )
        assert evaluate(c, s)

    def test_no_cross_when_still_above(self):
        c = IndicatorCrossCondition(fast="sma_20", slow="sma_50", direction="above")
        s = MarketSnapshot(
            symbol="AAPL",
            indicators={"sma_20": 102.0, "sma_50": 100.0},
            prev_indicators={"sma_20": 101.0, "sma_50": 100.0},
        )
        assert not evaluate(c, s)

    def test_first_bar_falls_back_to_level(self):
        c = IndicatorCrossCondition(fast="sma_20", slow="sma_50", direction="above")
        s = MarketSnapshot(symbol="AAPL", indicators={"sma_20": 102.0, "sma_50": 100.0})
        r = evaluate(c, s)
        assert r.matched
        assert "level" in r.reason


class TestTime:
    def test_hour_only(self):
        c = TimeCondition(hour_utc=14)
        assert evaluate(c, MarketSnapshot(symbol="X", utc_hour=14))
        assert not evaluate(c, MarketSnapshot(symbol="X", utc_hour=15))

    def test_hour_and_minute(self):
        c = TimeCondition(hour_utc=13, minute_utc=30)
        assert evaluate(c, MarketSnapshot(symbol="X", utc_hour=13, utc_minute=30))
        assert not evaluate(c, MarketSnapshot(symbol="X", utc_hour=13, utc_minute=31))


class TestComposite:
    def test_and_both_match(self):
        c = parse_condition({
            "kind": "and",
            "children": [
                {"kind": "price_move", "threshold_pct": 0.02},
                {"kind": "volume_spike", "multiplier": 2.0},
            ],
        })
        s = MarketSnapshot(
            symbol="AAPL", last_price=103.0, ref_price=100.0,
            volume_now=3_000_000, avg_volume=1_000_000,
        )
        assert evaluate(c, s)

    def test_and_one_fails(self):
        c = parse_condition({
            "kind": "and",
            "children": [
                {"kind": "price_move", "threshold_pct": 0.05},
                {"kind": "volume_spike", "multiplier": 2.0},
            ],
        })
        s = MarketSnapshot(
            symbol="AAPL", last_price=103.0, ref_price=100.0,
            volume_now=3_000_000, avg_volume=1_000_000,
        )
        assert not evaluate(c, s)

    def test_or_any_match(self):
        c = parse_condition({
            "kind": "or",
            "children": [
                {"kind": "price_move", "threshold_pct": 0.05},
                {"kind": "volume_spike", "multiplier": 2.0},
            ],
        })
        s = MarketSnapshot(
            symbol="AAPL", last_price=103.0, ref_price=100.0,
            volume_now=3_000_000, avg_volume=1_000_000,
        )
        assert evaluate(c, s)


class TestRequiredHistoryDays:
    def test_volume_spike_uses_avg_window(self):
        assert required_history_days(VolumeSpikeCondition(multiplier=2.0, avg_window_days=30)) == 30

    def test_indicator_cross_extracts_window(self):
        c = IndicatorCrossCondition(fast="sma_20", slow="sma_50")
        assert required_history_days(c) == 50

    def test_composite_takes_max(self):
        c = CompositeCondition(
            kind="and",
            children=[
                VolumeSpikeCondition(multiplier=2.0, avg_window_days=10),
                IndicatorCrossCondition(fast="ema_20", slow="ema_50"),
            ],
        )
        assert required_history_days(c) == 50

    def test_default(self):
        assert required_history_days(PriceMoveCondition(threshold_pct=0.03)) == 1
