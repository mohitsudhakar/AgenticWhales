"""Pydantic schemas used by agents that produce structured output.

The framework's primary artifact is still prose: each agent's natural-language
reasoning is what users read in the saved markdown reports and what the
downstream agents read as context.  Structured output is layered onto the
three decision-making agents (Research Manager, Trader, Portfolio Manager)
so that:

- Their outputs follow consistent section headers across runs and providers
- Each provider's native structured-output mode is used (json_schema for
  OpenAI/xAI, response_schema for Gemini, tool-use for Anthropic)
- Schema field descriptions become the model's output instructions, freeing
  the prompt body to focus on context and the rating-scale guidance
- A render helper turns the parsed Pydantic instance back into the same
  markdown shape the rest of the system already consumes, so display,
  memory log, and saved reports keep working unchanged
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Shared rating types
# ---------------------------------------------------------------------------


class PortfolioRating(str, Enum):
    """5-tier rating used by the Research Manager and Portfolio Manager."""

    BUY = "Buy"
    OVERWEIGHT = "Overweight"
    HOLD = "Hold"
    UNDERWEIGHT = "Underweight"
    SELL = "Sell"


class TraderAction(str, Enum):
    """3-tier transaction direction used by the Trader.

    The Trader's job is to translate the Research Manager's investment plan
    into a concrete transaction proposal: should the desk execute a Buy, a
    Sell, or sit on Hold this round.  Position sizing and the nuanced
    Overweight / Underweight calls happen later at the Portfolio Manager.
    """

    BUY = "Buy"
    HOLD = "Hold"
    SELL = "Sell"


# ---------------------------------------------------------------------------
# Research Manager
# ---------------------------------------------------------------------------


class ResearchPlan(BaseModel):
    """Structured investment plan produced by the Research Manager.

    Hand-off to the Trader: the recommendation pins the directional view,
    the rationale captures which side of the bull/bear debate carried the
    argument, and the strategic actions translate that into concrete
    instructions the trader can execute against.
    """

    recommendation: PortfolioRating = Field(
        description=(
            "The investment recommendation. Exactly one of Buy / Overweight / "
            "Hold / Underweight / Sell. Reserve Hold for situations where the "
            "evidence on both sides is genuinely balanced; otherwise commit to "
            "the side with the stronger arguments."
        ),
    )
    rationale: str = Field(
        description=(
            "Conversational summary of the key points from both sides of the "
            "debate, ending with which arguments led to the recommendation. "
            "Speak naturally, as if to a teammate."
        ),
    )
    strategic_actions: str = Field(
        description=(
            "Concrete steps for the trader to implement the recommendation, "
            "including position sizing guidance consistent with the rating."
        ),
    )


def render_research_plan(plan: ResearchPlan) -> str:
    """Render a ResearchPlan to markdown for storage and the trader's prompt context."""
    return "\n".join([
        f"**Recommendation**: {plan.recommendation.value}",
        "",
        f"**Rationale**: {plan.rationale}",
        "",
        f"**Strategic Actions**: {plan.strategic_actions}",
    ])


# ---------------------------------------------------------------------------
# Trader
# ---------------------------------------------------------------------------


class TraderProposal(BaseModel):
    """Structured transaction proposal produced by the Trader.

    The trader reads the Research Manager's investment plan and the analyst
    reports, then turns them into a concrete transaction: what action to
    take, the reasoning that justifies it, and the practical levels for
    entry, stop-loss, and sizing.
    """

    action: TraderAction = Field(
        description="The transaction direction. Exactly one of Buy / Hold / Sell.",
    )
    reasoning: str = Field(
        description=(
            "The case for this action, anchored in the analysts' reports and "
            "the research plan. Two to four sentences."
        ),
    )
    entry_price: Optional[float] = Field(
        default=None,
        description="Optional entry price target in the instrument's quote currency.",
    )
    stop_loss: Optional[float] = Field(
        default=None,
        description="Optional stop-loss price in the instrument's quote currency.",
    )
    position_sizing: Optional[str] = Field(
        default=None,
        description="Optional sizing guidance, e.g. '5% of portfolio'.",
    )


def render_trader_proposal(proposal: TraderProposal) -> str:
    """Render a TraderProposal to markdown.

    The trailing ``FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL**`` line is
    preserved for backward compatibility with the analyst stop-signal text
    and any external code that greps for it.
    """
    parts = [
        f"**Action**: {proposal.action.value}",
        "",
        f"**Reasoning**: {proposal.reasoning}",
    ]
    if proposal.entry_price is not None:
        parts.extend(["", f"**Entry Price**: {proposal.entry_price}"])
    if proposal.stop_loss is not None:
        parts.extend(["", f"**Stop Loss**: {proposal.stop_loss}"])
    if proposal.position_sizing:
        parts.extend(["", f"**Position Sizing**: {proposal.position_sizing}"])
    parts.extend([
        "",
        f"FINAL TRANSACTION PROPOSAL: **{proposal.action.value.upper()}**",
    ])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Portfolio Manager
# ---------------------------------------------------------------------------


class PortfolioDecision(BaseModel):
    """Structured output produced by the Portfolio Manager.

    The model fills every field as part of its primary LLM call; no separate
    extraction pass is required. Field descriptions double as the model's
    output instructions, so the prompt body only needs to convey context and
    the rating-scale guidance.
    """

    rating: PortfolioRating = Field(
        description=(
            "The final position rating. Exactly one of Buy / Overweight / Hold / "
            "Underweight / Sell, picked based on the analysts' debate."
        ),
    )
    executive_summary: str = Field(
        description=(
            "A concise action plan covering entry strategy, position sizing, "
            "key risk levels, and time horizon. Two to four sentences."
        ),
    )
    investment_thesis: str = Field(
        description=(
            "Detailed reasoning anchored in specific evidence from the analysts' "
            "debate. If prior lessons are referenced in the prompt context, "
            "incorporate them; otherwise rely solely on the current analysis."
        ),
    )
    price_target: Optional[float] = Field(
        default=None,
        description="Optional target price in the instrument's quote currency.",
    )
    stop_loss: Optional[float] = Field(
        default=None,
        description=(
            "Recommended stop-loss level in the instrument's quote currency. "
            "Required for any directional rating (Buy / Overweight / Underweight / "
            "Sell) — defining an explicit invalidation level improves execution "
            "quality and gives the post-trade reflector a concrete prediction "
            "to score against. Leave null only for Hold."
        ),
    )
    take_profit: Optional[float] = Field(
        default=None,
        description=(
            "Recommended take-profit level in the instrument's quote currency. "
            "Required for any directional rating; the implied risk-reward ratio "
            "(take_profit vs stop_loss vs current price) should be at least 1.2:1 "
            "(QuantAgent 2025 default). Leave null only for Hold."
        ),
    )
    time_horizon: Optional[str] = Field(
        default=None,
        description="Optional recommended holding period, e.g. '3-6 months'.",
    )


# ---------------------------------------------------------------------------
# Quant Analyst (radar signal)
# ---------------------------------------------------------------------------


class QuantRadar(BaseModel):
    """6-dimensional structured radar from the Quant Analyst.

    Adapts QuantAgent (Xiong et al. 2025) Figure 2: condenses raw price /
    indicator data into a compact multi-dimensional signal that downstream
    synthesizers can read without parsing prose. Each axis is on a 1-10
    integer scale — coarse enough that small noise doesn't swing it,
    fine enough to discriminate setups.
    """

    volatility_risk: int = Field(
        ge=1, le=10,
        description=(
            "Magnitude of recent price fluctuations on a 1-10 scale. "
            "1 = stable / coiled; 10 = extreme volatility / large gaps. "
            "Anchor in ATR, Bollinger bandwidth, or realized volatility."
        ),
    )
    sr_strength: int = Field(
        ge=1, le=10,
        description=(
            "Integrity / strength of nearby support and resistance zones, 1-10. "
            "1 = no defined levels; 10 = multi-tested, structurally strong levels. "
            "Reference recent swing highs / lows, VWAP, and round-number levels."
        ),
    )
    breakout_likelihood: int = Field(
        ge=1, le=10,
        description=(
            "Probability of price escaping current consolidation, 1-10. "
            "1 = range-bound and likely to remain; 10 = clear breakout imminent. "
            "Consider Bollinger band squeeze, declining volatility, and volume buildup."
        ),
    )
    momentum_strength: int = Field(
        ge=1, le=10,
        description=(
            "Speed and persistence of the prevailing price movement, 1-10. "
            "1 = no momentum / chop; 10 = strong, sustained directional momentum. "
            "Anchor in MACD histogram, RSI distance from 50, RoC, and slope of EMAs."
        ),
    )
    pattern_reliability: int = Field(
        ge=1, le=10,
        description=(
            "Validity and completion of any active chart pattern, 1-10. "
            "1 = no recognizable pattern; 10 = textbook-complete formation. "
            "Examples: double bottom, descending triangle, flag. If no pattern is "
            "active, score 1."
        ),
    )
    trend_certainty: int = Field(
        ge=1, le=10,
        description=(
            "Clarity and consistency of directional bias, 1-10. "
            "1 = sideways / undefined; 10 = unambiguous uptrend or downtrend. "
            "Anchor in 50/200 SMA relationship, fitted support/resistance slopes, "
            "and absence of failed swings."
        ),
    )
    direction: str = Field(
        description=(
            "Net directional bias the radar implies. Exactly one of "
            "'long', 'short', or 'neutral'."
        ),
    )
    reasoning: str = Field(
        description=(
            "Two to four sentences justifying the six scores and the direction "
            "call. Cite specific indicator readings (e.g. 'RSI 62, MACD histogram "
            "expanding, ATR at 1.8% of price')."
        ),
    )


def render_quant_radar(radar: QuantRadar) -> str:
    """Render a QuantRadar as a compact markdown block for downstream prompts."""
    lines = [
        "**Quant Radar (1-10):**",
        f"- Volatility Risk: {radar.volatility_risk}",
        f"- S/R Strength: {radar.sr_strength}",
        f"- Breakout Likelihood: {radar.breakout_likelihood}",
        f"- Momentum Strength: {radar.momentum_strength}",
        f"- Pattern Reliability: {radar.pattern_reliability}",
        f"- Trend Certainty: {radar.trend_certainty}",
        f"- Direction: **{radar.direction}**",
        "",
        f"**Reasoning:** {radar.reasoning}",
    ]
    return "\n".join(lines)


def render_pm_decision(decision: PortfolioDecision) -> str:
    """Render a PortfolioDecision back to the markdown shape the rest of the system expects.

    Memory log, CLI display, and saved report files all read this markdown,
    so the rendered output preserves the exact section headers (``**Rating**``,
    ``**Executive Summary**``, ``**Investment Thesis**``) that downstream
    parsers and the report writers already handle.
    """
    parts = [
        f"**Rating**: {decision.rating.value}",
        "",
        f"**Executive Summary**: {decision.executive_summary}",
        "",
        f"**Investment Thesis**: {decision.investment_thesis}",
    ]
    if decision.price_target is not None:
        parts.extend(["", f"**Price Target**: {decision.price_target}"])
    if decision.stop_loss is not None:
        parts.extend(["", f"**Stop Loss**: {decision.stop_loss}"])
    if decision.take_profit is not None:
        parts.extend(["", f"**Take Profit**: {decision.take_profit}"])
    if decision.time_horizon:
        parts.extend(["", f"**Time Horizon**: {decision.time_horizon}"])
    return "\n".join(parts)
