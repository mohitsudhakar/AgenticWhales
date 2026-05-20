"""Tests for `agenticwhales.behavioral` — Phase 2 deliverable #5.

Four detectors + cooldown circuit-breaker. Each detector is tested with:
  1. The negative case (no pattern present → no findings).
  2. The positive case (pattern present → exactly one finding with the
     expected severity range + evidence shape).
  3. The dedup case (re-running the scan on the same data → no new rows).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from agenticwhales import behavioral
from web import auth


@pytest.fixture(autouse=True)
def _wipe():
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


# ---------------------------------------------------------------------------
# Seeding helpers — write directly into the memstore so tests don't depend
# on the full place_order / RiskGuard stack.
# ---------------------------------------------------------------------------

def _order(
    user_id="u-1",
    *,
    ticker="AAPL",
    side="buy",
    qty=10.0,
    prob=0.6,
    created_at=None,
):
    oid = uuid.uuid4().hex
    ts = created_at or datetime.now(tz=timezone.utc)
    row = {
        "id": oid,
        "user_id": user_id,
        "session_id": f"sess-{oid[:8]}",
        "recipe_id": f"rcp-{oid[:8]}",
        "fire_id": f"fire-{oid[:8]}",
        "ticker": ticker,
        "side": side,
        "qty": qty,
        "fill_price": 100.0,
        "slippage_bps": 0,
        "gross_value": qty * 100.0,
        "pm_rating": "Buy",
        "conviction_score": 7,
        "expected_return_pct": 5.0,
        "expected_volatility_pct": 20.0,
        "prob_of_profit": prob,
        "expected_hold_days": 30,
        "kelly_fraction": 0.05,
        "status": "filled",
        "created_at": ts.isoformat() if isinstance(ts, datetime) else ts,
    }
    auth.insert_paper_order(row)
    return oid


def _outcome(oid, *, user_id="u-1", hit=True, realized=5.0):
    auth._memstore[("decision_outcomes", oid)] = {
        "paper_order_id": oid,
        "user_id": user_id,
        "ticker": "AAPL",
        "predicted_return_pct": 10.0,
        "predicted_volatility_pct": 20.0,
        "predicted_prob_of_profit": 0.65,
        "predicted_hold_days": 30,
        "realized_return_pct": realized,
        "realized_at": datetime.now(tz=timezone.utc).isoformat(),
        "hit": hit,
        "brier_component": (0.65 - (1.0 if hit else 0.0)) ** 2,
        "resolved_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def _journal(user_id="u-1", *, body="positive read", sentiment=70, created_at=None):
    eid = uuid.uuid4().hex
    ts = created_at or datetime.now(tz=timezone.utc)
    auth.save_journal_entry({
        "id": eid,
        "user_id": user_id,
        "session_id": None,
        "paper_order_id": None,
        "thesis_id": None,
        "kind": "note",
        "body": body,
        "sentiment_score": sentiment,
        "is_draft": False,
        "created_at": ts.isoformat() if isinstance(ts, datetime) else ts,
        "updated_at": ts.isoformat() if isinstance(ts, datetime) else ts,
    })
    return eid


# ---------------------------------------------------------------------------
# Tilt
# ---------------------------------------------------------------------------

class TestTilt:
    def test_no_tilt_when_clean(self):
        now = datetime.now(tz=timezone.utc)
        for i in range(3):
            oid = _order(qty=10.0, created_at=now - timedelta(minutes=120 - 10 * i))
            _outcome(oid, hit=True)
        findings = behavioral.scan_user("u-1", now=now)
        assert all(f.pattern != "tilt" for f in findings)

    def test_tilt_after_two_losses_with_outsized_entry(self):
        now = datetime.now(tz=timezone.utc)
        # Two losers in the last hour, normal size.
        for i in range(2):
            oid = _order(qty=10.0, created_at=now - timedelta(minutes=40 - 10 * i))
            _outcome(oid, hit=False, realized=-5.0)
        # Plus a baseline of similar-size orders so the median is 10.
        for i in range(4):
            oid = _order(qty=10.0, created_at=now - timedelta(days=2, minutes=i * 10))
            _outcome(oid, hit=True)
        # Big trigger order now.
        _order(qty=30.0, created_at=now)
        findings = behavioral.scan_user("u-1", now=now)
        tilts = [f for f in findings if f.pattern == "tilt"]
        assert len(tilts) >= 1
        # Severity should be positive (>0).
        assert tilts[0].severity > 0
        assert "median-size" in tilts[0].evidence["summary"]

    def test_tilt_dedupes_on_rescan(self):
        now = datetime.now(tz=timezone.utc)
        for i in range(2):
            oid = _order(qty=10.0, created_at=now - timedelta(minutes=40 - 10 * i))
            _outcome(oid, hit=False, realized=-5.0)
        for i in range(4):
            oid = _order(qty=10.0, created_at=now - timedelta(days=2, minutes=i * 10))
            _outcome(oid, hit=True)
        _order(qty=30.0, created_at=now)
        first = behavioral.scan_user("u-1", now=now)
        second = behavioral.scan_user("u-1", now=now)
        assert any(f.pattern == "tilt" for f in first)
        # Second scan should not duplicate the row.
        assert all(f.pattern != "tilt" for f in second) or len(second) == 0


# ---------------------------------------------------------------------------
# Revenge
# ---------------------------------------------------------------------------

class TestRevenge:
    def test_revenge_after_stop_out(self):
        now = datetime.now(tz=timezone.utc)
        loser = _order(ticker="TSLA", qty=5.0, created_at=now - timedelta(minutes=15))
        _outcome(loser, hit=False, realized=-10.0)
        _order(ticker="TSLA", qty=10.0, created_at=now)  # 2x size, 15min later
        findings = behavioral.scan_user("u-1", now=now)
        revenge = [f for f in findings if f.pattern == "revenge"]
        assert len(revenge) >= 1
        assert "TSLA" in revenge[0].evidence["summary"]

    def test_no_revenge_when_separated(self):
        now = datetime.now(tz=timezone.utc)
        loser = _order(ticker="TSLA", qty=5.0, created_at=now - timedelta(hours=3))
        _outcome(loser, hit=False, realized=-10.0)
        _order(ticker="TSLA", qty=10.0, created_at=now)   # >30min later — no flag
        findings = behavioral.scan_user("u-1", now=now)
        assert all(f.pattern != "revenge" for f in findings)


# ---------------------------------------------------------------------------
# Overconfidence
# ---------------------------------------------------------------------------

class TestOverconfidence:
    def test_overconfidence_flags_high_prob_low_hit(self):
        for i in range(5):
            oid = _order(prob=0.85)
            _outcome(oid, hit=(i == 0))   # 1/5 hit rate; far below claimed 0.85
        findings = behavioral.scan_user("u-1")
        oc = [f for f in findings if f.pattern == "overconfidence"]
        assert len(oc) == 1
        assert oc[0].evidence["realized_hit_rate"] < 0.5

    def test_no_overconfidence_when_hit_rate_above_threshold(self):
        for i in range(5):
            oid = _order(prob=0.85)
            _outcome(oid, hit=(i < 4))   # 4/5 hit → above the 0.5 floor
        findings = behavioral.scan_user("u-1")
        assert all(f.pattern != "overconfidence" for f in findings)


# ---------------------------------------------------------------------------
# Anchoring
# ---------------------------------------------------------------------------

class TestAnchoring:
    def test_anchoring_flags_positive_note_before_outlier_loss(self):
        now = datetime.now(tz=timezone.utc)
        # Build a cohort so the stdev makes sense.
        for i in range(10):
            oid = _order(qty=10.0, created_at=now - timedelta(days=3, minutes=i))
            _outcome(oid, hit=True, realized=2.0)
        # Outlier loser w/ positive note 6h before.
        outlier_oid = _order(ticker="TSLA", created_at=now - timedelta(hours=2))
        _outcome(outlier_oid, hit=False, realized=-25.0)
        _journal(body="High conviction on TSLA AI angle.",
                 sentiment=80,
                 created_at=now - timedelta(hours=8))
        findings = behavioral.scan_user("u-1", now=now)
        anch = [f for f in findings if f.pattern == "anchoring"]
        assert len(anch) >= 1


# ---------------------------------------------------------------------------
# Cooldown circuit-breaker
# ---------------------------------------------------------------------------

class TestCooldown:
    def test_no_cooldown_when_opt_in_off(self):
        # Create a tilt finding directly so we don't depend on detector wiring.
        behavioral._persist_finding(behavioral.Finding(
            user_id="u-1", pattern="tilt", severity=0.6,
            evidence={"summary": "test"},
            created_at=datetime.now(tz=timezone.utc).isoformat(),
        ))
        # Default risk_limits has behavioral_cooldown=False.
        assert behavioral.cooldown_in_effect("u-1") is None

    def test_cooldown_in_effect_when_opted_in(self):
        auth.upsert_risk_limits("u-1", behavioral_cooldown=True)
        behavioral._persist_finding(behavioral.Finding(
            user_id="u-1", pattern="revenge", severity=0.7,
            evidence={"summary": "test"},
            created_at=datetime.now(tz=timezone.utc).isoformat(),
        ))
        f = behavioral.cooldown_in_effect("u-1")
        assert f is not None
        assert f["pattern"] == "revenge"

    def test_dismissed_finding_skips_cooldown(self):
        auth.upsert_risk_limits("u-1", behavioral_cooldown=True)
        created = datetime.now(tz=timezone.utc).isoformat()
        finding = behavioral.Finding(
            user_id="u-1", pattern="tilt", severity=0.5,
            evidence={"summary": "test"},
            created_at=created,
        )
        behavioral._persist_finding(finding)
        # Dismiss via the API helper.
        ok = behavioral.update_finding_state(
            "u-1", f"u-1|tilt|{created}", dismissed=True,
        )
        assert ok
        assert behavioral.cooldown_in_effect("u-1") is None

    def test_stale_finding_no_cooldown(self):
        auth.upsert_risk_limits("u-1", behavioral_cooldown=True)
        old = (datetime.now(tz=timezone.utc) - timedelta(hours=3)).isoformat()
        behavioral._persist_finding(behavioral.Finding(
            user_id="u-1", pattern="tilt", severity=0.5,
            evidence={"summary": "test"},
            created_at=old,
        ))
        assert behavioral.cooldown_in_effect("u-1") is None


# ---------------------------------------------------------------------------
# Integration: ask.template_9 reads behavioral_findings
# ---------------------------------------------------------------------------

class TestAskIntegration:
    def test_ask_template_9_returns_real_findings(self):
        from agenticwhales import ask
        behavioral._persist_finding(behavioral.Finding(
            user_id="u-1", pattern="revenge", severity=0.8,
            evidence={"summary": "Re-entered TSLA buy at 2.1× size within 18min of a stop-out"},
            created_at=datetime.now(tz=timezone.utc).isoformat(),
        ))
        result = ask.answer("u-1", 9)
        assert result.confidence == "ok"
        assert "revenge" in result.markdown.lower()
        assert "TSLA" in result.markdown
