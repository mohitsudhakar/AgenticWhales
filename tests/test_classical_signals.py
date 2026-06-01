"""Coverage for agenticwhales/classical.py pure signal helpers (edge branches)
and the memory-log meta sidecar loader."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from agenticwhales import classical as cl


def _series(values):
    return pd.Series([float(v) for v in values])


# ===========================================================================
# momentum_signal
# ===========================================================================

def test_momentum_insufficient():
    assert cl.momentum_signal(_series(range(10))).notes == "insufficient history"


def test_momentum_up():
    sig = cl.momentum_signal(_series(range(1, 301)))
    assert sig.direction == 1 and sig.strength > 0


def test_momentum_zero_reference():
    vals = list(range(1, 301))
    vals[300 - (cl.MOMENTUM_LOOKBACK + cl.MOMENTUM_SKIP)] = 0  # p_old <= 0
    sig = cl.momentum_signal(_series(vals))
    assert sig.direction == 0 and "reference" in sig.notes


# ===========================================================================
# bollinger_signal
# ===========================================================================

def test_bollinger_insufficient():
    assert cl.bollinger_signal(_series(range(5))).notes == "insufficient history"


def test_bollinger_zero_std():
    assert cl.bollinger_signal(_series([100] * 30)).notes == "zero stdev"


def test_bollinger_above_below_inside():
    base = [100 + (i % 3) for i in range(30)]
    above = base[:-1] + [200]
    below = base[:-1] + [0]
    assert cl.bollinger_signal(_series(above)).direction == -1
    assert cl.bollinger_signal(_series(below)).direction == 1
    assert cl.bollinger_signal(_series(base)).direction == 0


# ===========================================================================
# trend_signal
# ===========================================================================

def test_trend_insufficient():
    assert "insufficient" in cl.trend_signal(_series(range(10))).notes


def test_trend_up():
    sig = cl.trend_signal(_series(range(1, 301)))
    assert sig.direction == 1


# ===========================================================================
# atr_value + vol_regime_multiplier
# ===========================================================================

def test_atr_value_insufficient_and_normal():
    s = _series(range(5))
    assert cl.atr_value(s, s, s) == 0.0
    n = 60
    high = _series([100 + i for i in range(n)])
    low = _series([99 + i for i in range(n)])
    close = _series([99.5 + i for i in range(n)])
    assert cl.atr_value(high, low, close) > 0


def test_vol_regime_insufficient():
    s = _series(range(10))
    mult, note = cl.vol_regime_multiplier(s, s, s)
    assert mult == 1.0 and "insufficient" in note


def test_vol_regime_normal():
    n = cl.ATR_LOOKBACK_FOR_PERCENTILE + cl.ATR_WINDOW + 5
    high = _series([100 + (i % 7) for i in range(n)])
    low = _series([98 + (i % 7) for i in range(n)])
    close = _series([99 + (i % 7) for i in range(n)])
    mult, note = cl.vol_regime_multiplier(high, low, close)
    assert 0.5 <= mult <= 1.0


# ===========================================================================
# _rating_for + _safe_pct
# ===========================================================================

def test_rating_for_extremes():
    from agenticwhales.agents.schemas import PortfolioRating
    assert cl._rating_for(1.0) != PortfolioRating.SELL  # high score → bullish
    assert cl._rating_for(-1.0) == PortfolioRating.SELL  # below all cutoffs


def test_safe_pct():
    assert cl._safe_pct(10, 0) == 0.0
    assert cl._safe_pct(10, 2) == 5.0


# ===========================================================================
# TradingMemoryLog._load_meta sidecar
# ===========================================================================

def test_load_meta_reads_partial_sidecar(tmp_path):
    from agenticwhales.agents.utils.memory import TradingMemoryLog
    log_path = tmp_path / "mem.md"
    log_path.write_text("# log\n")
    meta_path = tmp_path / "mem.md.meta.json"
    meta_path.write_text(json.dumps({"entries": {"k": {"layer": "deep"}}}))
    log = TradingMemoryLog({"memory_log_path": str(log_path)})
    # partial sidecar is read; missing keys get defaulted
    assert log._meta["entries"]["k"]["layer"] == "deep"
    assert log._meta["promotions"] == []


def test_load_meta_corrupt_sidecar_returns_empty(tmp_path):
    from agenticwhales.agents.utils.memory import TradingMemoryLog
    log_path = tmp_path / "mem.md"
    log_path.write_text("# log\n")
    (tmp_path / "mem.md.meta.json").write_text("{ not valid json")
    log = TradingMemoryLog({"memory_log_path": str(log_path)})
    assert log._meta["entries"] == {}
