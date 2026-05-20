"""Ablation report — Phase 2 deliverable #8.

For any completed session, surface **which analyst was load-bearing** in
the PM's final decision. The Phase-1 stub returned a `{status: queued}`
placeholder; this module makes the report real.

Two approaches we considered:

  1. **Real ablation re-run.** Mask each analyst's report, re-run the PM
     LangGraph node, compute rating delta. Most rigorous, most expensive
     (one extra deep-model call per analyst per request).

  2. **Citation-proxy.** Read the PM's `investment_thesis` markdown,
     measure how heavily it cites each analyst's report (token overlap,
     section-name mentions, key-metric carryover). Cheap, deterministic,
     no extra LLM cost. Approximation, not measurement.

Phase 2 ships #2 (citation-proxy) for the immediate user-facing surface.
The endpoint already accepts a `live=true` flag — Phase 2.x will wire that
to a real re-run, gated behind a per-user spend confirmation.

The proxy compares:
  - **Lexical overlap**: count of distinctive tokens from each analyst
    report that show up in the PM's investment_thesis.
  - **Section anchor**: did the PM literally name the analyst's section
    (e.g. "as the news analyst noted", "the Quant Radar shows")?
  - **Number carryover**: numbers from the analyst report that appear in
    the PM's reasoning.

Each analyst gets a 0..1 score; the highest scorers are the load-bearing
contributors. Surfacing this lets users (and ops) see when the PM is
silently ignoring a whole section.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional


# Token noise we strip before counting overlaps — common English words +
# debate-template boilerplate.
_STOPWORDS = set("""
the of and a to in for is on it with as that this be by from at are or an
not was but they were has have can will may could should would which has
its their our we i you them than then so if when how what who whose where
also however thus therefore based according
""".split())

_NUMBER_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?")
_WORD_RE   = re.compile(r"[A-Za-z][A-Za-z0-9'-]+")


def _toks(text: str) -> List[str]:
    return [w.lower() for w in _WORD_RE.findall(text or "") if w.lower() not in _STOPWORDS]


def _nums(text: str) -> List[str]:
    return _NUMBER_RE.findall(text or "")


# Section name → human-readable anchor strings the PM might cite. The PM
# prompt references these by name in its templates, so anchor-matching is
# a strong signal.
_ANCHOR_KEYWORDS = {
    "market_report":       ["market analyst", "technical", "price action", "trend", "support", "resistance"],
    "sentiment_report":    ["social", "sentiment", "social media", "twitter", "reddit"],
    "news_report":         ["news", "headline", "geopolitical", "macroeconomic"],
    "fundamentals_report": ["fundamentals", "valuation", "earnings", "balance sheet"],
    "quant_radar":         ["quant", "radar", "volatility", "breakout", "momentum strength"],
    "bull_history":        ["bull case", "bull researcher"],
    "bear_history":        ["bear case", "bear researcher"],
}


@dataclass
class AnalystContribution:
    section: str
    score: float                # 0..1 composite
    lexical_overlap: float      # 0..1 — distinctive-token overlap
    number_carryover: float     # 0..1 — fraction of analyst-numbers appearing in PM
    anchor_hits: int            # # times the PM cited this section's anchors


def _score_one(section: str, analyst_text: str, pm_text: str) -> AnalystContribution:
    """Compute the citation-proxy score for one analyst section."""
    analyst_toks = set(_toks(analyst_text))
    pm_toks = set(_toks(pm_text))
    if not analyst_toks:
        # No analyst content (analyst didn't run / errored) → contribution 0.
        return AnalystContribution(section=section, score=0.0,
                                   lexical_overlap=0.0, number_carryover=0.0,
                                   anchor_hits=0)

    # Distinctive tokens: tokens unique to this analyst (don't appear in
    # every analyst's vocabulary). We weight by frequency in the analyst
    # text but cap to avoid one heavy word swamping the score.
    overlap = analyst_toks & pm_toks
    lex = min(1.0, len(overlap) / max(20, len(analyst_toks) // 8))

    # Number carryover: numbers in the analyst report that the PM reuses
    # verbatim.
    analyst_nums = set(_nums(analyst_text))
    pm_nums = set(_nums(pm_text))
    nums_overlap = analyst_nums & pm_nums
    num_carry = (len(nums_overlap) / len(analyst_nums)) if analyst_nums else 0.0

    # Anchor hits.
    pm_lower = (pm_text or "").lower()
    anchor_hits = sum(pm_lower.count(a) for a in _ANCHOR_KEYWORDS.get(section, []))
    anchor_score = min(1.0, anchor_hits / 3.0)

    # Weighted composite. Lexical drives most of the signal; anchors and
    # numbers are corroborating evidence.
    composite = 0.55 * lex + 0.25 * anchor_score + 0.20 * num_carry
    return AnalystContribution(
        section=section,
        score=round(composite, 3),
        lexical_overlap=round(lex, 3),
        number_carryover=round(num_carry, 3),
        anchor_hits=anchor_hits,
    )


def explain_session(session: Dict[str, Any]) -> Dict[str, Any]:
    """Build the ablation report for a completed session.

    The session dict has the standard shape produced by `web.runner.build_session`
    + populated `report_sections` and `final_trade_decision`. We pull each
    analyst's report, score its contribution to the PM, and rank.

    Returns a dict the UI renders directly:
      - `contributions`: list of `AnalystContribution` (sorted desc by score)
      - `top_section`: highest-scoring section
      - `silent_sections`: analysts that ran but the PM seemingly ignored
        (lexical overlap < 0.10 AND anchor_hits == 0)
      - `pm_summary`: short markdown summary
    """
    sections = (session.get("report_sections") or {}).copy()
    pm_text = sections.pop("final_trade_decision", "") or session.get("final_trade_decision", "") or ""
    if not pm_text:
        return {"error": "Session has no PM decision yet; ablation needs a completed run."}

    contributions: List[AnalystContribution] = []
    for sec_name, sec_text in sections.items():
        if sec_name == "trader_investment_plan":   # Trader is downstream of analysts, exclude.
            continue
        contributions.append(_score_one(sec_name, sec_text or "", pm_text))
    contributions.sort(key=lambda c: c.score, reverse=True)

    top = contributions[0] if contributions else None
    silent = [
        c.section for c in contributions
        if c.lexical_overlap < 0.10 and c.anchor_hits == 0 and c.score < 0.10
    ]

    summary_lines = []
    if top:
        summary_lines.append(
            f"**Load-bearing:** {top.section} (score {top.score:.2f}). "
            f"The PM's reasoning leans heaviest on this section."
        )
    if silent:
        summary_lines.append(
            f"**Silently ignored:** {', '.join(silent)}. "
            f"The PM's text doesn't measurably reuse these reports — "
            f"either they didn't matter or the PM dropped them."
        )

    return {
        "contributions": [asdict(c) for c in contributions],
        "top_section": top.section if top else None,
        "silent_sections": silent,
        "pm_summary": "\n\n".join(summary_lines),
        "method": "citation-proxy (deterministic). Full re-run with masked "
                  "analyst inputs is a Phase 2.x enhancement.",
    }
