"""Coverage for agenticwhales/triggers.py primitive evaluation +
required_history_days across every condition kind. Pure model logic, no I/O.
"""

from __future__ import annotations

import pytest

from agenticwhales import triggers as tg
from agenticwhales.triggers import (
    CompositeCondition,
    IndicatorCrossCondition,
    MarketSnapshot,
    NewsKeywordCondition,
    PriceMoveCondition,
    TimeCondition,
    VolumeSpikeCondition,
    evaluate,
    required_history_days,
)


# ===========================================================================
# price move
# ===========================================================================

def test_price_move_up_match():
    c = PriceMoveCondition(threshold_pct=0.03, direction="up")
    snap = MarketSnapshot("AAPL", last_price=110.0, ref_price=100.0)
    assert evaluate(c, snap).matched is True


def test_price_move_missing_data():
    c = PriceMoveCondition(threshold_pct=0.03, direction="either")
    assert evaluate(c, MarketSnapshot("AAPL", last_price=None, ref_price=None)).matched is False


def test_price_move_wrong_direction():
    c = PriceMoveCondition(threshold_pct=0.03, direction="up")
    snap = MarketSnapshot("AAPL", last_price=90.0, ref_price=100.0)  # went down
    assert evaluate(c, snap).matched is False


def test_price_move_below_threshold():
    c = PriceMoveCondition(threshold_pct=0.10, direction="either")
    snap = MarketSnapshot("AAPL", last_price=101.0, ref_price=100.0)  # only 1%
    assert evaluate(c, snap).matched is False


# ===========================================================================
# volume spike
# ===========================================================================

def test_volume_spike_match_and_miss():
    c = VolumeSpikeCondition(multiplier=3.0)
    assert evaluate(c, MarketSnapshot("AAPL", volume_now=400, avg_volume=100)).matched is True
    assert evaluate(c, MarketSnapshot("AAPL", volume_now=200, avg_volume=100)).matched is False
    assert evaluate(c, MarketSnapshot("AAPL", volume_now=None, avg_volume=None)).matched is False


# ===========================================================================
# news keyword
# ===========================================================================

def test_news_keyword_match():
    c = NewsKeywordCondition(keywords=["merger", "buyout"])
    assert evaluate(c, MarketSnapshot("AAPL", headline="Big MERGER news")).matched is True
    assert evaluate(c, MarketSnapshot("AAPL", headline="quiet day")).matched is False
    assert evaluate(c, MarketSnapshot("AAPL", )).matched is False  # no text


# ===========================================================================
# indicator cross
# ===========================================================================

def test_indicator_cross_level_and_cross():
    c = IndicatorCrossCondition(fast="ema_10", slow="ema_50", direction="above")
    # first bar, level only
    level = MarketSnapshot("AAPL", indicators={"ema_10": 5, "ema_50": 3})
    assert evaluate(c, level).matched is True
    # actual cross
    crossed = MarketSnapshot("AAPL", indicators={"ema_10": 5, "ema_50": 3},
                             prev_indicators={"ema_10": 2, "ema_50": 3})
    assert evaluate(c, crossed).matched is True
    # no cross
    flat = MarketSnapshot("AAPL", indicators={"ema_10": 5, "ema_50": 3},
                          prev_indicators={"ema_10": 4, "ema_50": 3})
    assert evaluate(c, flat).matched is False


def test_indicator_cross_missing():
    c = IndicatorCrossCondition(fast="ema_10", slow="ema_50")
    assert evaluate(c, MarketSnapshot("AAPL", indicators={})).matched is False


# ===========================================================================
# time
# ===========================================================================

def test_time_condition():
    c = TimeCondition(hour_utc=14, minute_utc=30)
    assert evaluate(c, MarketSnapshot("AAPL", utc_hour=14, utc_minute=30)).matched is True
    assert evaluate(c, MarketSnapshot("AAPL", utc_hour=15, utc_minute=30)).matched is False
    assert evaluate(c, MarketSnapshot("AAPL", utc_hour=None)).matched is False


# ===========================================================================
# composite
# ===========================================================================

def test_composite_and_or():
    p = PriceMoveCondition(threshold_pct=0.03, direction="up")
    v = VolumeSpikeCondition(multiplier=3.0)
    snap = MarketSnapshot("AAPL", last_price=110.0, ref_price=100.0, volume_now=400, avg_volume=100)
    assert evaluate(CompositeCondition(kind="and", children=[p, v]), snap).matched is True
    bad = MarketSnapshot("AAPL", last_price=110.0, ref_price=100.0, volume_now=100, avg_volume=100)
    assert evaluate(CompositeCondition(kind="and", children=[p, v]), bad).matched is False
    assert evaluate(CompositeCondition(kind="or", children=[p, v]), bad).matched is True


# ===========================================================================
# required_history_days
# ===========================================================================

def test_required_history_days():
    assert required_history_days(PriceMoveCondition(threshold_pct=0.03)) == 1
    assert required_history_days(VolumeSpikeCondition(multiplier=2.0, avg_window_days=30)) == 30
    assert required_history_days(IndicatorCrossCondition(fast="ema_10", slow="ema_200")) == 200
    assert required_history_days(IndicatorCrossCondition(fast="macd", slow="signal")) == 50
    comp = CompositeCondition(kind="and", children=[
        VolumeSpikeCondition(multiplier=2.0, avg_window_days=15),
        PriceMoveCondition(threshold_pct=0.03),
    ])
    assert required_history_days(comp) == 15
