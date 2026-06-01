"""Coverage for web/batch_runner.py run/compose paths: _run_one, _run
(sequential + pooled + cancel + report-error), _recompute_totals, and
_compose_report (incl. the nested _position_summary + list-content handling).
The child SessionRunner and the meta-summary LLM are faked — no graph, no LLM.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agenticwhales import portfolio
from web import batch_runner
from web.batch_runner import BatchRunner, build_batch


class _FakeLoop:
    def call_soon_threadsafe(self, fn, *args):
        fn(*args)


class _FakeChild:
    """Stand-in for SessionRunner: start() synchronously 'completes' the run."""
    def __init__(self, session, loop):
        self.session = session
        self._thread = None

    def cancel(self):
        self.session["status"] = "cancelled"

    def start(self):
        self.session["status"] = "completed"
        self.session["report_sections"] = {
            "final_trade_decision": "BUY",
            "trader_investment_plan": "the plan",
        }
        self.session["stats"] = {"llm_calls": 1, "tool_calls": 0,
                                 "tokens_in": 10, "tokens_out": 5}
        self.session["team_timings"] = {"analysts": {"duration_s": 1.5}}

    def snapshot(self):
        return self.session


class _FakeLLM:
    def __init__(self, content="REPORT BODY"):
        self.content = content

    def invoke(self, msgs):
        return SimpleNamespace(content=self.content)


class _FakeClient:
    def __init__(self, content="REPORT BODY"):
        self._llm = _FakeLLM(content)

    def get_llm(self):
        return self._llm


@pytest.fixture(autouse=True)
def _offline(tmp_path, monkeypatch):
    from web import auth
    monkeypatch.setattr(auth, "_db_writable", lambda: False)
    auth._reset_memstore_for_tests()
    monkeypatch.setattr(batch_runner, "SessionRunner", _FakeChild)
    monkeypatch.setattr(portfolio, "_PATH", tmp_path / "portfolio.json")
    yield
    auth._reset_memstore_for_tests()


def _form(**over):
    f = {"tickers": ["aapl", "nvda"], "analysis_date": "2024-01-02",
         "llm_provider": "google", "quick_think_llm": "q", "deep_think_llm": "d",
         "analysts": ["market"]}
    f.update(over)
    return f


def _runner(**over):
    b = build_batch(_form(**over))
    b["user_id"] = "u1"
    return BatchRunner(b, _FakeLoop(), register_session=lambda child: None)


# ===========================================================================
# _run_one
# ===========================================================================

def test_run_one_completes_item():
    r = _runner()
    r._run_one(0, r.batch["items"][0])
    item = r.batch["items"][0]
    assert item["status"] == "completed"
    assert item["final_decision"] == "BUY"
    assert item["trader_plan"] == "the plan"
    # totals were recomputed from the child's stats
    assert r.batch["totals"]["llm_calls"] == 1


def test_run_one_skips_when_cancel_requested():
    r = _runner()
    r._cancel_requested.set()
    r._run_one(0, r.batch["items"][0])
    assert r.batch["items"][0]["status"] == "cancelled"


# ===========================================================================
# _recompute_totals
# ===========================================================================

def test_recompute_totals_aggregates_team_timings():
    r = _runner()
    r.batch["items"][0]["stats"] = {"llm_calls": 2, "tool_calls": 1,
                                    "tokens_in": 100, "tokens_out": 50}
    r.batch["items"][0]["team_timings"] = {"analysts": {"duration_s": 2.0}}
    r.batch["items"][1]["team_timings"] = {"analysts": {"duration_s": 4.0}}
    r._recompute_totals()
    assert r.batch["totals"]["llm_calls"] == 2
    team = r.batch["team_totals"]["analysts"]
    assert team["count"] == 2 and team["max_s"] == 4.0 and team["avg_s"] == 3.0


# ===========================================================================
# _run end-to-end
# ===========================================================================

def test_run_sequential_then_report(monkeypatch):
    monkeypatch.setattr(batch_runner, "create_llm_client", lambda **k: _FakeClient())
    r = _runner(max_concurrency=1)
    r._run()
    assert r.batch["status"] == "completed"
    assert r.batch["report"] == "REPORT BODY"
    assert all(it["status"] == "completed" for it in r.batch["items"])


def test_run_pooled(monkeypatch):
    monkeypatch.setattr(batch_runner, "create_llm_client", lambda **k: _FakeClient())
    r = _runner(max_concurrency=4)
    r._run()
    assert r.batch["status"] == "completed"


def test_run_cancel_before_start():
    r = _runner()
    r._cancel_requested.set()
    r._run()
    assert r.batch["status"] == "cancelled"
    assert all(it["status"] == "cancelled" for it in r.batch["items"])


def test_run_report_error_marks_no_report(monkeypatch):
    r = _runner(max_concurrency=1)

    def _boom():
        raise RuntimeError("llm down")
    monkeypatch.setattr(r, "_compose_report", _boom)
    r._run()
    assert r.batch["status"] == "completed_no_report"
    assert "llm down" in r.batch["report_error"]


# ===========================================================================
# _compose_report
# ===========================================================================

def _completed_runner(monkeypatch, content="REPORT BODY"):
    monkeypatch.setattr(batch_runner, "create_llm_client",
                        lambda **k: _FakeClient(content))
    r = _runner(max_concurrency=1)
    for it in r.batch["items"]:
        it["final_decision"] = f"BUY {it['ticker']}"
        it["trader_plan"] = "plan"
        it["status"] = "completed"
    return r


def test_compose_report_with_positions(monkeypatch):
    portfolio.save_all({"AAPL": {"qty": 10, "avg_cost": 150, "notes": "core"},
                        "NVDA": {"qty": -5}})
    r = _completed_runner(monkeypatch)
    report = r._compose_report()
    assert report == "REPORT BODY"


def test_compose_report_no_completed_raises(monkeypatch):
    monkeypatch.setattr(batch_runner, "create_llm_client", lambda **k: _FakeClient())
    r = _runner(max_concurrency=1)  # items have no final_decision
    with pytest.raises(RuntimeError):
        r._compose_report()


def test_compose_report_flat_book(monkeypatch):
    # no recorded positions → "FLAT" book block path
    r = _completed_runner(monkeypatch)
    assert r._compose_report() == "REPORT BODY"


def test_compose_report_list_content(monkeypatch):
    content = [{"type": "text", "text": "Part A"}, "Part B", {"other": 1}]
    r = _completed_runner(monkeypatch, content=content)
    out = r._compose_report()
    assert "Part A" in out and "Part B" in out
