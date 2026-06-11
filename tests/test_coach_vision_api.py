"""Screenshot-activation API behavior: vision-first routing, OCR fallback,
guest caps (scoped to documents, not CSV), real spend recording."""

import datetime as dt
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import web.coach_api as coach_api
import web.server as server
from agenticwhales.transactions import extract as extract_mod
from agenticwhales.transactions.models import Transaction
from web import auth


@pytest.fixture
def client():
    return TestClient(server.app)


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), (255, 255, 255)).save(buf, "PNG")
    return buf.getvalue()


def _fake_txns():
    return [
        Transaction(date="2025-01-06", type="Buy", symbol="AAPL", quantity=50,
                    price=180, amount=-9000),
        Transaction(date="2025-01-08", type="Sell", symbol="AAPL", quantity=50,
                    price=184, amount=9200),
    ]


def _sample_csv() -> str:
    rows = ["Date,Type,Symbol,Quantity,Price,Amount"]
    for t in _fake_txns():
        rows.append(f"{t.date},{t.type},{t.symbol},{t.quantity},{t.price},{t.amount}")
    return "\n".join(rows)


def test_image_uses_vision_first_no_ocr(client, monkeypatch):
    monkeypatch.setenv("AGENTICWHALES_VISION_PROVIDER", "google")
    ocr_called = {}
    monkeypatch.setattr(coach_api, "_ocr_pdf_to_markdown",
                        lambda b: ocr_called.setdefault("ocr", True) or "")
    monkeypatch.setattr(extract_mod, "extract_transactions_from_image",
                        lambda imgs, mimes=None, **kw: _fake_txns())
    r = client.post("/api/coach/upload",
                    files={"file": ("shot.png", _png(), "image/png")})
    assert r.status_code == 200
    j = r.json()
    assert j["n_trades"] == 1
    assert not ocr_called  # vision handled it; OCR never touched
    assert any("spot-check" in w for w in j["warnings"])


def test_vision_failure_falls_back_to_ocr(client, monkeypatch):
    monkeypatch.setenv("AGENTICWHALES_VISION_PROVIDER", "google")

    def boom(*a, **kw):
        raise RuntimeError("vision model down")
    monkeypatch.setattr(extract_mod, "extract_transactions_from_image", boom)
    monkeypatch.setattr(coach_api, "_ocr_pdf_to_markdown",
                        lambda b: "Buy 50 AAPL @180; Sell 50 AAPL @184")
    monkeypatch.setattr(coach_api, "coach_extract_pdf",
                        lambda text, on_warn, **kw: _fake_txns())
    r = client.post("/api/coach/upload",
                    files={"file": ("shot.png", _png(), "image/png")})
    assert r.status_code == 200
    assert any("Vision read failed" in w for w in r.json()["warnings"])


def test_vision_disabled_keeps_ocr_path(client, monkeypatch):
    monkeypatch.delenv("AGENTICWHALES_VISION_PROVIDER", raising=False)
    monkeypatch.setattr(coach_api, "_ocr_pdf_to_markdown",
                        lambda b: "Buy 50 AAPL @180; Sell 50 AAPL @184")
    monkeypatch.setattr(coach_api, "coach_extract_pdf",
                        lambda text, on_warn, **kw: _fake_txns())
    r = client.post("/api/coach/upload",
                    files={"file": ("shot.png", _png(), "image/png")})
    assert r.status_code == 200 and r.json()["n_trades"] == 1


def test_guest_document_uploads_capped_per_ip(client, monkeypatch):
    monkeypatch.setenv("AGENTICWHALES_VISION_PROVIDER", "google")
    monkeypatch.setenv("AGENTICWHALES_GUEST_UPLOADS_PER_DAY", "2")
    monkeypatch.setattr(extract_mod, "extract_transactions_from_image",
                        lambda imgs, mimes=None, **kw: _fake_txns())
    for _ in range(2):
        assert client.post("/api/coach/upload",
                           files={"file": ("s.png", _png(), "image/png")}).status_code == 200
    r = client.post("/api/coach/upload",
                    files={"file": ("s.png", _png(), "image/png")})
    assert r.status_code == 429
    j = r.json()
    assert j["signin"] is True and "Sign in" in j["error"]


def test_guest_csv_uploads_not_capped(client, monkeypatch):
    monkeypatch.setenv("AGENTICWHALES_GUEST_UPLOADS_PER_DAY", "1")
    for _ in range(4):  # deterministic CSV parsing costs nothing — never capped
        r = client.post("/api/coach/upload",
                        files={"file": ("h.csv", _sample_csv(), "text/csv")})
        assert r.status_code == 200


def test_budget_exceeded_maps_to_429(client, monkeypatch):
    from agenticwhales.llm_clients.cost_middleware import BudgetExceeded

    monkeypatch.setenv("AGENTICWHALES_VISION_PROVIDER", "google")

    def broke(user_id):
        raise BudgetExceeded(2.5, 2.6)
    import agenticwhales.llm_clients.cost_middleware as cm
    monkeypatch.setattr(cm, "check_user_budget", broke)
    r = client.post("/api/coach/upload",
                    files={"file": ("s.png", _png(), "image/png")})
    assert r.status_code == 429


def test_multi_file_upload_async_merges(client, monkeypatch):
    monkeypatch.setenv("AGENTICWHALES_VISION_PROVIDER", "google")
    monkeypatch.setattr(extract_mod, "extract_transactions_from_image",
                        lambda imgs, mimes=None, **kw: _fake_txns())
    r = client.post(
        "/api/coach/upload_async",
        files=[("file", ("a.png", _png(), "image/png")),
               ("file", ("h.csv", _sample_csv(), "text/csv"))])
    assert r.status_code == 200
    jid = r.json()["job_id"]
    # job runs in the bounded pool; poll briefly
    import time
    for _ in range(100):
        if coach_api._JOBS[jid]["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    j = coach_api._JOBS[jid]
    assert j["status"] == "done"
    # image rows + identical CSV rows dedupe to one round-trip
    assert j["report"]["n_trades"] == 1


def test_spend_recorder_writes_real_ledger():
    uid = "spend-user-1"
    rec = coach_api._spend_recorder(uid, "google", "gemini-3-flash-preview")
    rec(100_000, 5_000)
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    row = auth._memstore.get(("user_spend_daily", f"{uid}|{today}"))
    assert row and row["total_cost_usd"] > 0
