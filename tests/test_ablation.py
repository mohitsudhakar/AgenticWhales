"""Tests for `agenticwhales.ablation` — Phase 2 deliverable #8.

The citation-proxy is deterministic, so we can assert on numeric scores.
"""

from __future__ import annotations

import pytest

from agenticwhales import ablation


def _session(report_sections: dict, pm_text: str) -> dict:
    return {
        "id": "sess-test",
        "report_sections": {**report_sections, "final_trade_decision": pm_text},
        "final_trade_decision": pm_text,
    }


# ---------------------------------------------------------------------------
# Basic shape
# ---------------------------------------------------------------------------

class TestShape:
    def test_no_pm_decision_returns_error(self):
        result = ablation.explain_session({"id": "x", "report_sections": {}})
        assert "error" in result

    def test_contributions_sorted_descending(self):
        sections = {
            "market_report": "RSI 62 momentum strong moving averages diverging upside",
            "news_report":   "Earnings beat by 12% guidance raised guidance",
            "fundamentals_report": "P/E 18 cash flow stable margin expansion",
        }
        pm = (
            "Strong buy here. The technical analysis shows RSI 62 momentum "
            "diverging on the upside with moving averages turning positive. "
            "Combined with steady fundamentals (margin expansion remains intact) "
            "and the news beat, conviction is high. "
            "The market analyst noted the diverging strength."
        )
        result = ablation.explain_session(_session(sections, pm))
        contribs = result["contributions"]
        # Sorted by score descending.
        scores = [c["score"] for c in contribs]
        assert scores == sorted(scores, reverse=True)
        # Market report should top the list (we cited it the most + anchored on it).
        assert result["top_section"] == "market_report"


# ---------------------------------------------------------------------------
# Silent-section detection
# ---------------------------------------------------------------------------

class TestSilent:
    def test_unmentioned_section_marked_silent(self):
        sections = {
            "market_report":   "RSI MACD bollinger ATR support resistance",
            "news_report":     "Earnings beat guidance raised CEO comments",
            "sentiment_report": "Reddit chatter is bullish on AI hype",
        }
        # PM cites market_report by name + reuses RSI + MACD; news cited too.
        # Sentiment is completely ignored.
        pm = (
            "The market analyst flagged RSI and MACD diverging on the upside. "
            "The news beat from earnings reinforces the technical signal. "
            "Rating: Buy."
        )
        result = ablation.explain_session(_session(sections, pm))
        assert "sentiment_report" in result["silent_sections"]
        assert "market_report" not in result["silent_sections"]


# ---------------------------------------------------------------------------
# Number carryover
# ---------------------------------------------------------------------------

class TestNumbers:
    def test_number_carryover_adds_to_score(self):
        # Identical lexical content but one PM reuses the numbers and the
        # other doesn't.
        analyst = "Revenue grew 12.4% to $543M with a P/E of 18.2."
        pm_with_nums = "Revenue grew 12.4% to $543M is the headline. P/E 18.2 still cheap."
        pm_without   = "Revenue growth was solid. Multiple looks cheap."
        c_with = ablation._score_one("fundamentals_report", analyst, pm_with_nums)
        c_without = ablation._score_one("fundamentals_report", analyst, pm_without)
        assert c_with.number_carryover > 0
        assert c_with.score >= c_without.score


# ---------------------------------------------------------------------------
# Anchor matching
# ---------------------------------------------------------------------------

class TestAnchor:
    def test_anchor_keywords_register_hits(self):
        analyst = "any text"
        pm = ("The bull case rests on growth. The bear case warns. "
              "The market analyst noted technical support holding.")
        c = ablation._score_one("bull_history", analyst, pm)
        assert c.anchor_hits >= 1  # "bull case" matched
        c2 = ablation._score_one("market_report", analyst, pm)
        # "market analyst" + "technical" + "support" all match the anchor list.
        assert c2.anchor_hits >= 3
