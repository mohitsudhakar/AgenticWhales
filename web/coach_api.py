"""API endpoints for the behavioral coach + pre-trade decision-support surface.

All deterministic and read-only — no orders, ever. Mounted on the main app via
`app.include_router(router)` in server.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from agenticwhales import audit as audit_mod
from agenticwhales import coach, decision_support, pretrade, prices
from agenticwhales import coach_rules as rules_mod
from agenticwhales.transactions import extract as extract_mod
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

# J2 guardrail: ALL heavy upload work (OCR round-trips, chunked LLM extraction,
# price-path audits) runs in this small bounded pool — never inline on the event
# loop, and never one unbounded thread per upload. Excess jobs queue ("Queued…"
# is the job's initial state, so the UI already tells the user).
_UPLOAD_POOL = ThreadPoolExecutor(
    max_workers=int(os.getenv("AGENTICWHALES_COACH_UPLOAD_WORKERS", "2")),
    thread_name_prefix="coach-upload",
)


class OcrUnavailable(RuntimeError):
    """The OCR service couldn't be reached / failed — surfaced as a clear 503."""


class GuestLimitExceeded(RuntimeError):
    """A guest hit the free upload/spend cap — surfaced as 429 + sign-in CTA."""


# --------------------------------------------------------------------------- #
# Guest abuse / cost control.
#
# Two layers: (1) per-IP + global daily upload counters (in-memory — the job
# store is already single-process, see _JOBS); (2) REAL spend accounting: every
# extraction LLM call reports token usage, we price it and record it under the
# caller's user id (guests share ANONYMOUS_USER_ID), so cost_middleware's
# check_user_budget is an enforced backstop instead of a read of a ledger
# nothing writes.
# --------------------------------------------------------------------------- #
_GUEST_UPLOADS: Dict[tuple, int] = {}
_GUEST_LOCK = threading.Lock()


def _client_ip(request: Optional[Request]) -> str:
    if request is None:
        return "unknown"
    # Forwarded headers are spoofable unless a trusted edge sets them.
    if os.getenv("AGENTICWHALES_TRUST_PROXY_IP", "0") == "1":
        ip = (request.headers.get("fly-client-ip")
              or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip())
        if ip:
            return ip
    return request.client.host if request.client else "unknown"


def _needs_guest_cap(items: List[tuple]) -> bool:
    """Caps apply to documents that can trigger LLM/OCR work (images, PDFs).
    Plain CSV is parsed deterministically at zero marginal cost — uncapped."""
    for _, name, ctype in items:
        name = (name or "").lower()
        ctype = (ctype or "").lower()
        if (name.endswith(_IMAGE_EXTS) or ctype.startswith("image/")
                or name.endswith(".pdf") or "pdf" in ctype):
            return True
    return False


def _check_guest_upload_cap(request: Optional[Request]) -> None:
    """Count one guest upload; raise GuestLimitExceeded over the daily caps."""
    import datetime as _dt
    today = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
    per_ip = int(os.getenv("AGENTICWHALES_GUEST_UPLOADS_PER_DAY", "3"))
    global_cap = int(os.getenv("AGENTICWHALES_GUEST_UPLOADS_GLOBAL_PER_DAY", "200"))
    ip = _client_ip(request)
    with _GUEST_LOCK:
        for key in [k for k in _GUEST_UPLOADS if k[1] != today]:  # prune old days
            _GUEST_UPLOADS.pop(key, None)
        ip_key = (ip, today)
        g_key = ("__global__", today)
        if _GUEST_UPLOADS.get(g_key, 0) >= global_cap:
            raise GuestLimitExceeded("The free guest tier is at capacity today.")
        if _GUEST_UPLOADS.get(ip_key, 0) >= per_ip:
            raise GuestLimitExceeded(
                "You've used today's free guest analyses from this network.")
        _GUEST_UPLOADS[ip_key] = _GUEST_UPLOADS.get(ip_key, 0) + 1
        _GUEST_UPLOADS[g_key] = _GUEST_UPLOADS.get(g_key, 0) + 1


def _guest_limit_response(user_id: str, exc: GuestLimitExceeded) -> JSONResponse:
    is_guest = not user_id or user_id == auth.ANONYMOUS_USER_ID
    return JSONResponse(
        {"error": f"{exc} Sign in (free) to keep going — your audits also get "
                  "saved so your discipline trend builds over time." if is_guest
         else str(exc),
         "signin": is_guest},
        status_code=429)


def _spend_recorder(user_id: str, provider: str, model: str):
    """on_usage callback: price each extraction call and record it durably, so
    daily budget checks read real numbers."""
    import datetime as _dt
    from agenticwhales.llm_clients import pricing

    uid = user_id or auth.ANONYMOUS_USER_ID

    def on_usage(input_tokens: int, output_tokens: int) -> None:
        try:
            cost = float(pricing.cost_for(provider, model,
                                          input_tokens=input_tokens,
                                          output_tokens=output_tokens))
            if cost > 0:
                today = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
                auth.add_user_spend(uid, today, cost)
        except Exception as exc:  # noqa: BLE001
            log.warning("spend record failed: %s", exc)

    return on_usage


def _check_llm_budget(user_id: str) -> None:
    """Pre-LLM budget gate; guests share one ANONYMOUS ledger (global backstop)."""
    from agenticwhales.llm_clients.cost_middleware import BudgetExceeded, check_user_budget
    try:
        check_user_budget(user_id or auth.ANONYMOUS_USER_ID)
    except BudgetExceeded as exc:
        raise GuestLimitExceeded("Today's free analysis budget is used up.") from exc


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


# --------------------------------------------------------------------------- #
# Benchmark cohort — ONE shared scan + TTL cache over coach_audits (also the
# basis for the public stats page). The cache is load-bearing: without it every
# audit would trigger a full-table PostgREST read.
# --------------------------------------------------------------------------- #
_COHORT_CACHE: Dict = {"scores": None, "at": 0.0}
_COHORT_LOCK = threading.Lock()
_COHORT_TTL = 3600.0


def _cohort_scores(force: bool = False) -> List[float]:
    """Blocking on cache miss — call from a thread (or via the upload pool)."""
    now = time.time()
    with _COHORT_LOCK:
        if (not force and _COHORT_CACHE["scores"] is not None
                and now - _COHORT_CACHE["at"] < _COHORT_TTL):
            return _COHORT_CACHE["scores"]
    scores = auth.list_cohort_scores()
    with _COHORT_LOCK:
        _COHORT_CACHE["scores"] = scores
        _COHORT_CACHE["at"] = now
    return scores


def _benchmark_for(score: float) -> Dict:
    from agenticwhales import benchmarks
    try:
        return benchmarks.score_benchmark(score, _cohort_scores())
    except Exception as exc:  # noqa: BLE001 — benchmarks are a bonus, never fatal
        log.warning("benchmark failed: %s", exc)
        return {"cohort": "pending", "n_cohort": 0, "unlock_at": 0, "label": ""}


# --------------------------------------------------------------------------- #
# Product events — ONE vocabulary for funnel + product instrumentation.
# Allowlisted names only (fixed Prometheus cardinality). Anonymous events count
# in metrics but are NEVER written durably (no audit_log row, no memstore growth
# from unauthenticated traffic); signed-in events also land in audit_log so the
# admin funnel can be computed from durable rows.
# --------------------------------------------------------------------------- #
COACH_EVENTS = frozenset({
    "demo_viewed", "upload_started", "audit_viewed", "share_card_exported",
    "landing_cta_clicked", "sync_connected", "broker_connected",
    "pricing_viewed", "founding_reserved",
    # Premium-feature funnel (docs/product/premium-features-spec.md):
    "eval_configured", "standing_brief_configured", "violation_alert_sent",
    "upgrade_nudge_clicked",
})


def track_event(event: str, user_id: Optional[str], metadata: Optional[Dict] = None) -> None:
    """Record one allowlisted product event. Never raises."""
    if event not in COACH_EVENTS:
        return
    try:
        from agenticwhales.observability import METRICS
        if METRICS.enabled:
            METRICS.coach_event.labels(event=event).inc()
    except Exception:  # noqa: BLE001
        pass
    if user_id and user_id != auth.ANONYMOUS_USER_ID:
        try:
            audit_mod.audit(actor=user_id, action=event, target_user_id=user_id,
                            metadata=metadata or {})
        except Exception as exc:  # noqa: BLE001
            log.warning("event persist failed (%s): %s", event, exc)


class EventPayload(BaseModel):
    event: str = ""
    metadata: Optional[Dict] = None


@router.post("/api/coach/events")
async def coach_event(p: EventPayload, user_id: str = Depends(optional_user_id)):
    """Client-side event sink (share/CTA moments the server can't observe).
    Allowlist-only; guests count in metrics but write nothing durable."""
    if p.event not in COACH_EVENTS:
        return JSONResponse({"error": "unknown event"}, status_code=400)
    # Metadata is caller-supplied: keep only small scalar values.
    meta = {k: v for k, v in (p.metadata or {}).items()
            if isinstance(v, (str, int, float, bool)) and len(str(v)) <= 200}
    track_event(p.event, user_id, meta)
    return {"ok": True}


def _persist_audit(user_id: str, report_dict: Dict, txns: List[Transaction],
                   *, origin: str = "upload") -> None:
    """Save an audit (summary + raw trades) for signed-in users (no-op for guests).

    `origin` ("upload" | "sync_manual" | "sync_auto") is stored on the row so
    retention metrics can separate user-initiated audits from the nightly
    cron's autonomous ones — otherwise D30 retention would measure scheduler
    uptime, not users coming back."""
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return
    try:
        first_audit = not auth.list_coach_audits(user_id, limit=1)
        auth.insert_coach_audit({
            "id": uuid.uuid4().hex,
            "user_id": user_id,
            "created_at": auth._ts_iso(time.time()),
            "origin": origin,
            "discipline_score": float(report_dict.get("discipline_score", 0)),
            "total_pnl": float(report_dict.get("total_pnl", 0)),
            "disciplined_pnl": float(report_dict.get("disciplined_pnl", 0)),
            "n_trades": int(report_dict.get("n_trades", 0)),
            "leak_summary": [{"name": l["name"], "dollars": l["dollars"]}
                             for l in report_dict.get("leaks", [])],
            "transactions": [t.model_dump() for t in txns],
        })
        if first_audit:
            # Activation = the user's first quantified audit (the leak card
            # renders from this response). One durable event, counted in admin.
            audit_mod.audit(actor=user_id, action="coach_activation",
                            target_user_id=user_id,
                            metadata={"n_trades": int(report_dict.get("n_trades", 0)),
                                      "n_leaks": len(report_dict.get("leaks", [])),
                                      "discipline_score": report_dict.get("discipline_score")})
    except Exception as exc:  # noqa: BLE001 — persistence must never break the audit
        log.warning("coach audit persist failed: %s", exc)
    try:
        _persist_findings(user_id, report_dict, txns)
    except Exception as exc:  # noqa: BLE001
        log.warning("coach findings persist failed: %s", exc)
    try:
        _persist_rule_events(user_id, txns)
    except Exception as exc:  # noqa: BLE001
        log.warning("coach rule-event persist failed: %s", exc)


def _persist_rule_events(user_id: str, txns: List[Transaction]) -> None:
    """Check the user's ACTIVE fills-checkable rules against the merged history
    and persist genuinely new violations. Violation ids are deterministic
    hashes of (user|rule|trade identity), so re-audits are no-ops."""
    rules = [r for r in auth.list_coach_rules(user_id, status="active")
             if r.get("checkable_from_fills")]
    if not rules:
        return
    trips = coach.reconstruct_round_trips(txns)
    existing = {e.get("id") for e in auth.list_coach_rule_events(user_id, limit=2000)}
    fresh: List = []
    for v in rules_mod.detect_violations(rules, trips, user_id=user_id):
        if v.id in existing:
            continue
        fresh.append(v)
        row = v.to_row(user_id)
        row["created_at"] = auth._ts_iso(time.time())
        auth.insert_coach_rule_event(row)
        audit_mod.audit(actor=user_id, action="coach_rule_violation",
                        target_user_id=user_id,
                        metadata={"rule_kind": v.rule_kind,
                                  "occurred_on": v.occurred_on,
                                  "dollars": v.dollars})
        try:
            from agenticwhales.observability import METRICS
            if METRICS.enabled:
                METRICS.coach_rule_violation.labels(rule_kind=v.rule_kind).inc()
        except Exception:  # noqa: BLE001
            pass
    if fresh:
        try:
            _send_violation_alert(user_id, fresh)
        except Exception as exc:  # noqa: BLE001 — alerts must never break an audit
            log.warning("violation alert failed for %s: %s", user_id, exc)


def _send_violation_alert(user_id: str, violations: List) -> None:
    """One minimized email per audit when NEW violations land (Plus, opt-in,
    Resend-gated). Counts + rule labels only — dollars stay in-app, same
    posture as the weekly digest. The in-app feed is the source of truth."""
    from web import email_service, entitlements
    prefs = auth.get_coach_prefs(user_id)
    if not (prefs.get("email_alerts") and prefs.get("digest_email")):
        return
    if not entitlements.check(user_id, "violation_alerts"):
        return
    if not email_service.is_configured():
        return
    kinds = sorted({str(v.rule_kind).replace("_", " ") for v in violations})
    n = len(violations)
    base_url = os.getenv("AGENTICWHALES_PUBLIC_BASE_URL", "").rstrip("/")
    unsub = (f"{base_url}/api/coach/digest/unsubscribe"
             f"?token={prefs.get('unsubscribe_token')}"
             if prefs.get("unsubscribe_token") else None)
    html = (
        f"<p>Your latest audit recorded <strong>{n} new rule "
        f"violation{'s' if n != 1 else ''}</strong> against the rules you adopted:</p>"
        f"<p>{', '.join(kinds)}</p>"
        "<p>The details (dates, trades, amounts) are on your coach page.</p>"
        f'<p><a href="{base_url}/coach#rules">Open your rules</a></p>')
    email_service.send_email(
        prefs["digest_email"],
        f"{n} new rule violation{'s' if n != 1 else ''} recorded",
        html, unsubscribe_url=unsub)
    track_event("violation_alert_sent", user_id, {"n": n})


def _persist_findings(user_id: str, report_dict: Dict, txns: List[Transaction]) -> None:
    """The forward-validation seam (critique D1): keep one OPEN finding per leak
    kind; once enough later trades exist, resolve it — did the behavior persist?
    A persisted-leak resolution immediately opens a fresh finding, so the
    longitudinal chain (flagged → re-tested → flagged again / fixed) accumulates."""
    window_end = ((report_dict.get("insights") or {}).get("period") or {}).get("end")
    if not window_end:
        return
    trips = coach.reconstruct_round_trips(txns)
    for f in auth.list_coach_findings(user_id, unresolved_only=True):
        res = coach.resolve_finding_forward(f, trips)
        if res:
            auth.update_coach_finding(
                f["id"], {"resolved_at": auth._ts_iso(time.time()), **res})
    open_keys = {f.get("leak_key")
                 for f in auth.list_coach_findings(user_id, unresolved_only=True)}
    for l in report_dict.get("leaks", []):
        key = coach.leak_key(l["name"])
        if key in open_keys:
            continue
        auth.insert_coach_finding({
            "id": uuid.uuid4().hex,
            "user_id": user_id,
            "created_at": auth._ts_iso(time.time()),
            "leak_key": key,
            "name": l["name"],
            "severity": l["severity"],
            "dollars": float(l["dollars"]),
            "fix": l["fix"],
            "window_end": str(window_end)[:10],
            "resolved_at": None,
            "persisted": None,
            "forward_dollars": None,
            "n_forward_trades": None,
        })
        open_keys.add(key)


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

    def _work():
        # Merge + audit + persistence do storage round-trips — off the loop.
        audit_txns = txns
        if user_id and user_id != auth.ANONYMOUS_USER_ID:
            audit_txns = _merge_user_trades(user_id, txns)
        report = coach.audit_trades(audit_txns, fees_paid=p.fees_paid)
        out = report.to_dict()
        out["n_transactions"] = len(audit_txns)
        out["new_transactions"] = len(txns)
        out["benchmark"] = ({"cohort": "demo", "label": "sample trader — no benchmark"}
                            if p.use_demo else _benchmark_for(report.discipline_score))
        _persist_audit(user_id, out, audit_txns)
        return out

    return await asyncio.get_running_loop().run_in_executor(_UPLOAD_POOL, _work)


def _extract_images_vision(images: List[bytes], mimes: List[str], user_id: str,
                           on_warn, on_progress=None) -> List[Transaction]:
    """Screenshot(s) -> transactions via the vision model. Budget-gated and
    spend-recorded. Caller handles fallback to OCR on failure."""
    cfg = extract_mod.vision_config()
    if cfg is None:
        raise RuntimeError("vision extraction not enabled")
    _check_llm_budget(user_id)
    provider, model = cfg
    return extract_mod.extract_transactions_from_image(
        images, mimes, provider=provider, model=model, on_warn=on_warn,
        on_progress=on_progress, on_usage=_spend_recorder(user_id, provider, model))


def _process_upload(data: bytes, name: str, ctype: str, user_id: str = "") -> tuple:
    """Blocking parse/OCR/extract pipeline — runs in the bounded upload pool,
    NEVER inline on the event loop (J2: a long OCR round-trip or LLM extraction
    would starve the leader-only streaming worker and the SSE heartbeats)."""
    name = (name or "").lower()
    ctype = (ctype or "").lower()
    is_image = name.endswith(_IMAGE_EXTS) or ctype.startswith("image/")
    is_pdf = name.endswith(".pdf") or "pdf" in ctype
    warnings: List[str] = []
    txns: List[Transaction] = []
    text = ""

    if is_image:
        # Vision-first (no OCR server needed); external OCR is the fallback.
        if extract_mod.vision_config() is not None:
            try:
                txns = _extract_images_vision([data], [ctype or "image/png"],
                                              user_id, warnings.append)
            except GuestLimitExceeded:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("vision extraction failed, trying OCR: %s", exc)
                warnings.append("Vision read failed — used OCR.")
        if not txns:
            # Server is PDF-only; wrap the image in a one-page PDF, then OCR.
            text = _ocr_pdf_to_markdown(_image_to_pdf(data))
    elif is_pdf:
        text = _pdf_to_text(data)
        if len(text.strip()) < 40:  # scanned / image-only PDF -> OCR fallback
            warnings.append("PDF had little extractable text — used OCR.")
            text = _ocr_pdf_to_markdown(data)
    else:
        txns = parse_transactions_csv(data.decode("utf-8", errors="replace"))

    if (is_image or is_pdf) and not txns:
        if not text.strip():
            raise ValueError("No readable text found in the document.")
        try:
            txns = coach_extract_pdf(text, warnings.append, user_id=user_id)
        except GuestLimitExceeded:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("extraction failed: %s", exc)
            raise ValueError(f"Extraction failed: {exc}") from exc

    if not txns:
        raise ValueError("No transactions found in the file.")
    if is_image:
        warnings.append(f"Read {len(txns)} transactions from your image — "
                        "spot-check the figures below before trusting them.")
    return txns, warnings


@router.post("/api/coach/upload")
async def coach_upload(request: Request, file: UploadFile = File(...),
                       user_id: str = Depends(optional_user_id)):
    """Accept a brokerage history as CSV, PDF, or screenshot. CSV is parsed
    deterministically; documents go through vision/OCR + the LLM extractor."""
    data = await file.read()
    if ((not user_id or user_id == auth.ANONYMOUS_USER_ID)
            and _needs_guest_cap([(data, file.filename, file.content_type)])):
        try:
            _check_guest_upload_cap(request)
        except GuestLimitExceeded as exc:
            return _guest_limit_response(user_id, exc)
    track_event("upload_started", user_id, {"kind": "sync"})
    loop = asyncio.get_running_loop()
    try:
        txns, warnings = await loop.run_in_executor(
            _UPLOAD_POOL, _process_upload, data, file.filename,
            file.content_type, user_id)
    except GuestLimitExceeded as exc:
        return _guest_limit_response(user_id, exc)
    except OcrUnavailable as exc:
        log.warning("OCR unavailable: %s", exc)
        return JSONResponse(
            {"error": "We couldn't read this document. For scanned files, "
                      "either enable the vision reader or the OCR service "
                      "(AGENTICWHALES_OCR_URL) — or upload a text-based CSV/PDF."},
            status_code=503)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"Could not read the file: {exc}"}, status_code=400)

    def _finish():
        report = coach.audit_trades(txns, price_fetcher=prices.fetch_ohlc)
        out = report.to_dict()
        out["warnings"] = warnings
        out["n_transactions"] = len(txns)
        out["benchmark"] = _benchmark_for(report.discipline_score)
        _persist_audit(user_id, out, txns)
        return out

    return await loop.run_in_executor(_UPLOAD_POOL, _finish)


@router.get("/api/coach/history")
async def coach_history(user_id: str = Depends(optional_user_id)):
    """A signed-in user's past audit summaries, newest first — the score trend."""
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return {"signed_in": False, "audits": []}
    return {"signed_in": True, "audits": auth.list_coach_audits(user_id)}


# --------------------------------------------------------------------------- #
# Accountability partner — double-opt-in, compliance-only.
#
# Privacy contract (tested by a forbidden-keys test): the partner sees rule
# kinds + adoption status, weekly violation COUNTS, and the streak. NEVER
# P&L, dollars, symbols, trade details, or scores. Tokens are 32-byte URL-safe
# secrets with a 90-day TTL; revocation hard-kills the token; partner emails
# carry their own one-click stop link.
# --------------------------------------------------------------------------- #
_PARTNER_TOKEN_TTL_DAYS = 90
_PARTNER_VIEW_HITS: Dict[tuple, int] = {}
_PARTNER_INVITES: Dict[tuple, int] = {}


def _partner_rate(counter: Dict[tuple, int], key: str, cap: int) -> bool:
    """True if under the daily cap (and counts the hit)."""
    import datetime as _dt
    today = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
    with _GUEST_LOCK:
        for k in [k for k in counter if k[1] != today]:
            counter.pop(k, None)
        k = (key, today)
        if counter.get(k, 0) >= cap:
            return False
        counter[k] = counter.get(k, 0) + 1
        return True


def _partner_token_valid(row: Dict) -> bool:
    import datetime as _dt
    if row.get("status") != "active":
        return False
    anchor = auth._parse_iso_ts(row.get("confirmed_at") or row.get("invited_at"))
    return bool(anchor and (_dt.datetime.now(_dt.timezone.utc) - anchor).days
                <= _PARTNER_TOKEN_TTL_DAYS)


def _partner_view_payload(user_id: str) -> Dict:
    """Compliance-only serializer — counts and statuses, nothing financial."""
    import datetime as _dt
    rules = auth.list_coach_rules(user_id)
    events = auth.list_coach_rule_events(user_id, limit=500)
    week_ago = (_dt.datetime.now(_dt.timezone.utc).date()
                - _dt.timedelta(days=7)).isoformat()
    week_counts: Dict[str, int] = {}
    for e in events:
        if (e.get("occurred_on") or "") >= week_ago:
            k = e.get("rule_kind") or "other"
            week_counts[k] = week_counts.get(k, 0) + 1
    return {
        "rules": [{"label": r.get("label"), "rule_kind": r.get("rule_kind"),
                   "status": r.get("status")} for r in rules],
        "week_violations": sum(week_counts.values()),
        "week_violations_by_kind": week_counts,
        "streak_weeks": rules_mod.compute_streak(rules, events).get("weeks", 0),
    }


class PartnerInvite(BaseModel):
    email: str = ""


@router.get("/api/coach/partner")
async def coach_partner_get(user_id: str = Depends(optional_user_id)):
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return {"signed_in": False, "partners": []}
    return {"signed_in": True, "partners": [
        {"id": p.get("id"), "partner_email": p.get("partner_email"),
         "status": p.get("status"), "invited_at": p.get("invited_at")}
        for p in auth.list_coach_partners(user_id)
        if p.get("status") != "revoked"]}


@router.post("/api/coach/partner")
async def coach_partner_invite(p: PartnerInvite,
                               user_id: str = Depends(optional_user_id)):
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return JSONResponse({"error": "Sign in to invite a partner."}, status_code=401)
    if not _valid_email(p.email):
        return JSONResponse({"error": "invalid email"}, status_code=400)
    if any(x.get("status") in ("invited", "active")
           for x in auth.list_coach_partners(user_id)):
        return JSONResponse({"error": "One partner at a time — revoke the "
                                      "current one first."}, status_code=409)
    if not _partner_rate(_PARTNER_INVITES, user_id, 3):
        return JSONResponse({"error": "Invite limit reached for today."},
                            status_code=429)
    import secrets
    token = secrets.token_urlsafe(32)
    row = {"id": uuid.uuid4().hex, "user_id": user_id,
           "partner_email": p.email.strip(), "status": "invited",
           "view_token": token, "invited_at": auth._ts_iso(time.time()),
           "confirmed_at": None, "revoked_at": None}
    auth.insert_coach_partner(row)
    base = os.getenv("AGENTICWHALES_PUBLIC_BASE_URL", "").rstrip("/")
    confirm_url = f"{base}/api/coach/partner/confirm?token={token}"
    from web import email_service
    sent = False
    if email_service.is_configured():
        sent = email_service.send_email(
            p.email.strip(), "Accountability partner invite — AgenticWhales Coach",
            "<p>Someone asked you to be their trading-discipline accountability "
            "partner. If you accept, you'll see their rule compliance — counts "
            "and streaks only, never trades or money.</p>"
            f"<p><a href='{confirm_url}'>Review and accept</a></p>",
            unsubscribe_url=f"{base}/api/coach/partner/unsubscribe?token={token}")
    return {"ok": True, "emailed": sent,
            # When email is dark, the owner shares the link themselves.
            "confirm_url": None if sent else confirm_url}


@router.delete("/api/coach/partner/{partner_id}")
async def coach_partner_revoke(partner_id: str,
                               user_id: str = Depends(optional_user_id)):
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return JSONResponse({"error": "Sign in."}, status_code=401)
    row = auth.get_coach_partner(partner_id)
    if not row or row.get("user_id") != user_id:
        return JSONResponse({"error": "Unknown partner."}, status_code=404)
    auth.update_coach_partner(partner_id, {
        "status": "revoked", "view_token": "",   # hard-invalidate the capability
        "revoked_at": auth._ts_iso(time.time())})
    return {"ok": True}


@router.get("/api/coach/partner/confirm")
async def coach_partner_confirm_page(token: str = ""):
    """Double-opt-in: GET renders the accept form; only POST activates."""
    from fastapi.responses import HTMLResponse
    row = auth.find_coach_partner_by_token(token)
    if not row or row.get("status") == "revoked":
        return HTMLResponse(_UNSUB_PAGE.format(
            title="Invite expired", body="This invite link is no longer valid.",
            form=""), status_code=404)
    return HTMLResponse(_UNSUB_PAGE.format(
        title="Become an accountability partner?",
        body="You'll see rule compliance only — counts and streaks, never "
             "trades, dollars, or positions. You can stop anytime.",
        form=f'<form method="post" action="/api/coach/partner/confirm?token={token}">'
             '<button type="submit" style="background:#1A1A17;color:#FAF6EE;'
             'border:none;border-radius:999px;padding:12px 24px;cursor:pointer">'
             'Accept</button></form>'))


@router.post("/api/coach/partner/confirm")
async def coach_partner_confirm(token: str = ""):
    from fastapi.responses import HTMLResponse
    row = auth.find_coach_partner_by_token(token)
    if not row or row.get("status") == "revoked":
        return HTMLResponse(_UNSUB_PAGE.format(
            title="Invite expired", body="This invite link is no longer valid.",
            form=""), status_code=404)
    auth.update_coach_partner(row["id"], {
        "status": "active", "confirmed_at": auth._ts_iso(time.time())})
    return HTMLResponse(_UNSUB_PAGE.format(
        title="You're in", body=f'Bookmark the compliance view: '
        f'<a href="/partner?token={token}">your partner page</a>.', form=""))


@router.post("/api/coach/partner/unsubscribe")
async def coach_partner_unsubscribe(token: str = ""):
    """A partner can stop ALL contact unilaterally (kills the link entirely)."""
    from fastapi.responses import HTMLResponse
    row = auth.find_coach_partner_by_token(token)
    if row:
        auth.update_coach_partner(row["id"], {
            "status": "revoked", "view_token": "",
            "revoked_at": auth._ts_iso(time.time())})
    return HTMLResponse(_UNSUB_PAGE.format(
        title="Stopped", body="You won't receive anything further.", form=""))


@router.get("/api/coach/partner/view")
async def coach_partner_view(token: str = ""):
    row = auth.find_coach_partner_by_token(token)
    if not row or not _partner_token_valid(row):
        return JSONResponse({"error": "expired"}, status_code=404)
    if not _partner_rate(_PARTNER_VIEW_HITS, token, 100):
        return JSONResponse({"error": "rate limited"}, status_code=429)
    return _partner_view_payload(row["user_id"])


# --------------------------------------------------------------------------- #
# Public "State of Retail Discipline" stats — aggregate, k-anonymous, cached.
#
# Tiered suppression (server-side, never in JS):
#   - below MIN_USERS distinct traders overall -> "early" mode, n only;
#   - a per-leak bucket needs >= MIN_USERS traders for percentages;
#   - dollar aggregates need >= MIN_USERS_DOLLARS traders in the bucket
#     (a single whale can dominate—and identify—a small bucket), and are
#     rounded to 2 significant figures.
# --------------------------------------------------------------------------- #
_STATS_CACHE: Dict = {"data": None, "at": 0.0}
_STATS_LOCK = threading.Lock()


def _round_2sig(x: float) -> float:
    if x == 0:
        return 0.0
    from math import floor, log10
    digits = -int(floor(log10(abs(x)))) + 1
    return round(x, digits)


def _public_stats_compute() -> Dict:
    k = int(os.getenv("AGENTICWHALES_PUBLIC_STATS_MIN_USERS", "10"))
    k_dollars = int(os.getenv("AGENTICWHALES_PUBLIC_STATS_MIN_USERS_DOLLARS", "25"))
    if auth._db_writable():
        rows = auth._select_columns(
            "coach_audits", filters={},
            select="user_id,discipline_score,leak_summary,created_at", limit=10000)
    else:
        rows = [r for (t, _), r in auth._memstore.items() if t == "coach_audits"]
    latest: Dict[str, Dict] = {}
    for r in rows:
        uid = r.get("user_id")
        if not uid or uid == auth.ANONYMOUS_USER_ID:
            continue
        ts = r.get("created_at") or ""
        if uid not in latest or ts > (latest[uid].get("created_at") or ""):
            latest[uid] = r
    n = len(latest)
    if n < k:
        return {"mode": "early", "n_traders": n, "unlock_at": k}

    import statistics as _st
    scores = [float(r.get("discipline_score") or 0) for r in latest.values()]
    leak_users: Dict[str, int] = {}
    leak_dollars: Dict[str, float] = {}
    for r in latest.values():
        seen = set()
        for l in (r.get("leak_summary") or []):
            key = coach.leak_key(l.get("name", ""))
            if not key or key in seen:
                continue
            seen.add(key)
            leak_users[key] = leak_users.get(key, 0) + 1
            leak_dollars[key] = leak_dollars.get(key, 0.0) + float(l.get("dollars") or 0)
    leaks = []
    for key, users in sorted(leak_users.items(), key=lambda kv: -kv[1]):
        if users < k:
            continue  # bucket suppressed entirely
        entry = {"leak": key, "pct_of_traders": round(100.0 * users / n)}
        if users >= k_dollars:
            entry["historical_dollars"] = _round_2sig(leak_dollars[key])
        leaks.append(entry)
    cs = auth.admin_coach_stats()
    return {
        "mode": "live",
        "n_traders": n,
        "median_score": round(_st.median(scores), 0),
        "leaks": leaks,
        "findings": {"resolved": cs.get("resolved_findings", 0),
                     "not_detected_forward": cs.get("leaks_fixed", 0)},
        "note": "Aggregate, anonymized, past-tense historical attribution from "
                "consenting users' own trades. Buckets below the privacy "
                "threshold are suppressed.",
    }


@router.get("/api/coach/public-stats")
async def coach_public_stats():
    ttl = float(os.getenv("AGENTICWHALES_PUBLIC_STATS_TTL", "3600"))
    now = time.time()
    with _STATS_LOCK:
        if _STATS_CACHE["data"] is not None and now - _STATS_CACHE["at"] < ttl:
            return _STATS_CACHE["data"]
    data = await asyncio.get_running_loop().run_in_executor(
        _UPLOAD_POOL, _public_stats_compute)
    with _STATS_LOCK:
        _STATS_CACHE["data"] = data
        _STATS_CACHE["at"] = now
    return data


# --------------------------------------------------------------------------- #
# Digest prefs + in-app digests + unsubscribe (confirm-POST + RFC 8058)
# --------------------------------------------------------------------------- #

_EMAIL_RE = None  # compiled lazily


def _valid_email(s: str) -> bool:
    global _EMAIL_RE
    if _EMAIL_RE is None:
        import re
        _EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    return bool(s) and len(s) <= 254 and bool(_EMAIL_RE.match(s))


class PrefsUpdate(BaseModel):
    email_digest: Optional[bool] = None
    email_alerts: Optional[bool] = None
    email: Optional[str] = None


@router.get("/api/coach/prefs")
async def coach_prefs_get(user_id: str = Depends(optional_user_id)):
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return {"signed_in": False}
    p = auth.get_coach_prefs(user_id)
    return {"signed_in": True, "email_digest": bool(p.get("email_digest")),
            "email_alerts": bool(p.get("email_alerts")),
            "digest_email": p.get("digest_email") or ""}


@router.post("/api/coach/prefs")
async def coach_prefs_set(p: PrefsUpdate, user_id: str = Depends(optional_user_id)):
    """Digest emails are strict OPT-IN: enabling requires an explicit email
    address; a one-click unsubscribe token is minted on first opt-in."""
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return JSONResponse({"error": "Sign in to manage preferences."}, status_code=401)
    fields: Dict = {}
    if p.email is not None:
        if not _valid_email(p.email):
            return JSONResponse({"error": "invalid email"}, status_code=400)
        fields["digest_email"] = p.email.strip()
    if p.email_digest is not None:
        current = auth.get_coach_prefs(user_id)
        email_on_file = fields.get("digest_email") or current.get("digest_email")
        if p.email_digest and not email_on_file:
            return JSONResponse({"error": "provide an email to enable the digest"},
                                status_code=400)
        fields["email_digest"] = bool(p.email_digest)
        if p.email_digest and not current.get("unsubscribe_token"):
            import secrets
            fields["unsubscribe_token"] = secrets.token_urlsafe(32)
    if p.email_alerts is not None:
        current = auth.get_coach_prefs(user_id)
        email_on_file = fields.get("digest_email") or current.get("digest_email")
        if p.email_alerts and not email_on_file:
            return JSONResponse({"error": "provide an email to enable alerts"},
                                status_code=400)
        fields["email_alerts"] = bool(p.email_alerts)
        if p.email_alerts and not current.get("unsubscribe_token"):
            import secrets
            fields["unsubscribe_token"] = secrets.token_urlsafe(32)
    row = auth.upsert_coach_prefs(user_id, fields)
    return {"ok": True, "email_digest": bool(row.get("email_digest")),
            "email_alerts": bool(row.get("email_alerts")),
            "digest_email": row.get("digest_email") or ""}


class EvalPayload(BaseModel):
    preset: str = "custom"          # ftmo_style | topstep_style | custom
    account_size: float = 0
    start_date: str = ""
    end_date: Optional[str] = None
    profit_target: Optional[float] = None
    daily_loss_limit: Optional[float] = None
    max_drawdown: Optional[float] = None


@router.post("/api/coach/eval")
async def coach_eval_save(p: EvalPayload, user_id: str = Depends(get_current_user_id)):
    """Create/update the user's prop-firm evaluation tracker (Pro).
    Scorekeeping over their own realized P&L — never trading instructions."""
    from agenticwhales import prop_eval
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return JSONResponse({"error": "Sign in to track an evaluation."}, status_code=401)
    if p.account_size <= 0:
        return JSONResponse({"error": "account_size must be positive"}, status_code=400)
    if not p.start_date:
        return JSONResponse({"error": "start_date required (yyyy-mm-dd)"}, status_code=400)
    if p.preset not in prop_eval.PRESETS:
        return JSONResponse({"error": f"unknown preset {p.preset!r}"}, status_code=400)
    cfg = prop_eval.resolve_config(p.model_dump())
    auth.upsert_coach_eval(user_id, cfg)
    track_event("eval_configured", user_id, {"preset": p.preset})
    return await coach_eval_status(user_id)


@router.get("/api/coach/eval")
async def coach_eval_status(user_id: str = Depends(optional_user_id)):
    """The active evaluation scored against the user's actual closed trades."""
    from agenticwhales import prop_eval
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return {"signed_in": False, "configured": False}
    cfg = auth.get_coach_eval(user_id)
    if not cfg:
        return {"signed_in": True, "configured": False,
                "presets": prop_eval.PRESETS}
    def _score():
        trips = coach.reconstruct_round_trips(_latest_user_trades(user_id))
        return prop_eval.evaluate(cfg, trips)
    result = await asyncio.get_running_loop().run_in_executor(_UPLOAD_POOL, _score)
    return {"signed_in": True, "configured": True, **result}


@router.delete("/api/coach/eval")
async def coach_eval_delete(user_id: str = Depends(get_current_user_id)):
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return JSONResponse({"error": "Sign in first."}, status_code=401)
    auth.delete_coach_eval(user_id)
    return {"ok": True}


class StandingBriefPayload(BaseModel):
    tickers: List[str] = []
    active: bool = True


@router.get("/api/coach/standing-brief")
async def standing_brief_get(user_id: str = Depends(optional_user_id)):
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return {"signed_in": False, "configured": False}
    row = auth.get_standing_brief(user_id)
    if not row:
        return {"signed_in": True, "configured": False}
    return {"signed_in": True, "configured": True,
            "tickers": row.get("tickers") or [], "active": bool(row.get("active")),
            "cadence": row.get("cadence") or "weekly",
            "last_run_at": row.get("last_run_at")}


@router.post("/api/coach/standing-brief")
async def standing_brief_save(p: StandingBriefPayload,
                              user_id: str = Depends(get_current_user_id)):
    """Configure the weekly standing brief (Pro): the analysts brief you every
    Monday on your tickers, in brief mode — research synthesis, no verdict."""
    import re as _re
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return JSONResponse({"error": "Sign in first."}, status_code=401)
    tickers = [t.strip().upper() for t in p.tickers if t.strip()]
    if p.active and not tickers:
        return JSONResponse({"error": "Add at least one ticker."}, status_code=400)
    if len(tickers) > 5:
        return JSONResponse({"error": "Standing briefs cover up to 5 tickers."},
                            status_code=400)
    for t in tickers:
        if not _re.fullmatch(r"[A-Z.\-]{1,10}", t):
            return JSONResponse({"error": f"{t!r} doesn't look like a ticker."},
                                status_code=400)
    row = auth.upsert_standing_brief(user_id, {"tickers": tickers,
                                               "active": p.active,
                                               "cadence": "weekly"})
    track_event("standing_brief_configured", user_id, {"n": len(tickers)})
    return {"ok": True, "tickers": row["tickers"], "active": row["active"]}


@router.get("/api/account/plan")
async def account_plan(user_id: str = Depends(optional_user_id)):
    """The signed-in user's plan + entitlements (guests see the Free plan).
    Drives the nav plan chip, 'included in Plus/Pro' labels, and nudges."""
    from web import entitlements
    return entitlements.plan_summary(user_id or "")


@router.get("/api/coach/digests")
async def coach_digests(user_id: str = Depends(optional_user_id)):
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return {"signed_in": False, "digests": []}
    return {"signed_in": True, "digests": auth.list_coach_digests(user_id)}


_UNSUB_PAGE = """<!DOCTYPE html><html><body style="font-family:sans-serif;
background:#FAF6EE;color:#1A1A17;display:flex;justify-content:center;padding:60px">
<div style="max-width:420px"><h2>{title}</h2><p style="color:#6B6657">{body}</p>{form}</div>
</body></html>"""


@router.get("/api/coach/digest/unsubscribe")
async def digest_unsubscribe_confirm(token: str = ""):
    """GET never mutates — mail scanners prefetch links. Renders a confirm
    page whose button POSTs the actual change (RFC 8058 one-click also POSTs)."""
    from fastapi.responses import HTMLResponse
    if not auth.find_coach_prefs_by_token(token):
        return HTMLResponse(_UNSUB_PAGE.format(
            title="Link expired", body="This unsubscribe link is no longer valid.",
            form=""), status_code=404)
    return HTMLResponse(_UNSUB_PAGE.format(
        title="Unsubscribe from the weekly digest?",
        body="You'll keep your in-app digest; only the email stops.",
        form=f'<form method="post" action="/api/coach/digest/unsubscribe?token={token}">'
             '<button type="submit" style="background:#1A1A17;color:#FAF6EE;'
             'border:none;border-radius:999px;padding:12px 24px;cursor:pointer">'
             'Unsubscribe</button></form>'))


@router.post("/api/coach/digest/unsubscribe")
async def digest_unsubscribe(token: str = ""):
    from fastapi.responses import HTMLResponse
    row = auth.find_coach_prefs_by_token(token)
    if not row:
        return HTMLResponse(_UNSUB_PAGE.format(
            title="Link expired", body="This unsubscribe link is no longer valid.",
            form=""), status_code=404)
    auth.upsert_coach_prefs(row["user_id"], {"email_digest": False})
    return HTMLResponse(_UNSUB_PAGE.format(
        title="Unsubscribed", body="No more digest emails. Your in-app digest "
        "stays available on the coach page.", form=""))


@router.get("/api/coach/rules")
async def coach_rules_list(user_id: str = Depends(optional_user_id)):
    """The user's rule book. Auto-seeds suggestions from open findings on each
    read (deterministic ids — re-reads can't duplicate), then lists all."""
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return {"signed_in": False, "rules": []}
    existing = auth.list_coach_rules(user_id)
    suggestions = rules_mod.suggest_rules(
        auth.list_coach_findings(user_id, unresolved_only=True), existing)
    for s in suggestions:
        s["user_id"] = user_id
        s["created_at"] = auth._ts_iso(time.time())
        auth.insert_coach_rule(s)
    return {"signed_in": True, "rules": auth.list_coach_rules(user_id)}


@router.get("/api/coach/rules/summary")
async def coach_rules_summary(user_id: str = Depends(optional_user_id)):
    """Everything the "Your rules" section renders: rules with 30-day violation
    counts, the streak, recent violations, and the forward-validation pairs
    (per finding, both window sizes — no clipped aggregates)."""
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return {"signed_in": False}
    import datetime as _dt
    rules = auth.list_coach_rules(user_id)
    events = auth.list_coach_rule_events(user_id, limit=500)
    cutoff = (_dt.datetime.now(_dt.timezone.utc).date()
              - _dt.timedelta(days=30)).isoformat()
    by_rule: Dict[str, Dict] = {}
    for e in events:
        d = by_rule.setdefault(e.get("rule_id"),
                               {"violations_30d": 0, "last_violation": None})
        occurred = e.get("occurred_on") or ""
        if occurred >= cutoff:
            d["violations_30d"] += 1
        if not d["last_violation"] or occurred > d["last_violation"]:
            d["last_violation"] = occurred
    rules_out = [{**r, **by_rule.get(r.get("id"),
                                     {"violations_30d": 0, "last_violation": None})}
                 for r in rules]
    # Forward validation: per-finding pairs with BOTH window sizes shown —
    # past-tense attribution; "not detected forward", never "fixed".
    forward = [{
        "name": f.get("name"), "leak_key": f.get("leak_key"),
        "dollars": f.get("dollars"), "window_end": f.get("window_end"),
        "n_forward_trades": f.get("n_forward_trades"),
        "forward_dollars": f.get("forward_dollars"),
        "persisted": f.get("persisted"), "resolved_at": f.get("resolved_at"),
    } for f in auth.list_coach_findings(user_id) if f.get("resolved_at")]
    return {
        "signed_in": True,
        "rules": rules_out,
        "streak": rules_mod.compute_streak(rules, events),
        "recent_violations": events[:10],
        "forward_validation": forward,
    }


class RuleUpdate(BaseModel):
    status: Optional[str] = None     # suggested | active | paused
    params: Optional[Dict] = None    # clamped to template bounds server-side


@router.post("/api/coach/rules/{rule_id}")
async def coach_rule_update(rule_id: str, p: RuleUpdate,
                            user_id: str = Depends(optional_user_id)):
    """Adopt / pause / tune one rule. Ownership-checked: a rule id from another
    account 404s (no existence oracle)."""
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return JSONResponse({"error": "Sign in to manage rules."}, status_code=401)
    row = auth.get_coach_rule(rule_id)
    if not row or row.get("user_id") != user_id:
        return JSONResponse({"error": "Unknown rule."}, status_code=404)
    fields: Dict = {"updated_at": auth._ts_iso(time.time())}
    if p.status is not None:
        if p.status not in rules_mod.RULE_STATUSES:
            return JSONResponse({"error": "invalid status"}, status_code=400)
        fields["status"] = p.status
        if p.status == "active" and not row.get("adopted_at"):
            fields["adopted_at"] = auth._ts_iso(time.time())
    if p.params is not None:
        fields["params"] = rules_mod.clamp_params(row.get("rule_kind", ""), p.params)
    auth.update_coach_rule(rule_id, fields)
    return {"ok": True, "rule": auth.get_coach_rule(rule_id)}


def _rulebook_kwargs(user_id: str, payload_max_risk: Optional[float]) -> Dict:
    """Resolve pre-trade personalization from the user's ACTIVE rules.
    Resolution order per knob: explicit payload value > adopted rule > default."""
    kwargs: Dict = {}
    applied: List[Dict] = []
    if user_id and user_id != auth.ANONYMOUS_USER_ID:
        for r in auth.list_coach_rules(user_id, status="active"):
            params = r.get("params") or {}
            kind = r.get("rule_kind")
            if kind == "max_risk_pct" and payload_max_risk is None:
                kwargs["max_risk_pct"] = float(params.get("max_risk_pct", 0.02))
            elif kind == "min_payoff":
                kwargs["min_payoff"] = float(params.get("min_payoff", 1.5))
            elif kind == "stop_required":
                kwargs["stop_required"] = True
            elif kind == "size_cap_x_median":
                kwargs["size_cap_x_median"] = float(params.get("cap_x", 2.0))
            elif kind == "cooldown_after_loss":
                kwargs["cooldown_hours"] = float(params.get("cooldown_hours", 24))
            else:
                continue
            applied.append({"rule_kind": kind, "params": params})
    if payload_max_risk is not None:
        kwargs["max_risk_pct"] = payload_max_risk
    kwargs["_applied"] = applied
    return kwargs


@router.get("/api/coach/findings")
async def coach_findings(user_id: str = Depends(optional_user_id)):
    """The signed-in user's persisted findings + their forward-validation state:
    each flagged leak, the rule prescribed, and — once later trades exist —
    whether the behavior persisted. Keeps the product's central claim testable."""
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return {"signed_in": False, "findings": []}
    return {"signed_in": True, "findings": auth.list_coach_findings(user_id)}


@router.post("/api/coach/data/delete")
async def coach_data_delete(user_id: str = Depends(optional_user_id)):
    """Delete all of the signed-in user's uploaded data (trades + audit history)."""
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return JSONResponse({"error": "Sign in required."}, status_code=401)
    res = auth.delete_coach_data(user_id)
    return {"ok": True, **res}


# The /latest recompute is price-aware (yfinance per symbol), which is seconds
# of work cold. The report only changes when the trades change, so cache it
# per user keyed by a fingerprint of the trade rows — reloads are instant,
# and any upload/sync/delete changes the fingerprint and invalidates naturally.
_LATEST_REPORT_CACHE: Dict[str, tuple] = {}  # user_id -> (fingerprint, report)
_LATEST_REPORT_CACHE_MAX = 256


def _trades_fingerprint(txns: List[Transaction]) -> str:
    h = hashlib.sha256()
    for t in txns:
        h.update(f"{t.date}|{t.type}|{t.symbol}|{t.quantity}|{t.price};".encode())
    return h.hexdigest()


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
    track_event("audit_viewed", user_id)

    fp = _trades_fingerprint(txns)
    cached = _LATEST_REPORT_CACHE.get(user_id)
    if cached and cached[0] == fp:
        return {"signed_in": True, "has_audit": True, "report": cached[1]}

    def _recompute():
        # Same price-aware path as upload/sync audits — a price-blind recompute
        # here would show a different discipline score on reload than the one
        # the user just saw. fetch_ohlc is cached in-process; failures degrade
        # to the price-free leak set inside audit_trades.
        report = coach.audit_trades(txns, price_fetcher=prices.fetch_ohlc)
        out = report.to_dict()
        out["created_at"] = row.get("created_at")
        out["n_transactions"] = len(txns)
        out["benchmark"] = _benchmark_for(report.discipline_score)
        return out

    out = await asyncio.get_running_loop().run_in_executor(_UPLOAD_POOL, _recompute)
    if len(_LATEST_REPORT_CACHE) >= _LATEST_REPORT_CACHE_MAX:
        _LATEST_REPORT_CACHE.pop(next(iter(_LATEST_REPORT_CACHE)))
    _LATEST_REPORT_CACHE[user_id] = (fp, out)
    return {"signed_in": True, "has_audit": True, "report": out}


_EXTRACT_CONCURRENCY = 8  # chunks are independent; extract them in parallel


def coach_extract_pdf(text: str, on_warn, on_progress=None, *,
                      user_id: str = "") -> List[Transaction]:
    from agenticwhales.transactions.extract import extract_transactions
    _check_llm_budget(user_id)
    return extract_transactions(text, provider=_EXTRACT_PROVIDER, model=_EXTRACT_MODEL,
                                on_warn=on_warn, on_progress=on_progress,
                                on_usage=_spend_recorder(user_id, _EXTRACT_PROVIDER,
                                                         _EXTRACT_MODEL),
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


def _run_upload_job(jid: str, items: List[tuple], user_id: str) -> None:
    """Process one or more uploaded files into a single merged audit.

    `items` is a list of (bytes, name, ctype). Images are grouped into ONE
    vision extraction (so a 3-screenshot paste is one progress bar and one
    cross-image dedupe); CSV/PDF items go through the text pipeline; results
    are concatenated and deduped before the audit.
    """
    images: List[tuple] = []
    others: List[tuple] = []
    for data, name, ctype in items:
        name = (name or "").lower()
        ctype = (ctype or "").lower()
        if name.endswith(_IMAGE_EXTS) or ctype.startswith("image/"):
            images.append((data, name, ctype))
        else:
            others.append((data, name, ctype))

    warnings: List[str] = []
    txns: List[Transaction] = []
    try:
        _job_set(jid, status="running", stage="reading", pct=5,
                 message="Reading your file…" if len(items) == 1
                 else f"Reading {len(items)} files…")

        # --- images: vision-first, OCR-server fallback per image ---
        if images:
            vision_txns: List[Transaction] = []
            if extract_mod.vision_config() is not None:
                _job_set(jid, stage="extract", pct=10,
                         message="Reading your screenshot…" if len(images) == 1
                         else f"Reading {len(images)} screenshots…")
                try:
                    vision_txns = _extract_images_vision(
                        [d for d, _, _ in images],
                        [c or "image/png" for _, _, c in images],
                        user_id, warnings.append,
                        on_progress=lambda i, n: _job_set(
                            jid, stage="extract", pct=10 + int(40 * i / max(n, 1)),
                            message=f"Reading image {i} of {n}…"))
                except GuestLimitExceeded:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.warning("vision extraction failed, trying OCR: %s", exc)
                    warnings.append("Vision read failed — used OCR.")
            if vision_txns:
                txns.extend(vision_txns)
            else:
                for data, _, _ in images:
                    text = _ocr_with_progress(jid, _image_to_pdf(data))
                    if text.strip():
                        txns.extend(coach_extract_pdf(text, warnings.append,
                                                      user_id=user_id))
            if txns:
                warnings.append(f"Read {len(txns)} transactions from your "
                                f"image{'s' if len(images) > 1 else ''} — "
                                "spot-check the figures below before trusting them.")

        # --- CSV / PDF items: existing text pipeline ---
        for data, name, ctype in others:
            is_pdf = name.endswith(".pdf") or "pdf" in ctype
            if not is_pdf:
                txns.extend(parse_transactions_csv(
                    data.decode("utf-8", errors="replace")))
                continue
            text = _pdf_to_text(data)
            ocr_used = False
            if len(text.strip()) < 40:
                ocr_used = True
                warnings.append("Scanned PDF — used OCR.")
                text = _ocr_with_progress(jid, data)
            if not text.strip():
                raise ValueError("No readable text found in the document.")
            # No OCR phase (text PDF) -> extraction owns the whole bar (10-92%);
            # with OCR (8-54%) it picks up from 55%. Avoids an unearned jump.
            base, span = (55, 37) if ocr_used else (10, 82)
            _job_set(jid, stage="extract", pct=base, message="Extracting transactions…")
            txns.extend(coach_extract_pdf(
                text, warnings.append, user_id=user_id,
                on_progress=lambda i, n: _job_set(
                    jid, stage="extract", pct=base + int(span * i / max(n, 1)),
                    message=f"Extracting transactions ({i} of {n})…")))

        txns = coach.dedupe_transactions(txns)
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
        out["benchmark"] = _benchmark_for(report.discipline_score)
        _persist_audit(user_id, out, audit_txns)
        _job_set(jid, status="done", stage="done", pct=100, message="Done.", report=out)
    except GuestLimitExceeded as exc:
        _job_set(jid, status="error", stage="error",
                 error=f"{exc} Sign in (free) to keep going.")
    except OcrUnavailable:
        _job_set(jid, status="error", stage="error",
                 error="We couldn't read this document. Enable the vision reader "
                       "or the OCR service (AGENTICWHALES_OCR_URL), or upload a "
                       "text-based CSV/PDF.")
    except Exception as exc:  # noqa: BLE001
        _job_set(jid, status="error", stage="error", error=str(exc))


@router.post("/api/coach/upload_async")
async def coach_upload_async(request: Request,
                             file: List[UploadFile] = File(...),
                             user_id: str = Depends(optional_user_id)):
    """Start an upload+analyze job; returns a job_id to stream progress from.
    Accepts one or more repeated `file` fields (a single file stays
    wire-compatible — it arrives as a one-element list)."""
    items = [(await f.read(), f.filename, f.content_type) for f in file]
    if ((not user_id or user_id == auth.ANONYMOUS_USER_ID)
            and _needs_guest_cap(items)):
        try:
            _check_guest_upload_cap(request)
        except GuestLimitExceeded as exc:
            return _guest_limit_response(user_id, exc)
    track_event("upload_started", user_id,
                {"kind": "async", "n_files": len(items)})
    _prune_jobs()
    jid = _new_job()
    # Bounded pool, not a thread per upload: beyond the cap, jobs sit "Queued…"
    # and the SSE stream reports that state until a worker frees up.
    _UPLOAD_POOL.submit(_run_upload_job, jid, items, user_id)
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
async def coach_demo(user_id: str = Depends(optional_user_id)):
    track_event("demo_viewed", user_id)
    report = coach.audit_trades(coach.sample_history())
    out = report.to_dict()
    # Demo + benchmark would be double-synthetic — label it, claim nothing.
    out["benchmark"] = {"cohort": "demo", "label": "sample trader — no benchmark"}
    return out


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
    # None (the UI's default) lets the user's ADOPTED rule supply the cap;
    # an explicit value still overrides. Old clients sending 0.02 keep their
    # old behavior — the UI no longer sends it unless edited.
    max_risk_pct: Optional[float] = None
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
    rb = _rulebook_kwargs(user_id, p.max_risk_pct)
    applied = rb.pop("_applied", [])
    verdict = pretrade.check_trade(
        trade, equity=p.equity, recent_trades=recent,
        leak_profile=profile, **rb,
    )
    out = verdict.to_dict()
    out["rules_applied"] = applied   # "checked against YOUR rules", visibly
    # Decision support: a fast technical read on the symbol (+ optional LLM note).
    # Kept visually separate from the rule checks in the UI — process checks
    # are the product; this is context.
    if p.trade.symbol:
        out["decision_support"] = decision_support.analyze_symbol(
            p.trade.symbol, llm_note=p.include_ai)
    return out
