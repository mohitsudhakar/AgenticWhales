"""Coverage for llm_clients/factory.py provider dispatch, recipes.py validators
(timeframes + trigger conditions), and dataflows/x_trades.py helpers + the
injectable-LLM trade-rec extractor. No network, no real model.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agenticwhales.llm_clients.factory import create_llm_client


# ===========================================================================
# factory.create_llm_client provider dispatch
# ===========================================================================

@pytest.mark.parametrize("provider,cls", [
    ("openai", "OpenAIClient"),
    ("deepseek", "OpenAIClient"),
    ("xai", "OpenAIClient"),
    ("anthropic", "AnthropicClient"),
    ("google", "GoogleClient"),
    ("azure", "AzureOpenAIClient"),
])
def test_create_llm_client_dispatch(provider, cls):
    client = create_llm_client(provider, "some-model", None)
    assert type(client).__name__ == cls


def test_create_llm_client_unsupported_raises():
    with pytest.raises(ValueError):
        create_llm_client("not-a-provider", "m", None)


# ===========================================================================
# recipes validators
# ===========================================================================

def test_validate_timeframes():
    from agenticwhales import recipes
    assert recipes._validate_timeframes(None) == ["1d"]
    assert recipes._validate_timeframes([]) == ["1d"]
    # a single string is wrapped
    out = recipes._validate_timeframes("1d")
    assert out == ["1d"]
    # invalid entries dropped; dedupes; falls back to 1d if nothing valid
    assert recipes._validate_timeframes(["garbage", "nope"]) == ["1d"]


def test_validate_timeframes_keeps_valid_unique():
    from agenticwhales import recipes
    from agenticwhales.dag import CANONICAL_TIMEFRAMES
    tf = CANONICAL_TIMEFRAMES[0]
    out = recipes._validate_timeframes([tf, tf.upper(), "garbage"])
    assert out == [tf]  # dedup + case-insensitive


def test_validate_trigger_conditions_empty():
    from agenticwhales import recipes
    assert recipes._validate_trigger_conditions(None) is None
    assert recipes._validate_trigger_conditions({}) is None
    assert recipes._validate_trigger_conditions("") is None


def test_validate_trigger_conditions_parses(monkeypatch):
    from agenticwhales import recipes
    import agenticwhales.triggers as triggers
    parsed = SimpleNamespace(model_dump=lambda mode: {"kind": "price_above", "value": 100})
    monkeypatch.setattr(triggers, "parse_condition", lambda raw: parsed)
    out = recipes._validate_trigger_conditions({"kind": "price_above", "value": 100})
    assert out == {"kind": "price_above", "value": 100}


# ===========================================================================
# x_trades helpers
# ===========================================================================

def test_clamp_conviction():
    from agenticwhales.dataflows import x_trades as xt
    assert xt._clamp_conviction(0.5) == 0.5
    assert xt._clamp_conviction(2.0) == 1.0
    assert xt._clamp_conviction(-1.0) == 0.0
    assert xt._clamp_conviction("garbage") == 0.5
    assert xt._clamp_conviction(float("nan")) == 0.5


def test_normalize_action():
    from agenticwhales.dataflows import x_trades as xt
    assert xt._normalize_action("BUY") == "buy"
    assert xt._normalize_action("weird") == "hold"
    assert xt._normalize_action(None) == "hold"


def test_default_http_get(monkeypatch):
    from agenticwhales.dataflows import x_trades as xt
    import requests

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [1, 2]}

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
    assert xt._default_http_get("http://x", {}, {}) == {"data": [1, 2]}


# ===========================================================================
# x_trades.extract_trade_recs (injectable LLM)
# ===========================================================================

def _llm(content):
    return SimpleNamespace(invoke=lambda msgs: SimpleNamespace(content=content))


def test_extract_trade_recs_empty_tweets():
    from agenticwhales.dataflows.x_trades import extract_trade_recs
    assert extract_trade_recs("trader", []) == []


def test_extract_trade_recs_normalizes():
    from agenticwhales.dataflows.x_trades import extract_trade_recs
    payload = json.dumps({"recommendations": [
        {"ticker": "$aapl", "action": "BUY", "conviction": 0.8,
         "rationale": "earnings beat", "timeframe": "swing"},
        {"ticker": "", "action": "sell"},          # no ticker → dropped
        "not-a-dict",                              # skipped
    ]})
    recs = extract_trade_recs("trader", [{"id": "1", "text": "AAPL to the moon"}],
                              llm=_llm(payload))
    assert len(recs) == 1
    assert recs[0] == {"ticker": "AAPL", "action": "buy", "conviction": 0.8,
                       "rationale": "earnings beat", "timeframe": "swing"}


def test_extract_trade_recs_bad_json_raises():
    from agenticwhales.dataflows.x_trades import extract_trade_recs, XTradesError

    def _boom(msgs):
        raise RuntimeError("model error")
    llm = SimpleNamespace(invoke=_boom)
    with pytest.raises(XTradesError):
        extract_trade_recs("trader", [{"id": "1", "text": "x"}], llm=llm)


def test_extract_trade_recs_no_recommendations_key():
    from agenticwhales.dataflows.x_trades import extract_trade_recs
    recs = extract_trade_recs("trader", [{"id": "1", "text": "x"}],
                              llm=_llm(json.dumps({"other": []})))
    assert recs == []
