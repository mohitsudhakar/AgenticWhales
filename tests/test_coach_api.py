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


def test_pretrade_endpoint_blocks_revenge(client):
    r = client.post("/api/pretrade/check", json={
        "trade": {"symbol": "NVDA", "side": "long", "qty": 300, "entry_price": 120},
        "equity": 200000, "use_demo_history": True})
    assert r.status_code == 200
    assert r.json()["verdict"] == "BLOCK"
