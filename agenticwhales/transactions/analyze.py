"""LLM 4-lens behavioral analysis of computed metrics.

Port of robinhood-analyzer/lib/analyze.ts. The analysis system prompt and
the compact-sample / metrics user prompt are reused; the LLM call is routed
through ``agenticwhales.llm_clients`` per the project invariant. Output is
educational, not fiduciary advice.
"""

from __future__ import annotations

from typing import List, Optional

from agenticwhales.llm_clients import create_llm_client

from .extract import _invoke_llm, parse_json_loose
from .models import Analysis, AnalysisSection, Metrics, Transaction

# Reused verbatim from analyze.ts ANALYSIS_SYSTEM.
ANALYSIS_SYSTEM = """You are a panel of experts delivering a candid, personalized review of an individual's trading behavior, based ONLY on the metrics and sample transactions provided. You combine four lenses:

1. FINANCIAL: portfolio construction, risk, diversification, concentration, realized P&L, fees, position sizing, options leverage.
2. PSYCHOLOGICAL: behavioral-finance patterns — loss aversion, disposition effect, recency bias, FOMO, overconfidence, revenge trading, herding, anchoring. Tie each to specific evidence in the data.
3. HUMAN: empathetic coaching — money is emotional. Acknowledge effort and intent, frame growth without shame, respect that real money and real feelings are involved.
4. GLOBAL/MACRO: situate the behavior in the wider market and economic context where relevant, and note macro risks the portfolio is exposed to.

You are direct and specific, never generic. You cite the actual numbers. You do NOT give financial advice as a fiduciary; you provide educational analysis and ideas to consider. Avoid hype. Be honest about losses.

Return ONLY a JSON object with EXACTLY this shape:
{
  "headline": "one punchy sentence capturing the overall picture",
  "investorArchetype": "a vivid 2-5 word label, e.g. 'Disciplined Dividend Builder' or 'Momentum-Chasing Day Trader'",
  "riskScore": 0-100 integer (higher = more risk-taking),
  "disciplineScore": 0-100 integer (higher = more disciplined),
  "diversificationScore": 0-100 integer (higher = better diversified),
  "sections": [
    {"title": "Financial Perspective", "summary": "2-3 sentences", "points": ["specific insight with numbers", ...]},
    {"title": "Psychological Perspective", "summary": "...", "points": [...]},
    {"title": "Human Perspective", "summary": "...", "points": [...]},
    {"title": "Global & Macro Perspective", "summary": "...", "points": [...]}
  ],
  "suggestions": ["concrete, prioritized, actionable suggestion", ...],
  "habitsToKeep": ["positive habit observed in the data", ...],
  "habitsToChange": ["risky/limiting habit observed in the data", ...],
  "closingReflection": "a warm, motivating 2-3 sentence close"
}
Each section should have 3-5 points. Provide 4-7 suggestions. All claims must be grounded in the supplied data."""

MAX_ATTEMPTS = 3


def _clamp(n) -> int:
    try:
        v = float(n)
    except (TypeError, ValueError):
        return 50
    if v != v:  # NaN
        return 50
    return max(0, min(100, round(v)))


def _arr(v) -> List[str]:
    return [str(x) for x in v] if isinstance(v, list) else []


def _normalize_analysis(a: dict) -> Analysis:
    """Port of normalizeAnalysis(); maps camelCase JSON -> snake_case model."""
    if not isinstance(a, dict):
        a = {}
    sections = []
    for s in a.get("sections", []) if isinstance(a.get("sections"), list) else []:
        if isinstance(s, dict):
            sections.append(
                AnalysisSection(
                    title=str(s.get("title", "")),
                    summary=str(s.get("summary", "")),
                    points=_arr(s.get("points")),
                )
            )
    return Analysis(
        headline=str(a.get("headline", "Analysis complete.")),
        investor_archetype=str(a.get("investorArchetype", "Investor")),
        risk_score=_clamp(a.get("riskScore")),
        discipline_score=_clamp(a.get("disciplineScore")),
        diversification_score=_clamp(a.get("diversificationScore")),
        sections=sections,
        suggestions=_arr(a.get("suggestions")),
        habits_to_keep=_arr(a.get("habitsToKeep")),
        habits_to_change=_arr(a.get("habitsToChange")),
        closing_reflection=str(a.get("closingReflection", "")),
    )


def _build_user_prompt(metrics: Metrics, sample: List[Transaction]) -> str:
    import json

    compact = [
        {"d": t.date, "ty": t.type, "s": t.symbol, "q": t.quantity, "p": t.price, "a": t.amount}
        for t in sample[:60]
    ]
    return (
        "Here is the computed portfolio data. Analyze it.\n\n"
        f"METRICS:\n{json.dumps(metrics.model_dump(), indent=2)}\n\n"
        "SAMPLE TRANSACTIONS (up to 60, abbreviated keys "
        "d=date ty=type s=symbol q=qty p=price a=amount):\n"
        f"{json.dumps(compact)}\n\n"
        "Produce the JSON analysis now."
    )


def generate_analysis(
    metrics: Metrics,
    sample: List[Transaction],
    *,
    llm=None,
    provider: str = "openai",
    model: str = "gpt-5.4",
    base_url: Optional[str] = None,
) -> Analysis:
    """Generate the 4-lens behavioral analysis.

    The LLM is injectable via ``llm`` for tests. Retries transient
    empty/parse failures up to ``MAX_ATTEMPTS`` before giving up.
    """
    if llm is None:
        llm = create_llm_client(provider=provider, model=model, base_url=base_url).get_llm()

    user = _build_user_prompt(metrics, sample)
    last_err: Optional[Exception] = None
    for _ in range(MAX_ATTEMPTS):
        try:
            raw = _invoke_llm(llm, ANALYSIS_SYSTEM, user)
            return _normalize_analysis(parse_json_loose(raw))
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise last_err if last_err else RuntimeError("analysis failed")
