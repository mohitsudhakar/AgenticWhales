"""Tests for transaction persistence (auth storage) + the /api/transactions
and /api/signals/transactions endpoints. In-memory fallback; no Supabase.
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from web import auth, server


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(auth, "_db_writable", lambda: False)
    auth._reset_memstore_for_tests()
    yield
    auth._reset_memstore_for_tests()


@pytest.fixture
def client():
    c = TestClient(server.app)
    c.follow_redirects = False
    return c


# ---------------------------------------------------------------------------
# storage helpers
# ---------------------------------------------------------------------------

def _rows(user_id, batch_id="b1", n=2):
    return [
        {"id": f"{user_id}-{batch_id}-t{i}", "user_id": user_id, "batch_id": batch_id,
         "source": "csv_upload", "txn_date": "2024-01-0%d" % (i + 1),
         "type": "Buy", "symbol": "AAPL", "description": "",
         "quantity": 1.0, "price": 100.0, "amount": -100.0,
         "created_at": "2024-01-0%dT00:00:00Z" % (i + 1)}
        for i in range(n)
    ]


def test_save_and_list_transactions():
    n = auth.save_transactions(_rows("u1", n=3))
    assert n == 3
    got = auth.list_transactions("u1")
    assert len(got) == 3
    assert all(r["user_id"] == "u1" for r in got)


def test_list_transactions_scoped_by_user():
    auth.save_transactions(_rows("u1", n=2))
    auth.save_transactions(_rows("u2", batch_id="b2", n=1))
    assert len(auth.list_transactions("u1")) == 2
    assert len(auth.list_transactions("u2")) == 1


def test_list_transactions_filter_by_batch():
    auth.save_transactions(_rows("u1", batch_id="bA", n=2))
    rows_b = _rows("u1", batch_id="bB", n=1)
    rows_b[0]["id"] = "tb"
    auth.save_transactions(rows_b)
    assert len(auth.list_transactions("u1", batch_id="bA")) == 2
    assert len(auth.list_transactions("u1", batch_id="bB")) == 1


def test_list_transactions_respects_limit():
    auth.save_transactions(_rows("u1", n=5))
    assert len(auth.list_transactions("u1", limit=2)) == 2


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------

_CSV = (
    "Activity Date,Trans Code,Instrument,Description,Quantity,Price,Amount\n"
    "2024-01-15,Buy,AAPL,Apple,10,150.00,-1500.00\n"
    "2024-03-10,Sell,AAPL,Apple,8,180.00,1440.00\n"
)


def test_upload_persists_for_signed_in_user(client):
    files = {"file": ("trades.csv", io.BytesIO(_CSV.encode()), "text/csv")}
    r = client.post("/api/signals/transactions", files=files, data={"run_llm": "false"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["persisted"] is True
    assert body["saved_count"] == 2
    assert "batch_id" in body
    # And they're now listable.
    r2 = client.get("/api/transactions")
    assert r2.status_code == 200
    assert len(r2.json()) == 2


def test_upload_empty_csv_400(client):
    files = {"file": ("e.csv", io.BytesIO(b"not,a,known,header\n"), "text/csv")}
    r = client.post("/api/signals/transactions", files=files, data={"run_llm": "false"})
    assert r.status_code == 400


def test_transactions_metrics_recomputes_from_saved(client):
    files = {"file": ("trades.csv", io.BytesIO(_CSV.encode()), "text/csv")}
    client.post("/api/signals/transactions", files=files, data={"run_llm": "false"})
    r = client.get("/api/transactions/metrics")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert body["metrics"]["total_transactions"] == 2


def test_transactions_metrics_empty_when_none(client):
    r = client.get("/api/transactions/metrics")
    assert r.status_code == 200
    assert r.json().get("count", 0) == 0
