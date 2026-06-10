"""API endpoints for the behavioral coach + pre-trade decision-support surface.

All deterministic and read-only — no orders, ever. Mounted on the main app via
`app.include_router(router)` in server.py.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import threading
import time
import uuid
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from agenticwhales import coach, decision_support, pretrade, prices
from agenticwhales.transactions.models import Transaction
from agenticwhales.transactions.parser import parse_transactions_csv
from web import auth
from web.auth import get_current_user_id

router = APIRouter()
log = logging.getLogger(__name__)


def optional_user_id(authorization: Optional[str] = Header(None)) -> str:
    """Like get_current_user_id, but never 401s — guests get ANONYMOUS_USER_ID so
    they can try the coach without signing in. Persistence is gated on a real id."""
    try:
        return get_current_user_id(authorization)
    except HTTPException:
        return auth.ANONYMOUS_USER_ID

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif")
# stride-gpu/ocr-mlx — DeepSeek-OCR-2 server (GET /status, POST /ocr, PDF-only).
_OCR_URL = os.getenv("AGENTICWHALES_OCR_URL", os.getenv("GPU_OCR_URL", "http://localhost:8000"))


class OcrUnavailable(RuntimeError):
    """The OCR service couldn't be reached / failed — surfaced as a clear 503."""


def _image_to_pdf(data: bytes) -> bytes:
    from PIL import Image
    img = Image.open(io.BytesIO(data)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PDF")
    return buf.getvalue()


def _ocr_pdf_to_markdown(pdf_bytes: bytes) -> str:
    """OCR a (scanned) PDF via the stride-gpu/ocr-mlx endpoint -> markdown text."""
    import requests
    url = _OCR_URL.rstrip("/") + "/ocr"
    try:
        resp = requests.post(
            url, files={"file": ("document.pdf", pdf_bytes, "application/pdf")},
            timeout=600,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        raise OcrUnavailable(str(exc)) from exc
    if isinstance(data, dict) and data.get("status") not in (None, "success", "ready"):
        raise OcrUnavailable(f"OCR returned status {data.get('status')}")
    content = data.get("content") if isinstance(data, dict) else data
    if isinstance(content, dict):
        content = content.get("body", "")
    return str(content or "")


def _persist_audit(user_id: str, report_dict: Dict, txns: List[Transaction]) -> None:
    """Save an audit (summary + raw trades) for signed-in users (no-op for guests)."""
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return
    try:
        auth.insert_coach_audit({
            "id": uuid.uuid4().hex,
            "user_id": user_id,
            "created_at": auth._ts_iso(time.time()),
            "discipline_score": float(report_dict.get("discipline_score", 0)),
            "total_pnl": float(report_dict.get("total_pnl", 0)),
            "disciplined_pnl": float(report_dict.get("disciplined_pnl", 0)),
            "n_trades": int(report_dict.get("n_trades", 0)),
            "leak_summary": [{"name": l["name"], "dollars": l["dollars"]}
                             for l in report_dict.get("leaks", [])],
            "transactions": [t.model_dump() for t in txns],
        })
    except Exception as exc:  # noqa: BLE001 — persistence must never break the audit
        log.warning("coach audit persist failed: %s", exc)


def _latest_user_trades(user_id: str) -> List[Transaction]:
    """The signed-in user's full merged trade history (empty for guests)."""
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return []
    out = []
    for t in auth.get_coach_trades(user_id):
        try:
            out.append(Transaction(**t))
        except Exception:  # noqa: BLE001
            continue
    return out


def _merge_user_trades(user_id: str, new_txns: List[Transaction]) -> List[Transaction]:
    """Union the new upload into the user's stored history (deduped) and persist it,
    so the timeline accumulates across uploads/syncs without re-uploading old data."""
    merged = coach.dedupe_transactions(_latest_user_trades(user_id) + list(new_txns))
    auth.save_coach_trades(user_id, [t.model_dump() for t in merged])
    return merged

# Provider used to extract transactions from PDF statements (DeepSeek key is in .env).
_EXTRACT_PROVIDER = "deepseek"
_EXTRACT_MODEL = "deepseek-chat"


def _pdf_to_text(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


class TxnIn(BaseModel):
    date: str = ""
    type: str = "Other"
    symbol: str = ""
    quantity: float = 0.0
    price: float = 0.0
    amount: float = 0.0


class AuditPayload(BaseModel):
    transactions: Optional[List[TxnIn]] = None
    csv_text: Optional[str] = None
    fees_paid: float = 0.0
    use_demo: bool = False


def _txns_from(p) -> List[Transaction]:
    if getattr(p, "use_demo", False):
        return coach.sample_history()
    if getattr(p, "csv_text", None):
        return parse_transactions_csv(p.csv_text)
    if getattr(p, "transactions", None):
        return [Transaction(**t.model_dump()) for t in p.transactions]
    return []


@router.post("/api/coach/audit")
async def coach_audit(p: AuditPayload, user_id: str = Depends(optional_user_id)):
    txns = _txns_from(p)
    if not txns:
        return JSONResponse({"error": "provide transactions, csv_text, or use_demo"},
                            status_code=400)
    audit_txns = txns
    if user_id and user_id != auth.ANONYMOUS_USER_ID:
        audit_txns = _merge_user_trades(user_id, txns)
    report = coach.audit_trades(audit_txns, fees_paid=p.fees_paid)
    out = report.to_dict()
    out["n_transactions"] = len(audit_txns)
    out["new_transactions"] = len(txns)
    _persist_audit(user_id, out, audit_txns)
    return out


@router.post("/api/coach/upload")
async def coach_upload(file: UploadFile = File(...),
                       user_id: str = Depends(optional_user_id)):
    """Accept a brokerage history as CSV or PDF. CSV is parsed deterministically;
    PDF text is extracted and passed through the LLM transaction extractor."""
    data = await file.read()
    name = (file.filename or "").lower()
    ctype = (file.content_type or "").lower()
    is_image = name.endswith(_IMAGE_EXTS) or ctype.startswith("image/")
    is_pdf = name.endswith(".pdf") or "pdf" in ctype
    warnings: List[str] = []
    txns: List[Transaction] = []
    text = ""

    try:
        if is_image:
            # Server is PDF-only; wrap the image in a one-page PDF, then OCR.
            text = _ocr_pdf_to_markdown(_image_to_pdf(data))
        elif is_pdf:
            text = _pdf_to_text(data)
            if len(text.strip()) < 40:  # scanned / image-only PDF -> OCR fallback
                warnings.append("PDF had little extractable text — used OCR.")
                text = _ocr_pdf_to_markdown(data)
        else:
            txns = parse_transactions_csv(data.decode("utf-8", errors="replace"))
    except OcrUnavailable as exc:
        log.warning("OCR unavailable: %s", exc)
        return JSONResponse(
            {"error": "This looks like a scanned document. The OCR service "
                      "(stride-gpu/ocr-mlx) isn't reachable — start it, set "
                      "AGENTICWHALES_OCR_URL, or upload a text-based CSV/PDF."},
            status_code=503)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"Could not read the file: {exc}"}, status_code=400)

    if (is_image or is_pdf) and not txns:
        if not text.strip():
            return JSONResponse(
                {"error": "No readable text found in the document."}, status_code=400)
        try:
            txns = coach_extract_pdf(text, warnings.append)
        except Exception as exc:  # noqa: BLE001
            log.warning("extraction failed: %s", exc)
            return JSONResponse({"error": f"Extraction failed: {exc}"}, status_code=400)

    if not txns:
        return JSONResponse({"error": "No transactions found in the file."}, status_code=400)
    report = coach.audit_trades(txns, price_fetcher=prices.fetch_ohlc)
    out = report.to_dict()
    out["warnings"] = warnings
    out["n_transactions"] = len(txns)
    _persist_audit(user_id, out, txns)
    return out


@router.get("/api/coach/history")
async def coach_history(user_id: str = Depends(optional_user_id)):
    """A signed-in user's past audit summaries, newest first — the score trend."""
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return {"signed_in": False, "audits": []}
    return {"signed_in": True, "audits": auth.list_coach_audits(user_id)}


@router.post("/api/coach/data/delete")
async def coach_data_delete(user_id: str = Depends(optional_user_id)):
    """Delete all of the signed-in user's uploaded data (trades + audit history)."""
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return JSONResponse({"error": "Sign in required."}, status_code=401)
    res = auth.delete_coach_data(user_id)
    return {"ok": True, **res}


@router.get("/api/coach/latest")
async def coach_latest(user_id: str = Depends(optional_user_id)):
    """The signed-in user's latest audit, recomputed from their persisted trades.
    Drives the returning-user dashboard (no re-upload needed)."""
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return {"signed_in": False, "has_audit": False}
    row = auth.get_latest_coach_audit(user_id)
    txns = _latest_user_trades(user_id)
    if not row or not txns:
        return {"signed_in": True, "has_audit": False}
    report = coach.audit_trades(txns)  # deterministic recompute; fast, no network
    out = report.to_dict()
    out["created_at"] = row.get("created_at")
    out["n_transactions"] = len(txns)
    return {"signed_in": True, "has_audit": True, "report": out}


_EXTRACT_CONCURRENCY = 8  # chunks are independent; extract them in parallel


def coach_extract_pdf(text: str, on_warn, on_progress=None) -> List[Transaction]:
    from agenticwhales.transactions.extract import extract_transactions
    return extract_transactions(text, provider=_EXTRACT_PROVIDER, model=_EXTRACT_MODEL,
                                on_warn=on_warn, on_progress=on_progress,
                                concurrency=_EXTRACT_CONCURRENCY)


# --------------------------------------------------------------------------- #
# Async upload with live progress (long PDFs / scanned docs can take minutes)
# --------------------------------------------------------------------------- #
# In-process job store. NOTE: single-process only — with multiple uvicorn
# workers the upload POST and the SSE GET must hit the same worker, so put a
# shared store (Supabase/redis) behind this before horizontal scaling.
_JOBS: Dict[str, Dict] = {}
_JOBS_LOCK = threading.Lock()


def _new_job() -> str:
    jid = uuid.uuid4().hex
    with _JOBS_LOCK:
        _JOBS[jid] = {"status": "pending", "stage": "received", "pct": 0,
                      "message": "Queued…", "report": None, "error": None,
                      "updated": time.time()}
    return jid


def _job_set(jid: str, **kw) -> None:
    with _JOBS_LOCK:
        j = _JOBS.get(jid)
        if j:
            j.update(kw)
            j["updated"] = time.time()


def _prune_jobs(max_age: float = 1800.0) -> None:
    now = time.time()
    with _JOBS_LOCK:
        for k in [k for k, v in _JOBS.items() if now - v.get("updated", now) > max_age]:
            _JOBS.pop(k, None)


def _pdf_pages(pdf_bytes: bytes) -> List[bytes]:
    """Split a PDF into one-page PDFs so OCR can report per-page progress."""
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(io.BytesIO(pdf_bytes))
    out = []
    for page in reader.pages:
        w = PdfWriter()
        w.add_page(page)
        buf = io.BytesIO()
        w.write(buf)
        out.append(buf.getvalue())
    return out


def _ocr_with_progress(jid: str, pdf_bytes: bytes) -> str:
    try:
        pages = _pdf_pages(pdf_bytes)
    except Exception:  # noqa: BLE001
        pages = [pdf_bytes]
    if len(pages) <= 1:
        _job_set(jid, stage="ocr", pct=30, message="Running OCR…")
        return _ocr_pdf_to_markdown(pdf_bytes)
    md = []
    n = len(pages)
    for i, pg in enumerate(pages, 1):
        _job_set(jid, stage="ocr", pct=8 + int(46 * (i - 1) / n),
                 message=f"Reading the document — OCR page {i} of {n}…")
        md.append(_ocr_pdf_to_markdown(pg))
    return "\n\n".join(md)


def _run_upload_job(jid: str, data: bytes, name: str, ctype: str, user_id: str) -> None:
    name = (name or "").lower()
    ctype = (ctype or "").lower()
    is_image = name.endswith(_IMAGE_EXTS) or ctype.startswith("image/")
    is_pdf = name.endswith(".pdf") or "pdf" in ctype
    warnings: List[str] = []
    txns: List[Transaction] = []
    text = ""
    try:
        _job_set(jid, status="running", stage="reading", pct=5, message="Reading the file…")
        ocr_used = False
        if is_image:
            ocr_used = True
            text = _ocr_with_progress(jid, _image_to_pdf(data))
        elif is_pdf:
            text = _pdf_to_text(data)
            if len(text.strip()) < 40:
                ocr_used = True
                warnings.append("Scanned PDF — used OCR.")
                text = _ocr_with_progress(jid, data)
        else:
            txns = parse_transactions_csv(data.decode("utf-8", errors="replace"))

        if (is_image or is_pdf) and not txns:
            if not text.strip():
                raise ValueError("No readable text found in the document.")
            # No OCR phase (text PDF) -> extraction owns the whole bar (10-92%);
            # with OCR (8-54%) it picks up from 55%. Avoids an unearned jump.
            base, span = (55, 37) if ocr_used else (10, 82)
            _job_set(jid, stage="extract", pct=base, message="Extracting transactions…")
            txns = coach_extract_pdf(
                text, warnings.append,
                on_progress=lambda i, n: _job_set(
                    jid, stage="extract", pct=base + int(span * i / max(n, 1)),
                    message=f"Extracting transactions ({i} of {n})…"))

        if not txns:
            raise ValueError("No transactions found in the file.")
        _job_set(jid, stage="audit", pct=92, message="Analyzing your habits…")
        audit_txns = txns
        if user_id and user_id != auth.ANONYMOUS_USER_ID:
            audit_txns = _merge_user_trades(user_id, txns)  # accumulate the timeline
        report = coach.audit_trades(audit_txns, price_fetcher=prices.fetch_ohlc)
        out = report.to_dict()
        out["warnings"] = warnings
        out["n_transactions"] = len(audit_txns)
        out["new_transactions"] = len(txns)
        _persist_audit(user_id, out, audit_txns)
        _job_set(jid, status="done", stage="done", pct=100, message="Done.", report=out)
    except OcrUnavailable:
        _job_set(jid, status="error", stage="error",
                 error="This looks like a scanned document, but the OCR service "
                       "(stride-gpu/ocr-mlx) isn't reachable. Start it, set "
                       "AGENTICWHALES_OCR_URL, or upload a text-based CSV/PDF.")
    except Exception as exc:  # noqa: BLE001
        _job_set(jid, status="error", stage="error", error=str(exc))


@router.post("/api/coach/upload_async")
async def coach_upload_async(file: UploadFile = File(...),
                             user_id: str = Depends(optional_user_id)):
    """Start an upload+analyze job; returns a job_id to stream progress from."""
    data = await file.read()
    _prune_jobs()
    jid = _new_job()
    threading.Thread(
        target=_run_upload_job,
        args=(jid, data, file.filename, file.content_type, user_id),
        daemon=True,
    ).start()
    return {"job_id": jid}


@router.get("/api/coach/jobs/{job_id}/stream")
async def coach_job_stream(job_id: str):
    """Server-Sent Events of a job's progress. Unauthenticated — the random
    job_id is the capability (persistence already happened under the uploader)."""
    async def gen():
        last = None
        quiet = 0
        for _ in range(3200):  # ~21 min ceiling at 0.4s
            with _JOBS_LOCK:
                j = _JOBS.get(job_id)
                snap = None if j is None else {
                    k: j.get(k) for k in ("status", "stage", "pct", "message", "error")}
                if j is not None and j.get("status") == "done":
                    snap["report"] = j.get("report")
            if snap is None:
                yield "data: " + json.dumps({"status": "error", "error": "unknown or expired job"}) + "\n\n"
                return
            if snap != last:
                yield "data: " + json.dumps(snap) + "\n\n"
                last = snap
                quiet = 0
            else:
                quiet += 1
                if quiet >= 12:  # ~5s of silence (e.g. a slow OCR page) -> heartbeat
                    yield ": keepalive\n\n"
                    quiet = 0
            if snap["status"] in ("done", "error"):
                return
            await asyncio.sleep(0.4)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/coach/demo")
async def coach_demo():
    report = coach.audit_trades(coach.sample_history())
    return report.to_dict()


class TradeIn(BaseModel):
    symbol: str = ""
    side: str = "long"
    qty: float = 0.0
    entry_price: float = 0.0
    stop_price: Optional[float] = None
    target_price: Optional[float] = None


class PretradePayload(BaseModel):
    trade: TradeIn
    equity: float = 100_000.0
    use_demo_history: bool = False
    transactions: Optional[List[TxnIn]] = None
    max_risk_pct: float = 0.02
    include_ai: bool = True


@router.post("/api/pretrade/check")
async def pretrade_check(p: PretradePayload, user_id: str = Depends(optional_user_id)):
    recent = None
    profile = None
    txns: List[Transaction] = []
    if p.use_demo_history:
        txns = coach.sample_history()
    elif p.transactions:
        txns = [Transaction(**t.model_dump()) for t in p.transactions]
    else:
        # Signed-in: check against the user's OWN history automatically.
        txns = _latest_user_trades(user_id)
    if txns:
        recent = coach.reconstruct_round_trips(txns)
        profile = coach.audit_trades(txns)
    trade = pretrade.ProposedTrade(**p.trade.model_dump())
    verdict = pretrade.check_trade(
        trade, equity=p.equity, recent_trades=recent,
        leak_profile=profile, max_risk_pct=p.max_risk_pct,
    )
    out = verdict.to_dict()
    # Decision support: a fast technical read on the symbol (+ optional LLM note).
    if p.trade.symbol:
        out["decision_support"] = decision_support.analyze_symbol(
            p.trade.symbol, llm_note=p.include_ai)
    return out
