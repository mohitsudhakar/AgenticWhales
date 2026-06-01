"""Diversity Engine eval — does provider diversification reduce Brier?

This is the empirical claim that PR-1 promised: AgenticWhales's design
heuristic (cross-family synthesizers + cross-family debaters) is not a
theorem, it's a measurement, and the measurement lives here.

We compare three arms over the resolved fixtures in `fixtures.py`:

  A. **all-same-family**  — synthesizers and debaters all on the upstream
                            provider. The naive baseline.
  B. **diverse-synth**    — synthesizers drawn from a different family;
                            debaters still on upstream. Tests *only* the
                            synthesizer-diversification claim.
  C. **diverse-full**     — both diversifications on. The shipped default.

For each arm we collect (predicted_prob_of_profit, realized_profitable)
pairs and compute:

  - **Brier mean**       — lower is better calibration; the headline metric.
  - **Hit rate**         — fraction of decisions where direction matched.
  - **Alpha vs SPY**     — mean realized_return - spy_return when the decision
                           said "buy" (i.e. p > 0.5).

The PortfolioDecision is produced by a `DecisionProtocol` adapter; in CI
we use a deterministic fake (`MockDecisionMaker`) so the eval runs without
LLM cost. To plug in the real graph, swap the adapter in
`make_decision_maker(arm)` for one that calls
`AgenticWhalesGraph(...).propagate(...)`.

Marker: this module is gated by the `eval` pytest marker. Default `pytest`
runs do NOT execute it (would be slow + LLM-cost). Explicit:

    pytest tests/evals -m eval -s

The CI nightly job calls it with `--diversity-engine-report` to emit a
markdown summary under `tests/evals/reports/`.
"""

from __future__ import annotations

import dataclasses
import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Protocol

import pytest

from .fixtures import FIXTURES, ResolvedFixture

# ---------------------------------------------------------------------------
# Protocol — a "decision maker" produces (prob_of_profit, action) per fixture
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Prediction:
    prob_of_profit: float  # in [0, 1]
    action: str            # "buy" | "sell" | "hold"


class DecisionProtocol(Protocol):
    """Anything that can answer (ticker, decision_date) → Prediction.

    Real implementation: wrap AgenticWhalesGraph and extract the PM's
    PortfolioDecision.prob_of_profit + action. The mock implementation
    below is just to wire the eval scaffold deterministically.
    """

    def predict(self, fixture: ResolvedFixture) -> Prediction:  # pragma: no cover
        ...


# ---------------------------------------------------------------------------
# Mock implementation — keeps CI green without burning LLM tokens
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class MockDecisionMaker:
    """Deterministic stand-in for the real multi-agent graph.

    Each `arm` perturbs the mock's calibration:
      - all-same-family is intentionally over-confident (compresses toward 1)
      - diverse-synth is mid-confidence
      - diverse-full is best-calibrated
    The point of the mock is NOT to claim diversification helps — that's
    what the real eval would measure. The point is to prove the harness
    machinery (scoring, reporting, fixture loading) works end-to-end.
    Real adapters override this.
    """

    arm: str
    seed: int = 42

    def predict(self, fixture: ResolvedFixture) -> Prediction:
        # Per-fixture deterministic noise so the same fixture gets the same
        # answer across runs.
        rnd = random.Random(f"{self.arm}:{fixture.ticker}:{self.seed}")

        # Ground-truth nudge: known winners get p > 0.5, losers get p < 0.5,
        # but mixed with arm-specific overconfidence.
        base = 0.55 if fixture.profitable else 0.45
        if self.arm == "all-same-family":
            # Over-confident: pushed toward the extremes
            p = 0.5 + (base - 0.5) * 4.0
        elif self.arm == "diverse-synth":
            p = 0.5 + (base - 0.5) * 2.0
        else:  # diverse-full
            p = 0.5 + (base - 0.5) * 1.2

        # Add small noise to make the eval non-degenerate
        p = max(0.05, min(0.95, p + (rnd.random() - 0.5) * 0.10))
        action = "buy" if p > 0.5 else ("sell" if p < 0.5 else "hold")
        return Prediction(prob_of_profit=p, action=action)


def make_decision_maker(arm: str) -> DecisionProtocol:
    """Return the decision maker for an arm.

    Override this when wiring the real graph. The arm name is also used
    by the report writer to label the columns; keep the three names stable
    or update the report template too.
    """
    return MockDecisionMaker(arm=arm)


ARMS = ["all-same-family", "diverse-synth", "diverse-full"]


# ---------------------------------------------------------------------------
# Scoring — Brier, hit rate, alpha
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class ArmScores:
    arm: str
    n: int
    brier_mean: float
    hit_rate: float
    alpha_when_long: float


def score_arm(arm: str, fixtures: Iterable[ResolvedFixture]) -> ArmScores:
    dm = make_decision_maker(arm)
    fixtures = list(fixtures)
    if not fixtures:
        return ArmScores(arm=arm, n=0, brier_mean=float("nan"),
                         hit_rate=float("nan"), alpha_when_long=float("nan"))

    brier_components = []
    hits = 0
    longs = []
    for fx in fixtures:
        pred = dm.predict(fx)
        y = 1.0 if fx.profitable else 0.0
        brier_components.append((pred.prob_of_profit - y) ** 2)

        # Direction hit: long predicts profitable, short predicts not.
        directional_y = fx.profitable
        directional_pred = (pred.prob_of_profit > 0.5)
        if directional_y == directional_pred:
            hits += 1

        if pred.action == "buy":
            longs.append(fx.alpha)

    return ArmScores(
        arm=arm,
        n=len(fixtures),
        brier_mean=sum(brier_components) / len(brier_components),
        hit_rate=hits / len(fixtures),
        alpha_when_long=(sum(longs) / len(longs)) if longs else float("nan"),
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def render_markdown_report(scores: list[ArmScores]) -> str:
    """Render the comparison as a markdown table + a one-line conclusion."""
    rows = ["| Arm | N | Brier ↓ | Hit rate ↑ | α when long ↑ |",
            "|---|---|---|---|---|"]
    for s in scores:
        rows.append(
            f"| `{s.arm}` | {s.n} | "
            f"{s.brier_mean:.4f} | {s.hit_rate:.3f} | "
            f"{s.alpha_when_long:+.4f} |"
        )

    # Conclusion line: which arm has the lowest Brier
    best = min(scores, key=lambda s: s.brier_mean)
    conclusion = (
        f"\n**Best Brier:** `{best.arm}` ({best.brier_mean:.4f}). "
        f"Lower is better — interpret as: this arm's predicted "
        f"prob_of_profit was on average closest to the realized outcome."
    )
    return "\n".join(rows) + "\n" + conclusion


def write_report(scores: list[ArmScores], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    md_path = out_dir / f"diversity_engine_{ts}.md"
    md_path.write_text(
        f"# Diversity Engine eval — {ts}\n\n"
        f"Fixtures: N={len(FIXTURES)}\n\n"
        + render_markdown_report(scores)
        + "\n\n## Raw scores (JSON)\n\n```json\n"
        + json.dumps([dataclasses.asdict(s) for s in scores], indent=2)
        + "\n```\n",
        encoding="utf-8",
    )
    return md_path


# ---------------------------------------------------------------------------
# Pytest entry — gated behind the `eval` marker
# ---------------------------------------------------------------------------


@pytest.mark.eval
def test_diversity_engine_runs_end_to_end():
    """Sanity test — the harness produces three sets of scores and a report.

    This is intentionally lax — we do NOT assert that any particular arm
    wins, because the mock distribution is chosen to make the scaffold
    work, not to claim a result. When the real adapter is plugged in,
    add specific assertions (e.g. `diverse-full` Brier < `all-same-family`
    Brier by some margin).
    """
    scores = [score_arm(arm, FIXTURES) for arm in ARMS]
    assert {s.arm for s in scores} == set(ARMS)
    for s in scores:
        assert s.n == len(FIXTURES)
        assert 0.0 <= s.brier_mean <= 1.0
        assert 0.0 <= s.hit_rate <= 1.0

    # Write a report into a tempdir so the CI run produces an artefact.
    out_dir = Path(__file__).parent / "reports"
    report_path = write_report(scores, out_dir)
    assert report_path.exists()
    assert report_path.read_text(encoding="utf-8").startswith("# Diversity Engine eval")


def main() -> None:  # pragma: no cover
    """CLI entry: `python -m tests.evals.diversity_engine_eval`."""
    scores = [score_arm(arm, FIXTURES) for arm in ARMS]
    print(render_markdown_report(scores))
    report_path = write_report(scores, Path(__file__).parent / "reports")
    print(f"\nReport written to: {report_path}")


if __name__ == "__main__":  # pragma: no cover
    main()
