"""Coverage for the FinMem-style layered/scored-retrieval surface of
agenticwhales/agents/utils/memory.py — the parts test_memory_log.py doesn't
exercise: helpers, access-promotion, recent-performance block, importance/
recency scoring, scored context, and extended reflections.

All file I/O is under tmp_path; no LLM.
"""

from __future__ import annotations

import math

import pytest

from agenticwhales.agents.utils import memory as mem
from agenticwhales.agents.utils.memory import (
    TradingMemoryLog,
    _ACCESS_PROMOTION_THRESHOLD,
    _jaccard,
    _tokenize,
)


def _log(tmp_path):
    return TradingMemoryLog({"memory_log_path": str(tmp_path / "mem.md")})


def _resolve(log, ticker, date, decision="Rating: Buy\nGo long.",
             raw=0.05, alpha=0.02, days=5, reflection="Good call."):
    log.store_decision(ticker, date, decision)
    log.update_with_outcome(ticker, date, raw, alpha, days, reflection)


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------

def test_tokenize_drops_short_and_lowercases():
    # keeps len>2 tokens, lower-cased; "is"/"go" (len<=2) dropped
    assert _tokenize("The NVDA Call is GO") == {"the", "nvda", "call"}
    assert _tokenize("a I to") == set()  # all len<=2


def test_jaccard():
    assert _jaccard(set(), {"a"}) == 0.0
    assert _jaccard({"abc"}, {"abc"}) == 1.0
    assert _jaccard({"abc", "def"}, {"abc", "xyz"}) == pytest.approx(1 / 3)


def test_importance_from_outcome_range():
    assert TradingMemoryLog._importance_from_outcome(0.0) == 40
    assert TradingMemoryLog._importance_from_outcome(0.10) == 80
    assert TradingMemoryLog._importance_from_outcome(0.50) == 80   # clamped
    assert 40 <= TradingMemoryLog._importance_from_outcome(0.02) <= 80


def test_recency_score_decays_and_handles_bad_date():
    today = mem.datetime.utcnow().date().strftime("%Y-%m-%d")
    fresh = TradingMemoryLog._recency_score(today, "shallow", None)
    assert fresh == pytest.approx(1.0, abs=1e-6)  # 0 days → exp(0)=1
    old = TradingMemoryLog._recency_score("2000-01-01", "shallow", None)
    assert 0.0 <= old < fresh
    # boosted_date overrides entry_date
    boosted = TradingMemoryLog._recency_score("2000-01-01", "shallow", today)
    assert boosted == pytest.approx(1.0, abs=1e-6)
    # bad date → 0.0
    assert TradingMemoryLog._recency_score("not-a-date", "shallow", None) == 0.0


# ---------------------------------------------------------------------------
# get_recent_performance_block
# ---------------------------------------------------------------------------

def test_recent_performance_empty_when_no_history(tmp_path):
    assert _log(tmp_path).get_recent_performance_block("AAPL") == ""


def test_recent_performance_aggregates(tmp_path):
    log = _log(tmp_path)
    _resolve(log, "AAPL", "2024-01-02", raw=0.05, alpha=0.02)
    _resolve(log, "AAPL", "2024-01-09", raw=0.03, alpha=-0.01)
    block = log.get_recent_performance_block("AAPL", lookback=3)
    assert "RECENT PERFORMANCE" in block and "AAPL" in block
    assert "cumulative" in block


# ---------------------------------------------------------------------------
# _record_access promotion
# ---------------------------------------------------------------------------

def test_record_access_promotes_shallow_to_deep(tmp_path):
    log = _log(tmp_path)
    _resolve(log, "AAPL", "2024-01-02")
    promoted = False
    for _ in range(_ACCESS_PROMOTION_THRESHOLD):
        promoted = log._record_access("2024-01-02", "AAPL")
    assert promoted is True
    meta = log._entry_meta("2024-01-02", "AAPL")
    assert meta["layer"] == "deep"
    assert meta["boosted_date"] is not None
    assert log._meta["promotions"]  # a promotion was logged


# ---------------------------------------------------------------------------
# get_scored_context
# ---------------------------------------------------------------------------

def test_scored_context_empty(tmp_path):
    assert _log(tmp_path).get_scored_context("AAPL") == ""


def test_scored_context_returns_layered_sections(tmp_path):
    log = _log(tmp_path)
    _resolve(log, "AAPL", "2024-01-02", reflection="Strong momentum continuation.")
    _resolve(log, "NVDA", "2024-01-03", reflection="Cross-ticker AI tailwind.")
    out = log.get_scored_context("AAPL", top_k_per_layer=5,
                                 current_summary="momentum")
    assert "MEMORY LAYER" in out
    # same-ticker entry uses the full format (contains DECISION/REFLECTION text)
    assert "AAPL" in out


def test_scored_context_bumps_access(tmp_path):
    log = _log(tmp_path)
    _resolve(log, "AAPL", "2024-01-02")
    log.get_scored_context("AAPL")
    assert log._entry_meta("2024-01-02", "AAPL")["access_count"] >= 1


# ---------------------------------------------------------------------------
# extended reflection
# ---------------------------------------------------------------------------

def test_days_since_last_extended_none_then_value(tmp_path):
    log = _log(tmp_path)
    assert log.days_since_last_extended_reflection() is None
    log.store_extended_reflection("2024-01-02", "Quarterly retrospective.")
    val = log.days_since_last_extended_reflection()
    assert isinstance(val, int) and val >= 0


def test_store_extended_reflection_idempotent(tmp_path):
    log = _log(tmp_path)
    log.store_extended_reflection("2024-01-02", "First.")
    log.store_extended_reflection("2024-01-02", "Second (should be skipped).")
    text = log._log_path.read_text()
    assert text.count("_EXTENDED_") == 1


def test_store_extended_reflection_no_path():
    # No memory_log_path → no-op, no raise.
    TradingMemoryLog({}).store_extended_reflection("2024-01-02", "x")
