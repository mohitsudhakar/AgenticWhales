"""Coverage for agenticwhales/behavioral.py detectors + finding storage.
The detectors are pure over pre-fetched orders/outcomes/journal; storage runs
against the offline memstore.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agenticwhales import behavioral as bh


NOW = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


@pytest.fixture(autouse=True)
def _wipe():
    from web import auth
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


# ===========================================================================
# detect_tilt
# ===========================================================================

def test_detect_tilt_fires():
    orders = [
        {"id": "o1", "qty": 10, "side": "BUY", "ticker": "AAPL",
         "created_at": _iso(NOW - timedelta(minutes=50))},
        {"id": "o2", "qty": 10, "side": "BUY", "ticker": "AAPL",
         "created_at": _iso(NOW - timedelta(minutes=40))},
        {"id": "o3", "qty": 30, "side": "BUY", "ticker": "AAPL",
         "created_at": _iso(NOW)},
    ]
    outcomes = {"o1": {"hit": False}, "o2": {"hit": False}}
    findings = bh.detect_tilt("u1", orders, outcomes, NOW)
    assert len(findings) == 1 and findings[0].pattern == "tilt"
    assert findings[0].evidence["trigger_order_id"] == "o3"


def test_detect_tilt_empty():
    assert bh.detect_tilt("u1", [], {}, NOW) == []


# ===========================================================================
# detect_revenge
# ===========================================================================

def test_detect_revenge_fires():
    orders = [
        {"id": "o1", "qty": 10, "side": "BUY", "ticker": "AAPL",
         "created_at": _iso(NOW)},
        {"id": "o2", "qty": 15, "side": "BUY", "ticker": "AAPL",
         "created_at": _iso(NOW + timedelta(minutes=10))},
    ]
    outcomes = {"o1": {"hit": False}}
    findings = bh.detect_revenge("u1", orders, outcomes, NOW)
    assert len(findings) == 1 and findings[0].pattern == "revenge"
    assert findings[0].evidence["reentry_order_id"] == "o2"


# ===========================================================================
# detect_overconfidence
# ===========================================================================

def test_detect_overconfidence_fires():
    orders = [{"id": f"o{i}", "prob_of_profit": 0.9} for i in range(5)]
    outcomes = {"o0": {"hit": True}, "o1": {"hit": True},
                "o2": {"hit": False}, "o3": {"hit": False}, "o4": {"hit": False}}
    findings = bh.detect_overconfidence("u1", orders, outcomes, NOW)
    assert len(findings) == 1 and findings[0].pattern == "overconfidence"
    assert findings[0].evidence["realized_hit_rate"] == 0.4


def test_detect_overconfidence_too_few():
    orders = [{"id": "o0", "prob_of_profit": 0.9}]
    assert bh.detect_overconfidence("u1", orders, {"o0": {"hit": False}}, NOW) == []


# ===========================================================================
# detect_anchoring
# ===========================================================================

def test_detect_anchoring_fires():
    outcomes = {f"o{i}": {"realized_return_pct": 5.0} for i in range(5)}
    outcomes["o6"] = {"realized_return_pct": -100.0}
    orders = [{"id": "o6", "created_at": _iso(NOW)}]
    journal = [{"id": "j1", "created_at": _iso(NOW - timedelta(hours=1)),
                "sentiment_score": 80}]
    findings = bh.detect_anchoring("u1", orders, outcomes, journal, NOW)
    assert len(findings) == 1 and findings[0].pattern == "anchoring"
    assert "j1" in findings[0].evidence["journal_ids"]


def test_detect_anchoring_too_few_returns():
    assert bh.detect_anchoring("u1", [], {"o1": {"realized_return_pct": 1}}, [], NOW) == []


# ===========================================================================
# list_recent_findings / update_finding_state
# ===========================================================================

def test_list_recent_findings_and_update():
    from web import auth
    pk = "u1|tilt|x"
    auth._memstore[("behavioral_findings", pk)] = {
        "id": pk, "user_id": "u1", "pattern": "tilt",
        "created_at": bh._now_iso(), "dismissed": False, "acknowledged": False,
        "evidence": {"summary": "s"},
    }
    rows = bh.list_recent_findings("u1")
    assert len(rows) == 1
    assert bh.update_finding_state("u1", pk, acknowledged=True, dismissed=False) is True
    assert auth._memstore[("behavioral_findings", pk)]["acknowledged"] is True
    # wrong owner → False
    assert bh.update_finding_state("other", pk) is False


def test_list_recent_findings_excludes_old():
    from web import auth
    old = (datetime.now(tz=timezone.utc) - timedelta(days=60)).isoformat()
    auth._memstore[("behavioral_findings", "u1|tilt|old")] = {
        "id": "old", "user_id": "u1", "pattern": "tilt", "created_at": old,
    }
    assert bh.list_recent_findings("u1") == []


# ===========================================================================
# scan_user (orchestration, empty) + cooldown_in_effect
# ===========================================================================

def test_scan_user_empty_returns_nothing():
    assert bh.scan_user("u1", now=NOW) == []


def test_cooldown_in_effect_disabled_by_default():
    assert bh.cooldown_in_effect("u1", now=NOW) is None


def test_cooldown_in_effect_fires_when_opted_in():
    from web import auth
    # list_recent_findings filters by real wall-clock now, so anchor to it.
    real_now = datetime.now(tz=timezone.utc)
    auth.upsert_risk_limits("u1", behavioral_cooldown=True)
    pk = "u1|tilt|recent"
    auth._memstore[("behavioral_findings", pk)] = {
        "id": pk, "user_id": "u1", "pattern": "tilt",
        "created_at": (real_now - timedelta(minutes=10)).isoformat(),
        "dismissed": False, "evidence": {"summary": "tilt!"},
    }
    finding = bh.cooldown_in_effect("u1", now=real_now)
    assert finding is not None and finding["pattern"] == "tilt"
