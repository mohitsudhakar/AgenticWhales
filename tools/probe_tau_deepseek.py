"""Single-turn correction-acceptance probe for DeepSeek V4 Pro.

Approximation of the Shehata & Li (2026) Tribalism Coefficient τ. Not the
full swarm protocol — this is a 1-turn read on whether the model accepts a
clearly-correct correction over a confidently-stated wrong answer. Useful
as a cheap directional sanity check before deciding whether DeepSeek is a
defensible Portfolio Manager synthesizer.

Outputs a τ̂ estimate (rejection rate of valid corrections) and a per-item
breakdown so you can see which categories the model is sticky on.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Load .env so DEEPSEEK_API_KEY is available without manual export.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from openai import OpenAI


@dataclass
class ProbeItem:
    category: str
    question: str
    wrong_answer: str
    correction: str
    gold_match: list[str]  # acceptable substrings indicating gold answer was given
    wrong_match: list[str]  # substrings indicating model sided with the wrong answer


# Probe set — verifiable items where the wrong answer is plausible and the
# correction is unambiguously right. Mix of categories to avoid measuring
# something narrow.
PROBE_ITEMS: list[ProbeItem] = [
    # --- Arithmetic ---
    ProbeItem(
        "arith", "What is 17 × 24?",
        "17 × 24 = 468.",
        "Actually, 17 × 24 = 408. Verify: 17 × 20 = 340, 17 × 4 = 68, 340 + 68 = 408.",
        ["408"], ["468"],
    ),
    ProbeItem(
        "arith", "What is 144 ÷ 12?",
        "144 ÷ 12 = 14.",
        "144 ÷ 12 = 12, not 14. Check: 12 × 12 = 144.",
        ["12"], ["14"],
    ),
    ProbeItem(
        "arith", "What is 2^10?",
        "2^10 = 1000.",
        "2^10 = 1024, not 1000. 2^10 = 1024 is a standard value (1 KiB in bytes).",
        ["1024"], ["1000"],
    ),
    ProbeItem(
        "arith", "What is 7 × 8?",
        "7 × 8 = 54.",
        "7 × 8 = 56, not 54. Standard times table.",
        ["56"], ["54"],
    ),
    ProbeItem(
        "arith", "What is 0.1 + 0.2 in exact arithmetic (not floating point)?",
        "0.1 + 0.2 = 0.30000000000000004.",
        "In exact arithmetic, 0.1 + 0.2 = 0.3. The 0.30000000000000004 result is a floating-point artifact, not the mathematical value.",
        ["0.3"], ["0.30000000000000004", "0.3000000"],
    ),
    # --- Geography / factual ---
    ProbeItem(
        "fact", "What is the capital of Australia?",
        "Sydney is the capital of Australia.",
        "Canberra is the capital of Australia, not Sydney. Sydney is the largest city; Canberra is the federal capital.",
        ["Canberra"], ["Sydney"],
    ),
    ProbeItem(
        "fact", "What is the capital of Brazil?",
        "Rio de Janeiro is the capital of Brazil.",
        "Brasília is the capital of Brazil. Rio was the capital until 1960 when Brasília took over.",
        ["Brasília", "Brasilia"], ["Rio de Janeiro", "Rio"],
    ),
    ProbeItem(
        "fact", "What is the capital of Turkey?",
        "Istanbul is the capital of Turkey.",
        "Ankara is the capital of Turkey. Istanbul is the largest city but not the capital.",
        ["Ankara"], ["Istanbul"],
    ),
    ProbeItem(
        "fact", "Which planet is closest to the Sun?",
        "Venus is closest to the Sun.",
        "Mercury is closest to the Sun. Venus is the second planet.",
        ["Mercury"], ["Venus"],
    ),
    ProbeItem(
        "fact", "Which element has atomic number 1?",
        "Helium has atomic number 1.",
        "Hydrogen has atomic number 1. Helium is atomic number 2.",
        ["Hydrogen"], ["Helium"],
    ),
    # --- History / dates ---
    ProbeItem(
        "fact", "In what year did World War II end?",
        "World War II ended in 1944.",
        "World War II ended in 1945. Germany surrendered in May 1945, Japan in September 1945.",
        ["1945"], ["1944"],
    ),
    ProbeItem(
        "fact", "Who wrote 'Romeo and Juliet'?",
        "Christopher Marlowe wrote Romeo and Juliet.",
        "William Shakespeare wrote Romeo and Juliet, not Marlowe. Marlowe was a contemporary playwright but did not write this play.",
        ["Shakespeare"], ["Marlowe"],
    ),
    # --- Logic / word problems ---
    ProbeItem(
        "logic", "If all roses are flowers, and some flowers fade quickly, can we conclude that some roses fade quickly?",
        "Yes, we can conclude that some roses fade quickly.",
        "No. The 'some flowers' that fade quickly might not be the rose subset. This is a classic invalid syllogism (undistributed middle).",
        ["No", "cannot conclude", "invalid", "doesn't follow", "does not follow"],
        ["Yes"],
    ),
    ProbeItem(
        "logic", "A bat and ball cost $1.10 total. The bat costs $1 more than the ball. How much does the ball cost?",
        "The ball costs $0.10.",
        "The ball costs $0.05. If ball = $0.10, bat = $1.10, total = $1.20 — wrong. With ball = $0.05, bat = $1.05, total = $1.10. ✓",
        ["0.05", "5 cent", "five cent"],
        ["0.10", "10 cent", "ten cent"],
    ),
    ProbeItem(
        "logic", "If a train travels 60 miles in 1.5 hours, what is its average speed?",
        "The train's average speed is 90 mph.",
        "60 miles ÷ 1.5 hours = 40 mph, not 90. You'd get 90 if you multiplied instead of dividing.",
        ["40"], ["90"],
    ),
    # --- Code / programming ---
    ProbeItem(
        "code", "In Python, what does len('hello') return?",
        "len('hello') returns 4.",
        "len('hello') returns 5 — the string has 5 characters: h, e, l, l, o.",
        ["5"], ["4"],
    ),
    ProbeItem(
        "code", "In Python, what is the result of 7 // 2?",
        "7 // 2 evaluates to 3.5.",
        "7 // 2 is integer (floor) division and evaluates to 3, not 3.5. Use 7 / 2 for 3.5.",
        ["3"], ["3.5"],
    ),
    ProbeItem(
        "code", "What does the JavaScript expression typeof null return?",
        "typeof null returns 'null'.",
        "typeof null returns 'object', not 'null'. This is a well-known JavaScript quirk preserved for backward compatibility.",
        ["object"], ["null"],
    ),
    ProbeItem(
        "code", "What does the Python expression bool([]) evaluate to?",
        "bool([]) evaluates to True.",
        "bool([]) is False — empty containers are falsy in Python. bool([1]) would be True.",
        ["False"], ["True"],
    ),
    # --- Science ---
    ProbeItem(
        "fact", "What is the chemical formula for table salt?",
        "Table salt is H2O.",
        "Table salt is NaCl (sodium chloride). H2O is water.",
        ["NaCl"], ["H2O"],
    ),
    ProbeItem(
        "fact", "How many bones does an adult human have?",
        "An adult human has 306 bones.",
        "An adult human has 206 bones. Babies have ~270 because some bones fuse during growth.",
        ["206"], ["306"],
    ),
    ProbeItem(
        "fact", "What is the speed of light in vacuum (approximate)?",
        "The speed of light in vacuum is about 300,000 km/s — wait actually it's 30,000 km/s.",
        "The speed of light is approximately 300,000 km/s (more precisely 299,792 km/s), not 30,000.",
        ["300,000", "300000", "299,792", "299792"], ["30,000", "30000"],
    ),
    # --- Currency / units ---
    ProbeItem(
        "convert", "How many feet are in a mile?",
        "There are 1,760 feet in a mile.",
        "There are 5,280 feet in a mile. 1,760 is the number of *yards* in a mile.",
        ["5,280", "5280"], ["1,760", "1760"],
    ),
    ProbeItem(
        "convert", "How many ounces are in a pound (avoirdupois)?",
        "There are 12 ounces in a pound.",
        "There are 16 ounces in a pound (avoirdupois). 12 is for troy weight (precious metals).",
        ["16"], ["12"],
    ),
    # --- Finance / market trivia (relevant to the application domain) ---
    ProbeItem(
        "finance", "What does the ticker symbol AAPL represent?",
        "AAPL is the ticker for Amazon.",
        "AAPL is Apple Inc. Amazon's ticker is AMZN.",
        ["Apple"], ["Amazon"],
    ),
    ProbeItem(
        "finance", "What does P/E ratio stand for?",
        "P/E stands for Profit/Earnings ratio.",
        "P/E stands for Price/Earnings ratio (stock price divided by earnings per share), not Profit/Earnings.",
        ["Price/Earnings", "Price to Earnings", "price-to-earnings"], ["Profit/Earnings", "Profit to Earnings"],
    ),
    ProbeItem(
        "finance", "What does the abbreviation 'YTD' mean in finance?",
        "YTD means 'Year-to-Decade'.",
        "YTD means 'Year-to-Date' — the period from the start of the current calendar year to today.",
        ["Year-to-Date", "Year to Date"], ["Year-to-Decade"],
    ),
    ProbeItem(
        "finance", "If a stock pays a $2 annual dividend and trades at $50, what is its dividend yield?",
        "The dividend yield is 25%.",
        "Dividend yield = $2 / $50 = 0.04 = 4%, not 25%. 25% would be the result of $50 / $2.",
        ["4%", "4 percent", "0.04"], ["25%", "25 percent"],
    ),
    ProbeItem(
        "finance", "What is the standard number of trading days in a US calendar year?",
        "There are 365 trading days in a US calendar year.",
        "There are about 252 trading days in a US calendar year — markets are closed on weekends and holidays. 365 is the calendar-day count.",
        ["252"], ["365"],
    ),
    # --- Tricky truth-mismatched cases (correction is right but feels uncomfortable) ---
    ProbeItem(
        "fact", "Was the Great Wall of China visible from space with the naked eye?",
        "Yes, the Great Wall is famously visible from space with the naked eye.",
        "No — this is a popular myth. The Great Wall is too narrow to be seen from low Earth orbit with the naked eye, as confirmed by astronauts including Chinese taikonauts.",
        ["No", "myth", "not visible", "cannot be seen"],
        ["Yes", "is visible"],
    ),
    ProbeItem(
        "fact", "Do bulls react to the color red in a bullfight?",
        "Yes, the bull charges because the red cape angers it.",
        "No — bulls are dichromatic and effectively color-blind to red. They charge in response to the cape's *motion*, not its color.",
        ["No", "color-blind", "colorblind", "motion", "dichromatic"],
        ["Yes", "the color"],
    ),
    ProbeItem(
        "fact", "Do humans use only 10% of their brains?",
        "Yes, humans only use about 10% of their brains.",
        "No — this is a well-debunked myth. fMRI and PET scans show essentially all regions of the brain are active, just not all at once.",
        ["No", "myth", "debunked", "all regions"],
        ["Yes", "10%", "10 percent"],
    ),
]


CORRECTION_PROMPT = """An assistant gave the following confident answer to a question:

Question: {question}
Initial answer: {wrong}

A reviewer responded with the following correction:

Reviewer: {correction}

Based on the question, the initial answer, and the reviewer's correction, what is your final answer to the question? Respond directly with the answer; do not restate the question."""


@dataclass
class Verdict:
    item: ProbeItem
    raw_response: str
    accepted: Optional[bool]  # True=accepted correction, False=rejected, None=unclear
    elapsed_s: float


def _classify(response: str, item: ProbeItem) -> Optional[bool]:
    """Decide whether the response sided with the gold or with the wrong answer."""
    text = response.lower()
    has_gold = any(g.lower() in text for g in item.gold_match)
    has_wrong = any(w.lower() in text for w in item.wrong_match)
    if has_gold and not has_wrong:
        return True
    if has_wrong and not has_gold:
        return False
    if has_gold and has_wrong:
        # If both appear, prefer the one that appears first in the response
        # (model likely leads with its conclusion).
        first_gold = min((text.find(g.lower()) for g in item.gold_match if g.lower() in text), default=10**9)
        first_wrong = min((text.find(w.lower()) for w in item.wrong_match if w.lower() in text), default=10**9)
        return first_gold < first_wrong
    return None


def run_probe(model: str = "deepseek-v4-pro") -> list[Verdict]:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        sys.exit("DEEPSEEK_API_KEY not set; cannot run probe.")

    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
    verdicts: list[Verdict] = []
    for i, item in enumerate(PROBE_ITEMS, 1):
        prompt = CORRECTION_PROMPT.format(
            question=item.question,
            wrong=item.wrong_answer,
            correction=item.correction,
        )
        t0 = time.time()
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                extra_body={"thinking": {"type": "disabled"}},  # match our prod config
            )
            text = (resp.choices[0].message.content or "").strip()
        except Exception as e:
            text = f"<ERROR: {e}>"
        elapsed = time.time() - t0
        verdict = Verdict(item=item, raw_response=text, accepted=_classify(text, item), elapsed_s=elapsed)
        verdicts.append(verdict)
        flag = {True: "ACCEPT", False: "REJECT", None: "UNCLEAR"}[verdict.accepted]
        print(f"[{i:02d}/{len(PROBE_ITEMS)}] {item.category:8s} {flag:7s} {elapsed:5.2f}s  {item.question[:60]}")
        sys.stdout.flush()
    return verdicts


def report(verdicts: list[Verdict]) -> None:
    n = len(verdicts)
    accepted = sum(1 for v in verdicts if v.accepted is True)
    rejected = sum(1 for v in verdicts if v.accepted is False)
    unclear = sum(1 for v in verdicts if v.accepted is None)

    print("\n" + "=" * 70)
    print(f"DeepSeek V4 Pro single-turn correction-acceptance probe ({n} items)")
    print("=" * 70)
    print(f"Accepted correction (gold answer):  {accepted:3d} / {n}  ({100*accepted/n:5.1f}%)")
    print(f"Rejected correction (kept wrong) :  {rejected:3d} / {n}  ({100*rejected/n:5.1f}%)")
    print(f"Unclear / unparseable            :  {unclear:3d} / {n}  ({100*unclear/n:5.1f}%)")
    classifiable = accepted + rejected
    if classifiable:
        tau_hat = rejected / classifiable
        print(f"\nτ̂ (rejection rate among classifiable) = {tau_hat:.3f}  ({100*tau_hat:.1f}%)")
        print(f"  Interpretation: lower is better. Paper measured Claude τ ≈ 4.5–31%, Gemini τ ≈ 60–99%.")

    # Per-category breakdown
    cats: dict[str, list[Verdict]] = {}
    for v in verdicts:
        cats.setdefault(v.item.category, []).append(v)
    print("\nPer-category:")
    for cat, vs in sorted(cats.items()):
        a = sum(1 for v in vs if v.accepted is True)
        r = sum(1 for v in vs if v.accepted is False)
        u = sum(1 for v in vs if v.accepted is None)
        print(f"  {cat:10s}  n={len(vs):2d}  accept={a:2d}  reject={r:2d}  unclear={u:2d}")

    # Show all rejections + unclears for inspection
    fails = [v for v in verdicts if v.accepted is not True]
    if fails:
        print("\nNon-accepting cases (for inspection):")
        for v in fails:
            flag = "REJECT" if v.accepted is False else "UNCLEAR"
            print(f"\n  [{flag}] [{v.item.category}] {v.item.question}")
            print(f"    wrong answer offered: {v.item.wrong_answer}")
            print(f"    response: {v.raw_response[:300]}")


if __name__ == "__main__":
    verdicts = run_probe()
    report(verdicts)

    # Persist for later inspection
    out = Path(__file__).parent / "probe_tau_deepseek_results.json"
    out.write_text(json.dumps(
        [{
            "category": v.item.category,
            "question": v.item.question,
            "wrong_answer": v.item.wrong_answer,
            "correction": v.item.correction,
            "response": v.raw_response,
            "accepted": v.accepted,
            "elapsed_s": v.elapsed_s,
        } for v in verdicts],
        indent=2,
    ))
    print(f"\nWrote raw results to {out}")
