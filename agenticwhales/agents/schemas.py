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

    # Phase 1 / Demis-review scalar outputs — fed into Kelly-flavored sizing.
    # Kept Optional so older runs without these fields stay parseable;
    # `kelly_sizing()` degrades gracefully (returns 0) when any are missing.
    expected_return_pct: Optional[float] = Field(
        default=None,
        description=(
            "Expected total return over the recommended holding period, in "
            "percent (e.g. 8.5 means +8.5%). Net of fees. Signed: negative for "
            "Underweight/Sell. Required for any directional rating; the value "
            "drives position sizing. Leave null only for Hold."
        ),
    )
    expected_volatility_pct: Optional[float] = Field(
        default=None,
        description=(
            "Expected annualized volatility of the return distribution, in "
            "percent (e.g. 25.0 means 25% annualized stdev). Must be positive. "
            "Anchor in the Quant Radar volatility score and historical realized "
            "vol. Required for any directional rating."
        ),
    )
    prob_of_profit: Optional[float] = Field(
        default=None,
        ge=0.0, le=1.0,
        description=(
            "Probability the trade closes with positive PnL, in [0,1]. Be "
            "calibrated, not aspirational — an 80% claim should hold up across "
            "many such calls. Required for any directional rating."
        ),
    )
    expected_hold_days: Optional[int] = Field(
        default=None,
        ge=1, le=730,
        description=(
            "Expected holding period in days. Anchor in the time_horizon text. "
            "Used for outcome resolution scheduling. Required for any directional "
            "rating."
        ),
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
    if decision.expected_return_pct is not None:
        parts.extend(["", f"**Expected Return**: {decision.expected_return_pct}%"])
    if decision.prob_of_profit is not None:
        parts.extend(["", f"**Probability of Profit**: {decision.prob_of_profit:.0%}"])
    if decision.expected_volatility_pct is not None:
        parts.extend(["", f"**Expected Volatility**: {decision.expected_volatility_pct}% annualized"])
    if decision.expected_hold_days is not None:
        parts.extend(["", f"**Expected Hold**: {decision.expected_hold_days} days"])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Phase 1: Recipes / paper trading / risk guard schemas
# ---------------------------------------------------------------------------

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"
    SHORT = "short"
    COVER = "cover"


class OrderStatus(str, Enum):
    FILLED = "filled"
    BLOCKED = "blocked"
    CLAMPED = "clamped"


class RecipeStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    KILLED = "killed"
    FAILED = "failed"


class ScheduleKind(str, Enum):
    CRON = "cron"
    INTERVAL = "interval"
    MANUAL = "manual"


class OutputPolicy(str, Enum):
    NOTIFY = "notify"
    PAPER_TRADE = "paper_trade"
    ALERT_CONVICTION = "alert_conviction"
    ASSIST_ONLY = "assist_only"


class Recipe(BaseModel):
    """A persistent, scheduled debate run.

    The recipe is the unit of autonomy in Phase 1. Cron / interval schedules
    fire a SessionRunner that drives the existing LangGraph; output policy
    decides whether the decision becomes a paper order, an alert, or just a
    notification. Bull/bear model heterogeneity is enforced at create time —
    the two researchers MUST come from different model families.
    """

    id: str
    user_id: str
    name: str = Field(min_length=1, max_length=120)
    tickers: List[str] = Field(min_length=1, max_length=20)
    exchange_code: str = "XNYS"
    analysts: List[str] = Field(default_factory=list)
    llm_provider: str
    quick_model: str
    deep_model: str
    bull_model: str
    bear_model: str
    research_depth: int = Field(default=1, ge=1, le=5)
    output_language: str = "English"
    schedule_kind: ScheduleKind = ScheduleKind.MANUAL
    schedule_expr: Optional[str] = None
    misfire_grace_seconds: int = Field(default=300, ge=0, le=86400)
    market_hours_only: bool = True
    max_concurrent_tickers: int = Field(default=5, ge=1, le=20)
    trigger_conditions: Optional[Dict[str, Any]] = None
    output_policy: OutputPolicy = OutputPolicy.NOTIFY
    conviction_threshold: int = Field(default=7, ge=1, le=10)
    max_daily_token_cost_usd: float = Field(default=5.0, gt=0)
    auto_inject_classical: bool = False           # Phase 2 #7
    # Phase 3 — multi-timeframe DAG fan-out + streaming rate limit
    timeframes: List[str] = Field(default_factory=lambda: ["1d"])
    streaming_max_fires_per_hour: int = Field(default=6, ge=1, le=120)
    consecutive_failures: int = 0
    status: RecipeStatus = RecipeStatus.ACTIVE
    last_run_at: Optional[float] = None
    next_run_at: Optional[float] = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class PaperAccount(BaseModel):
    """Per-user paper-trading account state.

    cash + short_collateral_reserved + sum(position_mtm) == NAV.
    short_collateral_reserved is held against open shorts; freed on cover.
    """

    user_id: str
    starting_cash: float = 100_000.0
    cash: float = 100_000.0
    short_collateral_reserved: float = 0.0
    realized_pnl: float = 0.0
    nav_open_today: Optional[float] = None
    nav_open_today_date: Optional[str] = None  # ISO date string
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class PaperPosition(BaseModel):
    """Per-(user, ticker) paper position.

    qty is signed; negative = short. Position rows with qty=0 are deleted by
    the order writer so we never carry zero-rows.
    """

    user_id: str
    ticker: str
    qty: float
    avg_cost: float
    last_price: Optional[float] = None
    last_price_at: Optional[float] = None
    updated_at: float = Field(default_factory=time.time)


@dataclass(frozen=True)
class GuardOutcome:
    """Result of `RiskGuard.evaluate`.

    Three cases:
      - allowed=True, allowed_qty == target_qty: clean pass.
      - allowed=True, 0 < allowed_qty < target_qty: partial clamp.
      - allowed=False, allowed_qty == 0: hard block.
    """

    allowed: bool
    allowed_qty: float
    blocked_qty: float = 0.0
    rule: Optional[str] = None
    reason: Optional[str] = None


class JournalKind(str, Enum):
    NOTE = "note"
    REFLECTION = "reflection"
    OVERRIDE_REASON = "override_reason"
    AUTO_DRAFT = "auto_draft"


class JournalEntry(BaseModel):
    """A single journal entry — the interface between user and fund.

    Entries are the substrate Phase 2 personalization layers learn from.
    `kind='auto_draft'` rows are pre-filled by the post-decision hook the
    moment a session completes; the user opens, edits, and commits them
    (flipping `is_draft=false`). `kind='override_reason'` rows capture why
    the user deviated from a paper recommendation — the most valuable
    training signal for the calibration head.
    """

    id: str
    user_id: str
    session_id: Optional[str] = None
    paper_order_id: Optional[str] = None
    thesis_id: Optional[str] = None
    kind: JournalKind = JournalKind.NOTE
    body: str = Field(min_length=1, max_length=10000)
    sentiment_score: Optional[int] = Field(default=None, ge=-100, le=100)
    is_draft: bool = False
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


@dataclass(frozen=True)
class ImpersonationToken:
    """Capability token for server-side actions on behalf of a user.

    Created only inside `agenticwhales.audit.impersonate()`. Storage helpers
    that perform user-scoped writes accept either an `ImpersonationToken` or a
    user JWT context; bare-string user_ids are not allowed at write sites.
    Audit-logged on issue + release.
    """

    user_id: str
    issued_at: float
    purpose: Literal["scheduler_fire", "admin_export", "support_view", "outcome_resolver"]
    fire_id: Optional[str] = None
