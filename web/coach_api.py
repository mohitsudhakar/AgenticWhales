"""API endpoints for the behavioral coach + pre-trade decision-support surface.

All deterministic and read-only — no orders, ever. Mounted on the main app via
`app.include_router(router)` in server.py.
"""

from __future__ import annotations

import io
import logging
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


def _persist_audit(user_id: str, report_dict: Dict) -> None:
    """Save an audit summary for signed-in users (no-op for guests)."""
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
        })
    except Exception as exc:  # noqa: BLE001 — persistence must never break the audit
        log.warning("coach audit persist failed: %s", exc)

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
    _persist_audit(user_id, out)
    return out


@router.post("/api/coach/upload")
async def coach_upload(file: UploadFile = File(...),
                       user_id: str = Depends(get_current_user_id)):
    """Accept a brokerage history as CSV or PDF. CSV is parsed deterministically;
    PDF text is extracted and passed through the LLM transaction extractor."""
    data = await file.read()
    name = (file.filename or "").lower()
    ctype = (file.content_type or "").lower()
    warnings: List[str] = []
    txns: List[Transaction] = []

    if name.endswith(".pdf") or "pdf" in ctype:
        try:
            text = _pdf_to_text(data)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": f"Could not read the PDF: {exc}"}, status_code=400)
        if not text.strip():
            return JSONResponse(
                {"error": "No text found in the PDF — it may be a scanned image (OCR not supported yet)."},
                status_code=400)
        try:
            txns = coach_extract_pdf(text, warnings.append)
        except Exception as exc:  # noqa: BLE001
            log.warning("pdf extraction failed: %s", exc)
            return JSONResponse({"error": f"PDF extraction failed: {exc}"}, status_code=400)
    else:
        text = data.decode("utf-8", errors="replace")
        try:
            txns = parse_transactions_csv(text)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": f"Could not parse the CSV: {exc}"}, status_code=400)

    if not txns:
        return JSONResponse({"error": "No transactions found in the file."}, status_code=400)
    report = coach.audit_trades(txns, price_fetcher=prices.fetch_ohlc)
    out = report.to_dict()
    out["warnings"] = warnings
    out["n_transactions"] = len(txns)
    _persist_audit(user_id, out)
    return out


@router.get("/api/coach/history")
async def coach_history(user_id: str = Depends(get_current_user_id)):
    """A signed-in user's past audits, newest first — powers the score-over-time chart."""
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return {"signed_in": False, "audits": []}
    return {"signed_in": True, "audits": auth.list_coach_audits(user_id)}


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
async def pretrade_check(p: PretradePayload):
    recent = None
    profile = None
    txns: List[Transaction] = []
    if p.use_demo_history:
        txns = coach.sample_history()
    elif p.transactions:
        txns = [Transaction(**t.model_dump()) for t in p.transactions]
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
