"""API tests for the coach surface — TestClient, no network."""

import pytest
from fastapi.testclient import TestClient

import web.server as server
import web.coach_api as coach_api
from agenticwhales import coach


@pytest.fixture
def client():
    return TestClient(server.app)


def _sample_csv() -> str:
    rows = ["Date,Type,Symbol,Quantity,Price,Amount"]
    for t in coach.sample_history():
        rows.append(f"{t.date},{t.type},{t.symbol},{t.quantity},{t.price},{t.amount}")
    return "\n".join(rows)


def _minimal_pdf(text: str) -> bytes:
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
    ]
    content = b"BT /F1 10 Tf 50 740 Td 12 TL\n"
    for ln in text.split("\n"):
        content += b"(" + ln.replace("(", "").replace(")", "").encode("latin-1", "replace") + b") Tj T*\n"
    content += b"ET"
    objs.append(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    pdf = b"%PDF-1.4\n"
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(pdf))
        pdf += str(i).encode() + b" 0 obj\n" + o + b"\nendobj\n"
    xref = len(pdf)
    pdf += b"xref\n0 " + str(len(objs) + 1).encode() + b"\n0000000000 65535 f \n"
    for off in offs:
        pdf += ("%010d 00000 n \n" % off).encode()
    pdf += (b"trailer\n<< /Size " + str(len(objs) + 1).encode() + b" /Root 1 0 R >>\n"
            b"startxref\n" + str(xref).encode() + b"\n%%EOF")
    return pdf


def test_demo_endpoint(client):
    r = client.get("/api/coach/demo")
    assert r.status_code == 200
    j = r.json()
    assert j["n_trades"] == 12 and 0 <= j["discipline_score"] <= 100 and len(j["leaks"]) >= 1


def test_csv_upload(client):
    r = client.post("/api/coach/upload", files={"file": ("history.csv", _sample_csv(), "text/csv")})
    assert r.status_code == 200
    j = r.json()
    assert j["n_transactions"] == 24 and j["n_trades"] == 12


def test_upload_empty_rejected(client):
    r = client.post("/api/coach/upload", files={"file": ("x.csv", "Date,Type,Symbol\n", "text/csv")})
    assert r.status_code == 400


def test_pdf_text_extraction():
    pdf = _minimal_pdf("2025-01-06 Buy 50 AAPL @ 180.00 -9000.00")
    text = coach_api._pdf_to_text(pdf)
    assert "AAPL" in text and "180" in text


def test_pdf_upload_routes_through_extractor(client, monkeypatch):
    # Avoid the network: stub the LLM extractor.
    from agenticwhales.transactions.models import Transaction
    fake = [
        Transaction(date="2025-01-06", type="Buy", symbol="AAPL", quantity=50, price=180, amount=-9000),
        Transaction(date="2025-01-08", type="Sell", symbol="AAPL", quantity=50, price=184, amount=9200),
    ]
    monkeypatch.setattr(coach_api, "coach_extract_pdf", lambda text, on_warn: fake)
    pdf = _minimal_pdf("2025-01-06 Buy 50 AAPL @ 180\n2025-01-08 Sell 50 AAPL @ 184")
    r = client.post("/api/coach/upload", files={"file": ("stmt.pdf", pdf, "application/pdf")})
    assert r.status_code == 200
    assert r.json()["n_trades"] == 1


def _png_bytes() -> bytes:
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), (255, 255, 255)).save(buf, "PNG")
    return buf.getvalue()


def _fake_txns():
    from agenticwhales.transactions.models import Transaction
    return [
        Transaction(date="2025-01-06", type="Buy", symbol="AAPL", quantity=50, price=180, amount=-9000),
        Transaction(date="2025-01-08", type="Sell", symbol="AAPL", quantity=50, price=184, amount=9200),
    ]


def test_image_to_pdf():
    assert coach_api._image_to_pdf(_png_bytes())[:4] == b"%PDF"


def test_image_upload_routes_through_ocr(client, monkeypatch):
    monkeypatch.setattr(coach_api, "_ocr_pdf_to_markdown", lambda b: "Buy 50 AAPL @180; Sell 50 AAPL @184")
    monkeypatch.setattr(coach_api, "coach_extract_pdf", lambda text, on_warn: _fake_txns())
    r = client.post("/api/coach/upload", files={"file": ("scan.png", _png_bytes(), "image/png")})
    assert r.status_code == 200 and r.json()["n_trades"] == 1


def test_scanned_pdf_falls_back_to_ocr(client, monkeypatch):
    called = {}

    def ocr(b):
        called["ocr"] = True
        return "AAPL buy/sell text"
    monkeypatch.setattr(coach_api, "_ocr_pdf_to_markdown", ocr)
    monkeypatch.setattr(coach_api, "coach_extract_pdf", lambda text, on_warn: _fake_txns())
    pdf = _minimal_pdf("")  # no extractable text -> OCR fallback
    r = client.post("/api/coach/upload", files={"file": ("scan.pdf", pdf, "application/pdf")})
    assert r.status_code == 200 and called.get("ocr") and r.json()["n_trades"] == 1


def test_ocr_unavailable_returns_503(client, monkeypatch):
    def boom(b):
        raise coach_api.OcrUnavailable("connection refused")
    monkeypatch.setattr(coach_api, "_ocr_pdf_to_markdown", boom)
    r = client.post("/api/coach/upload", files={"file": ("scan.png", _png_bytes(), "image/png")})
    assert r.status_code == 503


def test_pdf_pages_split():
    assert len(coach_api._pdf_pages(_minimal_pdf("one page"))) == 1


def test_run_upload_job_csv_completes():
    jid = coach_api._new_job()
    coach_api._run_upload_job(jid, _sample_csv().encode(), "h.csv", "text/csv", "anonymous")
    j = coach_api._JOBS[jid]
    assert j["status"] == "done" and j["report"]["n_trades"] == 12 and j["pct"] == 100


def test_run_upload_job_scanned_pdf_uses_ocr(monkeypatch):
    monkeypatch.setattr(coach_api, "_ocr_pdf_to_markdown", lambda b: "AAPL buy/sell text")
    monkeypatch.setattr(coach_api, "coach_extract_pdf",
                        lambda text, on_warn, on_progress=None: _fake_txns())
    jid = coach_api._new_job()
    coach_api._run_upload_job(jid, _minimal_pdf(""), "scan.pdf", "application/pdf", "anonymous")
    assert coach_api._JOBS[jid]["status"] == "done"
    assert coach_api._JOBS[jid]["report"]["n_trades"] == 1


def test_upload_async_returns_job_id(client):
    r = client.post("/api/coach/upload_async", files={"file": ("h.csv", _sample_csv(), "text/csv")})
    assert r.status_code == 200 and "job_id" in r.json()


def test_job_stream_emits_done_with_report(client):
    jid = coach_api._new_job()
    coach_api._job_set(jid, status="done", stage="done", pct=100, message="Done.",
                       report={"discipline_score": 14, "n_trades": 12})
    body = client.get(f"/api/coach/jobs/{jid}/stream").text
    assert '"status": "done"' in body and '"discipline_score": 14' in body


def test_job_stream_unknown_job():
    from fastapi.testclient import TestClient
    body = TestClient(server.app).get("/api/coach/jobs/nope/stream").text
    assert "unknown or expired job" in body


def test_pretrade_endpoint_blocks_revenge(client, monkeypatch):
    import agenticwhales.decision_support as ds
    monkeypatch.setattr(ds, "analyze_symbol", lambda *a, **k: {"available": False, "symbol": "NVDA"})
    r = client.post("/api/pretrade/check", json={
        "trade": {"symbol": "NVDA", "side": "long", "qty": 300, "entry_price": 120},
        "equity": 200000, "use_demo_history": True, "include_ai": False})
    assert r.status_code == 200
    assert r.json()["verdict"] == "BLOCK"


def test_history_guest_is_empty(client):
    h = client.get("/api/coach/history").json()
    assert h["signed_in"] is False and h["audits"] == []


def test_audit_persists_and_history_returns_summary(client):
    from web.auth import get_current_user_id
    server.app.dependency_overrides[coach_api.optional_user_id] = lambda: "coach-test-user-1"
    try:
        assert client.post("/api/coach/audit", json={"use_demo": True}).status_code == 200
        h = client.get("/api/coach/history").json()
        assert h["signed_in"] is True
        assert len(h["audits"]) >= 1
        a = h["audits"][0]
        assert "discipline_score" in a and "n_trades" in a
        assert "transactions" not in a  # history is summary-only (light payload)
    finally:
        server.app.dependency_overrides.clear()


def test_latest_recomputes_full_report_from_persisted_trades(client):
    from web.auth import get_current_user_id
    server.app.dependency_overrides[coach_api.optional_user_id] = lambda: "coach-test-user-2"
    try:
        client.post("/api/coach/audit", json={"use_demo": True})
        r = client.get("/api/coach/latest").json()
        assert r["signed_in"] and r["has_audit"]
        rep = r["report"]
        assert rep["n_trades"] == 12 and r["report"]["n_transactions"] == 24
        assert len(rep["leaks"]) >= 1 and "evidence" in rep["leaks"][0]  # full detail, not just summary
    finally:
        server.app.dependency_overrides.clear()


def test_latest_no_audit_for_fresh_user(client):
    from web.auth import get_current_user_id
    server.app.dependency_overrides[coach_api.optional_user_id] = lambda: "coach-fresh-user"
    try:
        r = client.get("/api/coach/latest").json()
        assert r["signed_in"] is True and r["has_audit"] is False
    finally:
        server.app.dependency_overrides.clear()


def test_pretrade_uses_signed_in_users_history(client, monkeypatch):
    import agenticwhales.decision_support as ds
    monkeypatch.setattr(ds, "analyze_symbol", lambda *a, **k: {"available": False, "symbol": "NVDA"})
    from web.auth import get_current_user_id
    server.app.dependency_overrides[coach_api.optional_user_id] = lambda: "coach-test-user-3"
    try:
        client.post("/api/coach/audit", json={"use_demo": True})  # persist their (demo) history
        # No demo flag, no transactions -> server should load THEIR history and catch the revenge trade.
        r = client.post("/api/pretrade/check", json={
            "trade": {"symbol": "NVDA", "side": "long", "qty": 300, "entry_price": 120},
            "equity": 200000, "use_demo_history": False, "include_ai": False}).json()
        assert r["verdict"] == "BLOCK"
        assert any("revenge" in c["name"].lower() for c in r["checks"])
    finally:
        server.app.dependency_overrides.clear()


def test_pretrade_includes_decision_support(client, monkeypatch):
    import agenticwhales.decision_support as ds
    monkeypatch.setattr(ds, "analyze_symbol", lambda *a, **k: {
        "available": True, "symbol": "NVDA", "rating": "Buy", "score": 0.5,
        "last_price": 120.0, "signals": [{"name": "trend", "direction": 1, "strength": 0.8, "notes": ""}],
        "summary": "uptrend"})
    r = client.post("/api/pretrade/check", json={
        "trade": {"symbol": "NVDA", "qty": 10, "entry_price": 120, "stop_price": 115},
        "equity": 100000, "include_ai": False})
    ds_out = r.json()["decision_support"]
    assert ds_out["available"] and ds_out["rating"] == "Buy"
