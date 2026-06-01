"""Unit coverage for web/batch_runner.py — build_batch + BatchRunner's
synchronous surface (no worker threads, no SessionRunner graph)."""

from __future__ import annotations

import pytest

from web.batch_runner import BatchRunner, build_batch


class _FakeLoop:
    def call_soon_threadsafe(self, fn, *args):
        fn(*args)


def _form(**over):
    f = {"tickers": ["aapl", "nvda"], "analysis_date": "2024-01-02",
         "llm_provider": "google", "quick_think_llm": "q", "deep_think_llm": "d"}
    f.update(over)
    return f


def _runner(**over):
    b = build_batch(_form(**over))
    r = BatchRunner(b, _FakeLoop(), register_session=lambda child: None)
    r.events = []
    r._broadcast = r.events.append  # capture broadcasts inline
    return r


# ---------------------------------------------------------------------------
# build_batch
# ---------------------------------------------------------------------------

def test_build_batch_shape():
    b = build_batch(_form())
    assert [it["ticker"] for it in b["items"]] == ["AAPL", "NVDA"]  # upper-cased
    assert all(it["status"] == "pending" for it in b["items"])
    assert b["status"] == "pending"
    assert b["config"]["llm_provider"] == "google"
    assert b["config"]["max_concurrency"] == 4
    assert b["totals"] == {"llm_calls": 0, "tool_calls": 0, "tokens_in": 0, "tokens_out": 0}
    assert len(b["id"]) == 32


def test_build_batch_strips_and_dedupes_blank_tickers():
    b = build_batch(_form(tickers=["AAPL", "  ", "nvda"]))
    assert [it["ticker"] for it in b["items"]] == ["AAPL", "NVDA"]


def test_build_batch_empty_tickers_raises():
    with pytest.raises(ValueError):
        build_batch(_form(tickers=[]))


# ---------------------------------------------------------------------------
# subscribe / snapshot
# ---------------------------------------------------------------------------

def test_subscribe_unsubscribe():
    r = _runner()
    q = r.subscribe()
    assert q in r.subscribers
    r.unsubscribe(q)
    assert q not in r.subscribers
    r.unsubscribe(q)  # idempotent — no error


def test_snapshot_is_a_copy():
    r = _runner()
    snap = r.snapshot()
    snap["status"] = "MUT"
    assert r.batch["status"] == "pending"


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------

def test_cancel_only_when_active():
    r = _runner()
    r.batch["status"] = "running"
    assert r.cancel() is True
    assert r.is_cancelled() is True


def test_cancel_cascades_to_children():
    r = _runner()
    r.batch["status"] = "running"
    cancelled = {"n": 0}

    class _Child:
        def cancel(self):
            cancelled["n"] += 1

    r._children = [_Child(), _Child()]
    assert r.cancel() is True
    assert cancelled["n"] == 2


def test_cancel_rejected_when_done():
    r = _runner()
    r.batch["status"] = "completed"
    assert r.cancel() is False


def test_finalize_cancelled_marks_items():
    r = _runner()
    r.batch["items"][0]["status"] = "running"
    # second item stays pending
    r._finalize_cancelled()
    assert r.batch["status"] == "cancelled"
    assert all(it["status"] == "cancelled" for it in r.batch["items"])
    assert any(e["type"] == "batch" for e in r.events)


def test_run_safe_marks_failed_on_exception(monkeypatch):
    r = _runner()
    def _boom():
        raise RuntimeError("kaboom")
    monkeypatch.setattr(r, "_run", _boom)
    r._run_safe()
    assert r.batch["status"] == "failed"
    assert "kaboom" in r.batch["error"]


def test_run_safe_post_cancel_exception_is_cancelled(monkeypatch):
    r = _runner()
    r._cancel_requested.set()
    def _boom():
        raise RuntimeError("torn down")
    monkeypatch.setattr(r, "_run", _boom)
    r._run_safe()
    assert r.batch["status"] == "cancelled"


# ---------------------------------------------------------------------------
# mutators
# ---------------------------------------------------------------------------

def test_patch_updates_and_broadcasts():
    r = _runner()
    r._patch(status="running", started_at=123.0)
    assert r.batch["status"] == "running"
    assert any(e["type"] == "batch" for e in r.events)


def test_update_item_by_index():
    r = _runner()
    r._update_item(0, status="running", session_id="s1")
    assert r.batch["items"][0]["status"] == "running"
    assert r.batch["items"][0]["session_id"] == "s1"
    ev = next(e for e in r.events if e["type"] == "item")
    assert ev["index"] == 0 and ev["item"]["session_id"] == "s1"


# ---------------------------------------------------------------------------
# _recompute_totals
# ---------------------------------------------------------------------------

def test_recompute_totals_sums_item_stats():
    r = _runner()
    r.batch["items"][0]["stats"] = {"llm_calls": 2, "tool_calls": 1,
                                    "tokens_in": 100, "tokens_out": 50}
    r.batch["items"][1]["stats"] = {"llm_calls": 3, "tool_calls": 0,
                                    "tokens_in": 200, "tokens_out": 80}
    r._recompute_totals()
    assert r.batch["totals"] == {"llm_calls": 5, "tool_calls": 1,
                                 "tokens_in": 300, "tokens_out": 130}
    assert any(e["type"] == "totals" for e in r.events)


def test_recompute_totals_team_timings():
    r = _runner()
    r.batch["items"][0]["team_timings"] = {"Analyst Team": {"duration_s": 10.0}}
    r.batch["items"][1]["team_timings"] = {"Analyst Team": {"duration_s": 20.0}}
    r._recompute_totals()
    team = r.batch["team_totals"]["Analyst Team"]
    assert team["count"] == 2
    assert team["total_s"] == 30.0
    assert team["max_s"] == 20.0
    assert team["avg_s"] == 15.0


def test_recompute_totals_dedupes_identical():
    r = _runner()
    r._recompute_totals()        # first computes zero totals
    r.events.clear()
    r._recompute_totals()        # unchanged → no second broadcast
    assert not any(e["type"] == "totals" for e in r.events)


def test_recompute_totals_tolerates_missing_stats():
    r = _runner()
    r.batch["items"][0]["stats"] = None
    r.batch["items"][1]["stats"] = {"llm_calls": 4}
    r._recompute_totals()
    assert r.batch["totals"]["llm_calls"] == 4
    assert r.batch["totals"]["tokens_in"] == 0
