"""Unit coverage for agenticwhales/portfolio.py — file-backed position store,
futures-aware related-symbol matching, and prompt-block formatting.

_PATH is redirected to a temp file so the user's real ~/.agenticwhales is
never touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agenticwhales import portfolio


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(portfolio, "_PATH", tmp_path / "portfolio.json")
    yield


# ---------------------------------------------------------------------------
# load_all / save_all
# ---------------------------------------------------------------------------

def test_load_all_missing_file_empty():
    assert portfolio.load_all() == {}


def test_load_all_bad_json_empty():
    portfolio._PATH.write_text("{ not valid json")
    assert portfolio.load_all() == {}


def test_load_all_non_dict_empty():
    portfolio._PATH.write_text("[1, 2, 3]")
    assert portfolio.load_all() == {}


def test_save_all_roundtrip_and_cleaning():
    portfolio.save_all({
        "aapl": {"qty": "10", "avg_cost": "150.5", "notes": " core long "},
        "  ": {"qty": 5},                       # blank symbol → dropped
        "nvda": {"qty": 0},                     # zero qty → flat, dropped
        "msft": "notadict",                     # non-dict → dropped
        "tsla": {"qty": "abc"},                 # unparseable qty → dropped
        "amzn": {"avg_cost": 100},              # no qty but has avg_cost → kept
    })
    data = portfolio.load_all()
    assert data["AAPL"] == {"qty": 10.0, "avg_cost": 150.5, "notes": "core long"}
    assert "NVDA" not in data and "MSFT" not in data and "TSLA" not in data
    assert data["AMZN"] == {"avg_cost": 100.0}


def test_save_all_bad_avg_cost_is_skipped():
    portfolio.save_all({"AAPL": {"qty": 10, "avg_cost": "notnum"}})
    assert portfolio.load_all()["AAPL"] == {"qty": 10.0}


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------

def test_get_present_and_absent():
    portfolio.save_all({"AAPL": {"qty": 10}})
    assert portfolio.get("aapl")["qty"] == 10.0
    assert portfolio.get("NVDA") is None
    assert portfolio.get("") is None


# ---------------------------------------------------------------------------
# _futures_root
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sym,root", [
    ("CL=F", "CL"), ("GC=F", "GC"), ("CLM26", "CL"), ("ESZ25", "ES"),
])
def test_futures_root_matches(sym, root):
    assert portfolio._futures_root(sym) == root


@pytest.mark.parametrize("sym", ["AAPL", "123=F", "CLZ", "", "CL99X"])
def test_futures_root_none(sym):
    assert portfolio._futures_root(sym) is None


# ---------------------------------------------------------------------------
# find_related
# ---------------------------------------------------------------------------

def test_find_related_exact_and_token_and_futures():
    portfolio.save_all({
        "NVDA": {"qty": 100},
        "NVDA $215 5/11/2026 CALL": {"qty": 5},   # option → token match
        "AAPL": {"qty": 10},                      # unrelated
        "CLM26": {"qty": 2},                      # futures dated
    })
    rel = portfolio.find_related("nvda")
    assert set(rel.keys()) == {"NVDA", "NVDA $215 5/11/2026 CALL"}

    # Continuous futures finds the dated contract by root.
    rel_cl = portfolio.find_related("CL=F")
    assert "CLM26" in rel_cl
    # Different root never crosses.
    assert "CLM26" not in portfolio.find_related("GC=F")


def test_find_related_empty_symbol():
    assert portfolio.find_related("") == {}


def test_find_related_skips_zero_qty():
    # save_all drops zero qty, so inject directly via the file.
    import json
    portfolio._PATH.write_text(json.dumps({"AAPL": {"qty": 0}}))
    assert portfolio.find_related("AAPL") == {}


# ---------------------------------------------------------------------------
# _net_side + _describe_one
# ---------------------------------------------------------------------------

def test_net_side_variants():
    assert portfolio._net_side({"A": {"qty": 5}}) == "LONG"
    assert portfolio._net_side({"A": {"qty": -5}}) == "SHORT"
    assert portfolio._net_side({"A": {"qty": 5}, "B": {"qty": -5}}) == "MIXED"
    assert portfolio._net_side({"A": {"qty": 0}}) is None


def test_describe_one():
    assert portfolio._describe_one("AAPL", {"qty": 0}) == ""
    long_s = portfolio._describe_one("AAPL", {"qty": 10, "avg_cost": 150, "notes": "core"})
    assert "LONG 10 units of AAPL" in long_s and "avg cost 150" in long_s and "core" in long_s
    short_s = portfolio._describe_one("AAPL", {"qty": -3})
    assert short_s.startswith("SHORT 3 units")


# ---------------------------------------------------------------------------
# format_for_prompt
# ---------------------------------------------------------------------------

def test_format_for_prompt_flat_and_empty_symbol():
    assert portfolio.format_for_prompt("") == ""
    assert portfolio.format_for_prompt("AAPL") == ""          # nothing held
    assert portfolio.format_for_prompt("AAPL", {"qty": 0}) == ""  # explicit flat


def test_format_for_prompt_long_block():
    portfolio.save_all({"AAPL": {"qty": 10, "avg_cost": 150}})
    block = portfolio.format_for_prompt("aapl")
    assert "USER'S CURRENT POSITION" in block
    assert "LONG" in block and "Express recommendations" in block
    assert "LONG 10 units of AAPL" in block


def test_format_for_prompt_short_vocab():
    portfolio.save_all({"AAPL": {"qty": -5}})
    block = portfolio.format_for_prompt("AAPL")
    assert "**SHORT**" in block and "Cover" in block


def test_format_for_prompt_mixed_vocab():
    portfolio.save_all({"NVDA": {"qty": 100}, "NVDA CALL": {"qty": -2}})
    block = portfolio.format_for_prompt("NVDA")
    assert "**MIXED**" in block


def test_format_for_prompt_explicit_position_arg():
    block = portfolio.format_for_prompt("AAPL", {"qty": 7, "avg_cost": 200})
    assert "LONG 7 units of AAPL" in block
