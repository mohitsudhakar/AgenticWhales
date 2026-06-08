"""API endpoints for the behavioral coach + pre-trade decision-support surface.

All deterministic and read-only — no orders, ever. Mounted on the main app via
`app.include_router(router)` in server.py.
"""

from __future__ import annotations

import io
import logging
import os
import time
import uuid
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agenticwhales import coach, decision_support, pretrade, prices
from agenticwhales.transactions.models import Transaction
from agenticwhales.transactions.parser import parse_transactions_csv
from web import auth
from web.auth import get_current_user_id

router = APIRouter()
log = logging.getLogger(__name__)

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
    """The signed-in user's most recently-uploaded trades (empty for guests)."""
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return []
    row = auth.get_latest_coach_audit(user_id)
    raw = (row or {}).get("transactions") or []
    out = []
    for t in raw:
        try:
            out.append(Transaction(**t))
        except Exception:  # noqa: BLE001
            continue
    return out

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
async def coach_audit(p: AuditPayload, user_id: str = Depends(get_current_user_id)):
    txns = _txns_from(p)
    if not txns:
        return JSONResponse({"error": "provide transactions, csv_text, or use_demo"},
                            status_code=400)
    report = coach.audit_trades(txns, fees_paid=p.fees_paid)
    out = report.to_dict()
    _persist_audit(user_id, out, txns)
    return out


@router.post("/api/coach/upload")
async def coach_upload(file: UploadFile = File(...),
                       user_id: str = Depends(get_current_user_id)):
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
async def coach_history(user_id: str = Depends(get_current_user_id)):
    """A signed-in user's past audit summaries, newest first — the score trend."""
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return {"signed_in": False, "audits": []}
    return {"signed_in": True, "audits": auth.list_coach_audits(user_id)}


@router.get("/api/coach/latest")
async def coach_latest(user_id: str = Depends(get_current_user_id)):
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


def coach_extract_pdf(text: str, on_warn) -> List[Transaction]:
    from agenticwhales.transactions.extract import extract_transactions
    return extract_transactions(text, provider=_EXTRACT_PROVIDER, model=_EXTRACT_MODEL,
                                on_warn=on_warn)


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
async def pretrade_check(p: PretradePayload, user_id: str = Depends(get_current_user_id)):
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
