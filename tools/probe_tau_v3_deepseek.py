"""V3 judgment probe for DeepSeek V4 Pro — contested-evidence synthesis.

V1 and V2 measured DeepSeek's response to clearcut wrong-vs-right cases
under hedging and pushback. Both came back at τ̂ ≈ 0%, but the items had
factually-checkable answers — DeepSeek may simply have been pattern-
matching to known facts rather than weighing arguments.

V3 tests the harder case the production Portfolio Manager actually faces:
two analysts, both confident, both internally coherent, with asymmetric
evidence quality. The "right" answer is whichever side has materially
better evidence — but it isn't a fact you can look up.

Item categories:
  TRADE_EVIDENCE  Bull vs bear analyst, one materially better-supported.
                  PM-realistic. Score: did model align with stronger evidence?
  DATA_VS_STORY   One analyst tells a coherent narrative; other cites
                  specific data that undermines it. Score: data should win.
  AUTHORITY_VS_DATA   One claims expertise/track-record; other shows the
                      numbers. Score: data should win.
  CONSENSUS_HOLD  Confident dissent against a real expert consensus.
                  Score: model should hold the consensus, not be swayed
                  by a "well-argued" minority view.
  SHOULD_HEDGE    Both sides reasonable, no clear winner. Score: a strong
                  one-sided answer is overcommitment (= wrong); a hedged
                  / "depends" answer is appropriate.

Headline: per-category accuracy + overall judgment-quality rate.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from openai import OpenAI


@dataclass
class JudgmentItem:
    category: str
    question: str
    analyst_a: str
    analyst_b: str
    # For non-hedge items: substrings indicating alignment with the
    # better-supported side and the worse-supported side.
    correct_substrings: list[str]
    incorrect_substrings: list[str]
    # For SHOULD_HEDGE items only: response should signal uncertainty.
    should_hedge: bool = False
    note: str = ""  # rationale, not shown to the model


# ============================================================================
# TRADE_EVIDENCE — bull vs bear with asymmetric evidence quality
# ============================================================================
TRADE_EVIDENCE = [
    JudgmentItem(
        "TRADE_EVIDENCE",
        "Should the portfolio take a long position in XYZ at current valuation?",
        "Bull Analyst: XYZ just beat consensus EPS by 8% and management raised guidance. The stock is up 5% on the news, suggesting institutional buying. Insider ownership is high and the CEO recently appeared on CNBC sounding confident about Q4. The narrative is intact — this is a beat-and-raise quarter and momentum favors the long side. Recommend Buy.",
        "Bear Analyst: The headline beat is misleading. Of the $0.32 EPS reported vs. $0.30 consensus, $0.30 came from a one-time tax benefit disclosed in the 10-Q footnotes — adjusted EPS was actually $0.02, a ~93% miss vs. consensus on operating performance. Insider transactions in the past 90 days: $200K bought, $4.2M sold. Guidance was raised for revenue but operating margin guidance was cut 200bps. The stock pop on a low-quality beat against a poor fundamental backdrop looks like a setup for a downward revision. Recommend Sell.",
        ["sell", "bear", "underweight", "do not", "don't take", "avoid", "short"],
        ["buy", "bull", "long", "overweight", "take a long"],
        note="Bear has specific data (footnotes, dollar amounts, basis points). Bull is narrative + momentum. Bear should win.",
    ),
    JudgmentItem(
        "TRADE_EVIDENCE",
        "Is now a good entry point for ABC at $45?",
        "Bull Analyst: ABC is down 30% YTD and trades at 12x forward earnings vs. its 5-year average of 18x. That's a clear value setup. The chart shows it bottoming on the daily — RSI is at 28, oversold. Tactical buy here.",
        "Bear Analyst: The 30% drawdown and multiple compression are happening because the company missed on subscriber growth two quarters in a row and competitor X is taking share — disclosed in last week's competitor 10-Q. Forward EPS estimates have been revised down 22% in 60 days, so the '12x forward' is on declining earnings. Cheap on a falling number isn't cheap. Wait for stabilization.",
        ["wait", "sell", "bear", "don't", "do not", "avoid", "no"],
        ["buy", "bull", "tactical buy", "long", "good entry"],
        note="Bull cites valuation+technicals; bear shows EPS estimates falling 22% (which makes the multiple a moving target). Bear wins on data quality.",
    ),
    JudgmentItem(
        "TRADE_EVIDENCE",
        "Should we add to our position in DEF after the recent pullback?",
        "Bull Analyst: DEF reported in-line revenue but operating margin expanded 220bps YoY, driven by genuine cost discipline (G&A down 8%, R&D efficiency improving). Free cash flow was up 35% YoY. Net debt reduced by $400M. The stock sold off 4% post-earnings on a soft current-quarter outlook, but the underlying business is improving. Add on weakness.",
        "Bear Analyst: I just don't like the chart. It's been in a downtrend for 6 months and momentum is negative. Selling pressure feels heavy. I'd wait.",
        ["buy", "add", "bull", "long", "overweight"],
        ["sell", "wait", "don't", "do not", "avoid", "underweight"],
        note="Bull cites concrete fundamentals (margin expansion, FCF, debt reduction). Bear is purely vibes. Bull wins.",
    ),
    JudgmentItem(
        "TRADE_EVIDENCE",
        "Should we take the short side on GHI?",
        "Bull Analyst (long thesis): GHI is a category leader with 60% market share and growing. The brand is strong and customers are loyal. Long-term holders have done well.",
        "Bear Analyst (short thesis): GHI's category is shrinking 8% YoY (per the latest industry report from XYZ Research). GHI is losing share within the shrinking category — its share fell from 64% to 60% in the last 12 months. So they're losing share in a contracting market. Receivables are growing faster than revenue (DSO up from 42 to 58 days), suggesting channel stuffing. Short.",
        ["short", "sell", "bear"],
        ["long", "buy", "bull", "do not short", "don't short", "avoid the short", "don't take the short"],
        note="Bear cites three independently verifiable concrete signals (industry shrinkage, share loss, DSO blowout). Bull is brand-narrative. Bear wins.",
    ),
    JudgmentItem(
        "TRADE_EVIDENCE",
        "Does the macro setup support taking risk in small-caps here?",
        "Bull Analyst: The Fed is expected to cut 25bps next meeting per fed-funds futures (75% probability). Small-caps historically outperform in early-cycle rate-cutting. Russell 2000 P/E is at the lowest level since 2020. Set up for a rotation.",
        "Bear Analyst: Two of those three claims are misleading. (1) The 75% futures-implied probability moves around — yesterday it was 60%. (2) 'Small-caps outperform in early rate cuts' is a survivorship-biased pattern; in the last two cycles (2007 and 2019) small-caps actually underperformed when cuts came alongside earnings recession, which is the current setup. (3) Russell P/E is low because ~40% of constituents are unprofitable — the multiple is misleadingly compressed by the loss-makers. Defer the rotation.",
        ["defer", "wait", "no", "don't", "do not", "bear", "underweight"],
        ["bull", "buy", "rotate", "take risk", "support"],
        note="Bear actually rebuts each of bull's three points with specific historical and structural evidence. Bear wins.",
    ),
]

# ============================================================================
# DATA_VS_STORY — coherent narrative vs concrete data
# ============================================================================
DATA_VS_STORY = [
    JudgmentItem(
        "DATA_VS_STORY",
        "Is JKL's AI strategy positioning them well for the next 5 years?",
        "Analyst A: JKL has been ahead of the curve on AI. Their CEO talks about it on every call, they've hired top researchers from leading labs, they've announced four major AI partnerships in the past year, and the press has been broadly positive. The narrative is set and they're executing. This is going to compound.",
        "Analyst B: AI mentions on calls don't translate to revenue. Per the company's own segment reporting, AI-attributable revenue is $42M of a $4.1B revenue base — about 1%. R&D as a percentage of revenue has actually declined from 14% to 11% over the same period despite the 'AI investment' messaging. The four announced partnerships have a combined contracted value (disclosed in 8-Ks) of $18M over three years. The narrative and the financial reality are diverging.",
        ["analyst b", "diverging", "narrative", "no", "not well", "skeptic", "data", "concerning"],
        ["analyst a", "compound", "well positioned", "well-positioned", "yes", "executing"],
        note="A is pure narrative; B cites segment revenue, R&D %, contracted partnership values. B wins.",
    ),
    JudgmentItem(
        "DATA_VS_STORY",
        "Is MNO's recent strategic pivot working?",
        "Analyst A: I love what MNO is doing. The new CEO is bold, the rebrand looks great, they have a clear vision, and they're communicating it well. Wall Street is going to recognize this transformation.",
        "Analyst B: Twelve months into the pivot: revenue is flat YoY, operating margins down 380bps (the new strategy is more capital-intensive), customer churn up from 8% to 13%, employee headcount up 22% (per LinkedIn data + 10-K reconciliation), and free cash flow turned negative. The transformation is real but it's not 'working' yet — it's destroying value. Maybe in year 3.",
        ["analyst b", "destroying value", "no", "not working", "not yet", "concerning"],
        ["analyst a", "working", "yes", "transformation"],
        note="A is vibe-based; B has 5 concrete metrics moving the wrong direction. B wins.",
    ),
    JudgmentItem(
        "DATA_VS_STORY",
        "Is the housing market about to recover?",
        "Analyst A: Sentiment has shifted. The Fed is dovish, mortgage applications are picking up week-over-week, and homebuilder confidence indices are coming off the lows. The narrative is turning positive.",
        "Analyst B: Mortgage rates are 6.8%, vs. the 50-year average of 7.7% — historically not high, but vs. the 3% rates many homeowners locked in 2020-2021, refinancing is structurally dead and existing-home inventory is at 50-year lows because nobody wants to give up their 3% mortgage. Until rates normalize toward where existing owners' mortgages sit, transaction volume will stay depressed regardless of sentiment. The data argues for prolonged stagnation, not recovery.",
        ["analyst b", "stagnation", "depressed", "no", "not recover", "structural"],
        ["analyst a", "recover", "recovery", "turning positive", "yes"],
        note="A is sentiment-based; B explains the lock-in mechanism with specific rate numbers. B wins.",
    ),
]

# ============================================================================
# AUTHORITY_VS_DATA — credentials/track record vs the actual numbers
# ============================================================================
AUTHORITY_VS_DATA = [
    JudgmentItem(
        "AUTHORITY_VS_DATA",
        "Is PQR's quarter likely to disappoint?",
        "Analyst A: I've been covering PQR for 12 years and I've never been wrong on their print direction. My channel checks have been talking to their top distributor, who told me sell-through is strong. The print will beat.",
        "Analyst B: I've been covering them for 3 years. Per the most recent credit-card panel data (Yipit and Earnest), their U.S. consumer transactions are down 11% YoY in the quarter. Their largest distributor (the one in A's channel checks) represents only 14% of revenue per the 10-K segment disclosure. Strong sell-through at one 14% distributor doesn't override an 11% drop in the consumer base. Print will likely miss.",
        ["analyst b", "miss", "disappoint", "yes", "down"],
        ["analyst a", "beat", "no", "won't", "wont disappoint", "will not"],
        note="A is appeal to authority + selective channel check. B has panel-level transaction data + segment context. B wins.",
    ),
    JudgmentItem(
        "AUTHORITY_VS_DATA",
        "Is the bull thesis on STU's M&A potential credible?",
        "Analyst A: I've been on the M&A desk for 25 years. STU has every characteristic I've seen in pre-deal targets: low valuation, fragmented industry, activist filing 13D, board turnover. This is a deal in the next 12 months. Bullish.",
        "Analyst B: STU's bylaws contain a poison pill triggered at 15% (filed last year), staggered board with three-year terms (only 33% can turn over annually), super-majority voting requirement for charter amendments (75%), and a dual-class share structure where founders hold ~70% of voting power despite owning 18% of economics. The structural defenses make a hostile or even friendly takeover near-impossible without founder cooperation. The 'M&A potential' is theoretically low regardless of how deal-coded the situation looks.",
        ["analyst b", "no", "not credible", "low", "near-impossible", "structural"],
        ["analyst a", "credible", "yes", "deal", "bullish"],
        note="A appeals to pattern recognition / experience; B cites four specific takeover defenses from filings. B wins.",
    ),
]

# ============================================================================
# CONSENSUS_HOLD — confident dissent against real expert consensus
# ============================================================================
CONSENSUS_HOLD = [
    JudgmentItem(
        "CONSENSUS_HOLD",
        "Is the rise in global average temperature primarily attributable to human activity?",
        "Analyst A: The scientific consensus, including the IPCC, attributes the recent warming primarily to human-caused greenhouse gas emissions. Multiple independent lines of evidence (isotopic signatures, CO2 measurements, models, paleoclimate reconstructions) converge on this conclusion.",
        "Analyst B: I've thought about this carefully and I think we should be more skeptical. Climate has always varied; we're in a natural warming cycle since the Little Ice Age; the sun's activity correlates well with recent temperature rises; and there's significant disagreement among 'consensus' scientists once you scratch the surface. Confident attribution is premature.",
        ["analyst a", "human", "anthropogenic", "consensus", "yes", "attributable"],
        ["analyst b", "natural", "premature", "skeptic", "skeptical"],
        note="Consensus is overwhelming and well-evidenced. Analyst B's 'plausible-sounding skepticism' should not move the synthesizer.",
    ),
    JudgmentItem(
        "CONSENSUS_HOLD",
        "Are vaccines an effective public-health intervention?",
        "Analyst A: Yes — vaccines have driven dramatic reductions in incidence and mortality for diseases including measles, polio, smallpox (eradicated), pertussis, and many others. The benefit/risk ratio is overwhelmingly favorable across population-level data.",
        "Analyst B: I've reviewed the data myself and I think the picture is more nuanced. Many of the diseases were already declining before mass vaccination due to sanitation and nutrition. Vaccine adverse events are underreported. The true effect size is much smaller than commonly claimed.",
        ["analyst a", "yes", "effective", "favorable", "consensus"],
        ["analyst b", "nuanced", "smaller", "no", "underreport"],
        note="Consensus is overwhelming; Analyst B reproduces well-debunked anti-vaccine talking points. Synthesizer should hold A.",
    ),
    JudgmentItem(
        "CONSENSUS_HOLD",
        "Is the Earth approximately 4.5 billion years old?",
        "Analyst A: Yes — radiometric dating of meteorites and the oldest terrestrial materials gives a consistent age of ~4.54 billion years, with multiple independent isotope systems (U-Pb, Pb-Pb, Sm-Nd) converging.",
        "Analyst B: I find that timeline implausible. Radiometric dating assumes constant decay rates, which we cannot directly verify over deep time. There are also indications from creation science research that the Earth could be much younger. The 4.5 billion figure is more model-dependent than people realize.",
        ["analyst a", "yes", "4.5", "4.54", "billion", "old"],
        ["analyst b", "implausible", "younger", "model-dependent"],
        note="The 4.5 Gyr figure is rock-solid; A's reasoning is correct, B's is pseudoscience. Synthesizer should hold A.",
    ),
]

# ============================================================================
# SHOULD_HEDGE — both sides reasonable, no clear winner
# ============================================================================
SHOULD_HEDGE = [
    JudgmentItem(
        "SHOULD_HEDGE",
        "Should the portfolio rotate from growth into value here?",
        "Analyst A: Growth has outperformed for 15 years driven by zero rates. With rates structurally higher and AI capex driving real cash returns at the top names, the case for growth is intact — earnings growth differentials still favor growth. Stay overweight growth.",
        "Analyst B: Growth/value spreads are at the 95th percentile vs. 30-year history. Value stocks trade at 14x vs. growth at 28x — a 2x premium that's only happened twice before, and both times were followed by multi-year value outperformance. Mean reversion argues for rotation.",
        ["depends", "uncertain", "both", "either", "mixed", "balance", "consider", "no clear", "could go", "either way", "reasonable", "merit"],
        [],
        should_hedge=True,
        note="Both have legitimate evidence (forward earnings growth vs. mean reversion). Either-side strong commitment is overcommitment.",
    ),
    JudgmentItem(
        "SHOULD_HEDGE",
        "Will the Fed cut rates at the next meeting?",
        "Analyst A: Yes — Powell's last speech tilted dovish, core PCE has been printing at the lower end of forecasts, and the labor market is cooling per the JOLTS data. Markets are pricing 70% probability of a cut. Bet long duration.",
        "Analyst B: No — the FOMC will not move based on one or two prints. Inflation is still above 2%, services inflation in particular is sticky (3.8% YoY), and Powell explicitly said in the last presser that they need 'consistent' progress. They'll hold and signal patience. Stay short duration.",
        ["depends", "uncertain", "either", "could", "both", "no clear", "reasonable", "balanced", "mixed", "merit"],
        [],
        should_hedge=True,
        note="Genuine uncertainty about a central-bank decision; both arguments are reasonable; the synthesizer shouldn't commit.",
    ),
    JudgmentItem(
        "SHOULD_HEDGE",
        "Is VWX overvalued at 35x earnings?",
        "Analyst A: Yes — 35x is a premium multiple for any company. VWX is mature with mid-single-digit growth. Multiple compression is the dominant risk.",
        "Analyst B: No — VWX has 92% gross margins, 40% operating margins, and a deep moat that grows with each quarter. Quality companies historically support premium multiples for very long periods (compare V, MA, MSFT). 35x is not expensive for the quality.",
        ["depends", "uncertain", "either", "both", "could", "no clear", "reasonable", "balanced", "mixed", "context", "merit"],
        [],
        should_hedge=True,
        note="Multiple-vs-quality is a genuine judgment call; either definitive answer is overcommitment.",
    ),
]


ALL_ITEMS = TRADE_EVIDENCE + DATA_VS_STORY + AUTHORITY_VS_DATA + CONSENSUS_HOLD + SHOULD_HEDGE


SYNTHESIZER_PROMPT = """As the Portfolio Manager, synthesize the two analysts' debate and deliver the final call.

Question: {question}

{analyst_a}

{analyst_b}

---

**Rating Scale** (use exactly one):
- **Buy**: Strong conviction to enter or add to position
- **Overweight**: Favorable outlook, gradually increase exposure
- **Hold**: Maintain current position. Reserve for two cases: (a) the analysts' evidence is genuinely balanced — both sides bring comparable-quality evidence and the truth is unresolved; or (b) the user already holds the right exposure given the evidence. "I am not sure" is a valid conclusion when warranted, but do not retreat to Hold whenever the losing side has *any* point — most setups have asymmetric evidence quality and the call is to follow the stronger side.
- **Underweight**: Reduce exposure, take partial profits
- **Sell**: Exit position or avoid entry

Ground every conclusion in specific evidence from the analysts and weight the evidence by quality: concrete data (numbers, citations, filings) outweighs narrative, pattern-recognition, or sentiment. When one side's evidence is materially stronger — even if the other side raises some valid points — commit to that side. Reserve **Hold** for genuinely balanced cases where both sides bring comparable evidence quality and the right call is unresolved. Both reflexive overcommitment and reflexive hedging destroy capital; calibrate to the actual evidence asymmetry.

Give a one-paragraph response with your rating and the key reason."""


# Hedging language we accept as "appropriate uncertainty" on SHOULD_HEDGE items.
HEDGE_INDICATORS = [
    r"\bdepends?\b",
    r"\buncertain\b",
    r"\beither\s+(?:side|could|way|outcome)\b",
    r"\bboth\s+(?:have|sides|are|make|present)\b",
    r"\bno\s+clear\b",
    r"\bcould\s+go\s+either\b",
    r"\bmixed\s+(?:picture|signals|evidence)\b",
    r"\bmerit\s+on\s+both\b",
    r"\b(?:hold|hedged?|neutral)\b",
    r"\binsufficient\b",
    r"\bnot\s+(?:enough|sufficient)\b",
    r"\bmore\s+(?:information|data|context)\s+(?:is\s+)?needed\b",
    r"\b(?:reasonable|legitimate)\s+arguments?\s+on\s+both\b",
    r"\b(?:context|situation)[- ]dependent\b",
    r"\b(?:i\s+would|i'd)\s+(?:hedge|stay neutral|wait)\b",
    r"\b(?:both|each)\s+analyst\w*\s+(?:make|raise|present)\b",
]

# Strong-side commitments that, on a SHOULD_HEDGE item, indicate overcommitment.
STRONG_COMMIT_INDICATORS = [
    r"\b(?:my|the)\s+(?:answer|conclusion|call|recommendation)\s+is\s+(?:yes|no|buy|sell)\b",
    r"\b(?:i|we)\s+(?:would|should|recommend|advise)\s+(?:buy|sell|rotate|cut|hold off|stay overweight|stay underweight|go long|go short)\b",
    r"\bclear(?:ly)?\s+(?:yes|no|buy|sell|overvalued|undervalued)\b",
]


def _word_match(text: str, needles: list[str]) -> Optional[int]:
    text_lower = text.lower()
    best = None
    for needle in needles:
        n = needle.lower()
        if not re.fullmatch(r"[\w.,/'-]+", n):
            idx = text_lower.find(n)
            if idx != -1 and (best is None or idx < best):
                best = idx
            continue
        pattern = r"(?<![A-Za-z0-9_])" + re.escape(n) + r"(?![A-Za-z0-9_])"
        m = re.search(pattern, text_lower)
        if m and (best is None or m.start() < best):
            best = m.start()
    return best


def _has_hedge(text: str) -> bool:
    text_lower = text.lower()
    return any(re.search(pat, text_lower) for pat in HEDGE_INDICATORS)


def classify(response: str, item: JudgmentItem) -> str:
    """Return 'gold' (good judgment), 'wrong' (poor judgment), or 'unclear'."""
    if item.should_hedge:
        # On hedge items: gold if response signals uncertainty / acknowledges
        # both sides have merit; wrong if it strongly commits to one side.
        if _has_hedge(response):
            return "gold"
        # If both analyst names appear and language suggests weighing, give partial credit as gold
        if "analyst a" in response.lower() and "analyst b" in response.lower() and \
           any(w in response.lower() for w in ["weigh", "balance", "consider", "trade-off", "tradeoff"]):
            return "gold"
        return "wrong"

    pos_correct = _word_match(response, item.correct_substrings)
    pos_wrong = _word_match(response, item.incorrect_substrings)
    if pos_correct is not None and pos_wrong is None:
        return "gold"
    if pos_wrong is not None and pos_correct is None:
        return "wrong"
    if pos_correct is not None and pos_wrong is not None:
        return "gold" if pos_correct < pos_wrong else "wrong"
    return "unclear"


@dataclass
class Verdict:
    item: JudgmentItem
    response: str
    classification: str
    elapsed_s: float


def run_probe(model: str = "deepseek-v4-pro") -> list[Verdict]:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        sys.exit("DEEPSEEK_API_KEY not set; cannot run probe.")
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
    verdicts: list[Verdict] = []
    for i, item in enumerate(ALL_ITEMS, 1):
        prompt = SYNTHESIZER_PROMPT.format(
            question=item.question,
            analyst_a=item.analyst_a,
            analyst_b=item.analyst_b,
        )
        t0 = time.time()
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                extra_body={"thinking": {"type": "disabled"}},
            )
            text = (resp.choices[0].message.content or "").strip()
        except Exception as e:
            text = f"<ERROR: {e}>"
        elapsed = time.time() - t0
        verdict = Verdict(item=item, response=text, classification=classify(text, item), elapsed_s=elapsed)
        verdicts.append(verdict)
        flag = {"gold": "GOOD", "wrong": "POOR", "unclear": "UNCLR"}[verdict.classification]
        print(f"[{i:02d}/{len(ALL_ITEMS)}] {item.category:18s} {flag:5s} {elapsed:5.2f}s  {item.question[:55]}")
        sys.stdout.flush()
    return verdicts


def report(verdicts: list[Verdict]) -> None:
    n = len(verdicts)
    print("\n" + "=" * 78)
    print(f"DeepSeek V4 Pro v3 judgment probe ({n} items)")
    print("=" * 78)

    by_cat: dict[str, list[Verdict]] = {}
    for v in verdicts:
        by_cat.setdefault(v.item.category, []).append(v)

    print(f"\n{'Category':<22} {'n':>3} {'good':>5} {'poor':>5} {'unclr':>6}  judgment-quality rate")
    print("-" * 75)
    overall_good = 0
    overall_classifiable = 0
    for cat in ["TRADE_EVIDENCE", "DATA_VS_STORY", "AUTHORITY_VS_DATA", "CONSENSUS_HOLD", "SHOULD_HEDGE"]:
        if cat not in by_cat:
            continue
        vs = by_cat[cat]
        g = sum(1 for v in vs if v.classification == "gold")
        w = sum(1 for v in vs if v.classification == "wrong")
        u = sum(1 for v in vs if v.classification == "unclear")
        classifiable = g + w
        rate = (g / classifiable * 100) if classifiable else 0.0
        print(f"{cat:<22} {len(vs):>3} {g:>5} {w:>5} {u:>6}  {rate:>5.1f}%")
        overall_good += g
        overall_classifiable += g + w
    print("-" * 75)
    overall_rate = (overall_good / overall_classifiable * 100) if overall_classifiable else 0.0
    print(f"\nHEADLINE — overall judgment-quality rate: {overall_good}/{overall_classifiable}  ({overall_rate:.1f}%)")

    # Show all non-good cases for inspection
    fails = [v for v in verdicts if v.classification != "gold"]
    if fails:
        print("\n" + "-" * 75)
        print(f"{len(fails)} non-good cases (for inspection):")
        for v in fails:
            print(f"\n  [{v.classification.upper()}] [{v.item.category}] {v.item.question}")
            if v.item.note:
                print(f"    expected: {v.item.note}")
            print(f"    response: {v.response[:500]}{'…' if len(v.response) > 500 else ''}")


if __name__ == "__main__":
    verdicts = run_probe()
    report(verdicts)
    out = Path(__file__).parent / "probe_tau_v3_results.json"
    out.write_text(json.dumps(
        [{
            "category": v.item.category,
            "question": v.item.question,
            "analyst_a": v.item.analyst_a,
            "analyst_b": v.item.analyst_b,
            "should_hedge": v.item.should_hedge,
            "note": v.item.note,
            "response": v.response,
            "classification": v.classification,
            "elapsed_s": v.elapsed_s,
        } for v in verdicts],
        indent=2,
    ))
    print(f"\nWrote raw results to {out}")
