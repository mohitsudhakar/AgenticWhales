"""Unit tests for the X (Twitter) trade-recommendation dataflow vendor + tool.

All HTTP is mocked via the injectable ``http_get`` callable, and the LLM
extraction step uses an injected fake llm, so there is no live network and no
API key / model credential is required.
"""

from __future__ import annotations

import pytest

from agenticwhales.dataflows.x_trades import (
    XTradesError,
    extract_trade_recs,
    fetch_user_tweets,
    get_x_trade_recs,
)

pytestmark = pytest.mark.unit


_USER_PAYLOAD = {"data": {"id": "12345", "username": "tradeguru", "name": "Trade Guru"}}

_TWEETS_PAYLOAD = {
    "data": [
        {
            "id": "1",
            "text": "Loading up on $NVDA into earnings, this is a screaming buy.",
            "created_at": "2024-05-20T10:00:00.000Z",
            "public_metrics": {"like_count": 120, "retweet_count": 30},
        },
        {
            "id": "2",
            "text": "Trimming AAPL here, momentum is fading.",
            "created_at": "2024-05-21T11:00:00.000Z",
            "public_metrics": {"like_count": 40, "retweet_count": 5},
        },
    ]
}


def _stub(user_payload=_USER_PAYLOAD, tweets_payload=_TWEETS_PAYLOAD, capture=None):
    """Return an http_get stub that routes by URL: the /users/by/username/
    lookup returns the user payload; the /users/:id/tweets call returns tweets.
    """

    def _get(url, headers, params):
        if capture is not None:
            capture.setdefault("calls", []).append({"url": url, "headers": headers, "params": params})
        if "/users/by/username/" in url:
            return user_payload
        if "/tweets" in url:
            return tweets_payload
        raise AssertionError(f"unexpected url {url}")

    return _get


class _FakeLLM:
    """Minimal stand-in for a LangChain chat model.

    Records the messages it was invoked with and returns a canned response
    object exposing ``.content``.
    """

    def __init__(self, content):
        self._content = content
        self.last_messages = None

    def invoke(self, messages):
        self.last_messages = messages

        class _Resp:
            content = self._content

        return _Resp()


_LLM_JSON = (
    '{"recommendations": ['
    '{"ticker": "$nvda", "action": "long", "conviction": 0.9, '
    '"rationale": "loading up into earnings", "timeframe": "earnings play"},'
    '{"ticker": "AAPL", "action": "trim", "conviction": 1.4, '
    '"rationale": "momentum fading", "timeframe": "swing"}'
    "]}"
)


class TestFetchTweets:
    def test_normalizes_tweet_records(self):
        tweets = fetch_user_tweets("tradeguru", http_get=_stub())
        assert len(tweets) == 2
        assert tweets[0]["id"] == "1"
        assert tweets[0]["like_count"] == 120
        assert tweets[0]["retweet_count"] == 30
        assert "NVDA" in tweets[0]["text"]

    def test_handle_at_prefix_stripped_in_url(self):
        capture = {}
        fetch_user_tweets("@tradeguru", http_get=_stub(capture=capture))
        lookup = capture["calls"][0]["url"]
        assert lookup.endswith("/users/by/username/tradeguru")

    def test_bearer_token_becomes_auth_header(self):
        capture = {}
        fetch_user_tweets("tradeguru", bearer_token="secrettoken", http_get=_stub(capture=capture))
        assert capture["calls"][0]["headers"]["Authorization"] == "Bearer secrettoken"

    def test_two_step_flow_uses_resolved_user_id(self):
        capture = {}
        fetch_user_tweets("tradeguru", http_get=_stub(capture=capture))
        # Second call is the tweets endpoint, scoped to the resolved id.
        tweets_url = capture["calls"][1]["url"]
        assert tweets_url.endswith("/users/12345/tweets")

    def test_max_results_clamped_in_params(self):
        capture = {}
        fetch_user_tweets("tradeguru", max_results=3, http_get=_stub(capture=capture))
        # X v2 minimum page size is 5.
        assert capture["calls"][1]["params"]["max_results"] == 5

    def test_empty_username_returns_empty(self):
        assert fetch_user_tweets("", http_get=_stub()) == []

    def test_unknown_user_raises(self):
        with pytest.raises(XTradesError):
            fetch_user_tweets("ghost", http_get=_stub(user_payload={"data": {}}))

    def test_http_failure_raises_vendor_error(self):
        def _boom(url, headers, params):
            raise ConnectionError("network down")

        with pytest.raises(XTradesError):
            fetch_user_tweets("tradeguru", http_get=_boom)

    def test_tweets_fetch_failure_raises_vendor_error(self):
        def _get(url, headers, params):
            if "/users/by/username/" in url:
                return _USER_PAYLOAD
            raise TimeoutError("slow")

        with pytest.raises(XTradesError):
            fetch_user_tweets("tradeguru", http_get=_get)


class TestExtractRecs:
    def test_extracts_and_normalizes(self):
        llm = _FakeLLM(_LLM_JSON)
        recs = extract_trade_recs("tradeguru", _TWEETS_PAYLOAD["data"], llm=llm)
        assert len(recs) == 2
        nvda = next(r for r in recs if r["ticker"] == "NVDA")
        # "$nvda" -> "NVDA", "long" -> "buy".
        assert nvda["action"] == "buy"
        assert nvda["conviction"] == 0.9
        assert nvda["timeframe"] == "earnings play"
        aapl = next(r for r in recs if r["ticker"] == "AAPL")
        # "trim" -> "sell", conviction 1.4 clamped to 1.0.
        assert aapl["action"] == "sell"
        assert aapl["conviction"] == 1.0

    def test_handles_code_fenced_json(self):
        llm = _FakeLLM("```json\n" + _LLM_JSON + "\n```")
        recs = extract_trade_recs("tradeguru", _TWEETS_PAYLOAD["data"], llm=llm)
        assert len(recs) == 2

    def test_empty_tweets_returns_empty_without_llm_call(self):
        llm = _FakeLLM("should not be used")
        recs = extract_trade_recs("tradeguru", [], llm=llm)
        assert recs == []
        assert llm.last_messages is None

    def test_records_without_ticker_dropped(self):
        llm = _FakeLLM('{"recommendations": [{"action": "buy", "conviction": 0.5}]}')
        recs = extract_trade_recs("tradeguru", _TWEETS_PAYLOAD["data"], llm=llm)
        assert recs == []

    def test_no_recommendations_key(self):
        llm = _FakeLLM('{"recommendations": []}')
        recs = extract_trade_recs("tradeguru", _TWEETS_PAYLOAD["data"], llm=llm)
        assert recs == []

    def test_llm_failure_raises_vendor_error(self):
        class _BoomLLM:
            def invoke(self, messages):
                raise RuntimeError("model exploded")

        with pytest.raises(XTradesError):
            extract_trade_recs("tradeguru", _TWEETS_PAYLOAD["data"], llm=_BoomLLM())


class TestReport:
    def test_report_contains_table_and_counts(self):
        report = get_x_trade_recs(
            "tradeguru", http_get=_stub(), llm=_FakeLLM(_LLM_JSON)
        )
        assert "X Trade Recommendations for @tradeguru" in report
        assert "buy: 1" in report
        assert "sell: 1" in report
        assert "NVDA" in report
        assert "AAPL" in report
        assert "| Ticker |" in report  # markdown table header
        # Sorted by conviction descending: AAPL (1.00, clamped) before NVDA (0.90).
        assert report.index("AAPL") < report.index("NVDA")

    def test_report_no_tweets(self):
        report = get_x_trade_recs(
            "silent", http_get=_stub(tweets_payload={"data": []}), llm=_FakeLLM(_LLM_JSON)
        )
        assert "No recent tweets found" in report

    def test_report_no_recs(self):
        report = get_x_trade_recs(
            "tradeguru", http_get=_stub(), llm=_FakeLLM('{"recommendations": []}')
        )
        assert "no explicit trade recommendations detected" in report

    def test_report_handles_fetch_error_gracefully(self):
        def _boom(url, headers, params):
            raise ConnectionError("down")

        report = get_x_trade_recs("tradeguru", http_get=_boom, llm=_FakeLLM(_LLM_JSON))
        assert "Data unavailable" in report

    def test_report_handles_extraction_error_gracefully(self):
        class _BoomLLM:
            def invoke(self, messages):
                raise RuntimeError("model exploded")

        report = get_x_trade_recs("tradeguru", http_get=_stub(), llm=_BoomLLM())
        assert "Extraction unavailable" in report

    def test_report_empty_username(self):
        report = get_x_trade_recs("", http_get=_stub(), llm=_FakeLLM(_LLM_JSON))
        assert "No username provided" in report


class TestRouting:
    def test_route_to_vendor_dispatches_to_x_api(self, monkeypatch):
        from agenticwhales.dataflows import interface

        captured = {}

        def _fake(username, max_results=50, **kwargs):
            captured["username"] = username
            captured["max_results"] = max_results
            return f"report for @{username}"

        monkeypatch.setitem(
            interface.VENDOR_METHODS["get_x_trade_recs"], "x_api", _fake
        )
        out = interface.route_to_vendor("get_x_trade_recs", "tradeguru", 10)
        assert out == "report for @tradeguru"
        assert captured == {"username": "tradeguru", "max_results": 10}

    def test_category_lookup(self):
        from agenticwhales.dataflows import interface

        assert interface.get_category_for_method("get_x_trade_recs") == "x_social"


class TestTool:
    def test_tool_wraps_output_in_external_data(self, monkeypatch):
        from agenticwhales.agents.utils import news_data_tools

        monkeypatch.setattr(
            news_data_tools,
            "route_to_vendor",
            lambda method, *a, **k: "## X Trade Recommendations for @tradeguru\n\nbody",
        )
        out = news_data_tools.get_x_trade_recs.invoke(
            {"username": "tradeguru", "max_results": 5}
        )
        assert "<external_data" in out
        assert "x_trade_recs" in out
        assert "X Trade Recommendations for @tradeguru" in out
