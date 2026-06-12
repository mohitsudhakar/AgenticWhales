"""SnapTrade brokerage-connect endpoints (read-only).

Flow: connect → SnapTrade registers the user (once) and returns a Connection
Portal URL → the user links their brokerage there → sync pulls their activities,
normalizes them into the coach's `Transaction` model, and runs the same audit +
persistence as a CSV/PDF upload. We never place orders.

All endpoints require a signed-in user. Everything is gated on SnapTrade being
configured (SNAPTRADE_CLIENT_ID / SNAPTRADE_CONSUMER_KEY); when it isn't, the
endpoints report `configured: false` so the UI can explain instead of erroring.
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agenticwhales import coach, prices
from agenticwhales.dataflows import snaptrade_client, snaptrade_normalize
from web import auth
from web import coach_api
from web.auth import get_current_user_id
from web.coach_api import _merge_user_trades, _persist_audit, optional_user_id

router = APIRouter()
log = logging.getLogger(__name__)


def _require_user(user_id: str) -> Optional[JSONResponse]:
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return JSONResponse({"error": "Sign in to connect a brokerage."}, status_code=401)
    return None


def _sync_status(user_id: str) -> dict:
    """Ingestion transparency: when the last sync ran, what date range the
    stored history covers, and how many rows we hold. All derived — no new
    tables. `last_synced_at` needs the coach_audits.origin column
    (docs/migrations/2026-06-10_coach_audit_origin.sql); until that migration
    runs it degrades to null rather than breaking the endpoint."""
    from agenticwhales.transactions.models import normalize_date
    txns = auth.get_coach_trades(user_id)
    # Raw dict reads bypass the Transaction model's date normalization, and a
    # lexicographic min/max over mixed formats ('01/02/2025' vs '2024-…')
    # would report a nonsense window — normalize and keep ISO-shaped only.
    dates = sorted(d for t in txns if t.get("date")
                   for d in [normalize_date(str(t.get("date", "")))[:10]]
                   if len(d) == 10 and d[:4].isdigit() and d[4] == "-")
    last_synced = None
    try:
        if auth._db_writable():
            rows = auth._select_columns(
                "coach_audits", filters={"user_id": user_id},
                select="created_at,origin", order="created_at.desc", limit=100)
        else:
            rows = sorted((r for (t, _), r in auth._memstore.items()
                           if t == "coach_audits" and r.get("user_id") == user_id),
                          key=lambda r: r.get("created_at") or "", reverse=True)
        for r in rows:
            if str(r.get("origin") or "").startswith("sync"):
                last_synced = r.get("created_at")
                break
    except Exception as exc:  # noqa: BLE001 — status must never 500
        log.warning("sync status derivation failed: %s", exc)
    return {
        "last_synced_at": last_synced,
        "history_start": dates[0] if dates else None,
        "history_end": dates[-1] if dates else None,
        "n_transactions": len(txns),
    }


@router.get("/api/snaptrade/status")
async def snaptrade_status(user_id: str = Depends(optional_user_id)):
    configured = snaptrade_client.from_env() is not None
    connected = bool(configured and user_id and user_id != auth.ANONYMOUS_USER_ID
                     and auth.get_snaptrade_user(user_id))
    out = {"configured": configured, "connected": connected}
    if connected:
        out["sync"] = _sync_status(user_id)
    return out


class ConnectPayload(BaseModel):
    redirect: Optional[str] = None  # where SnapTrade returns the user after linking


@router.post("/api/snaptrade/connect")
async def snaptrade_connect(p: ConnectPayload, user_id: str = Depends(get_current_user_id)):
    guard = _require_user(user_id)
    if guard:
        return guard
    client = snaptrade_client.from_env()
    if client is None:
        return JSONResponse(
            {"configured": False,
             "error": "Brokerage connect isn't configured yet (set SNAPTRADE_CLIENT_ID "
                      "and SNAPTRADE_CONSUMER_KEY)."},
            status_code=503)
    try:
        rec = auth.get_snaptrade_user(user_id)
        if not rec:
            reg = client.register_user(user_id)
            secret = reg.get("userSecret")
            if not secret:
                raise RuntimeError("SnapTrade did not return a userSecret")
            auth.upsert_snaptrade_user(user_id, reg.get("userId", user_id), secret)
            rec = {"st_user_id": reg.get("userId", user_id), "st_user_secret": secret}
        opts = {"customRedirect": p.redirect} if p.redirect else {}
        login = client.login_portal(rec["st_user_id"], rec["st_user_secret"], **opts)
        url = login.get("redirectURI") or login.get("redirectUri")
        if not url:
            raise RuntimeError("SnapTrade did not return a portal URL")
        return {"configured": True, "url": url}
    except Exception as exc:  # noqa: BLE001
        log.warning("snaptrade connect failed: %s", exc)
        return JSONResponse({"error": f"Could not start the brokerage connection: {exc}"},
                            status_code=502)


def _sync_core(user_id: str, client, rec, *, lookback_days: int = 365 * 3,
               origin: str = "sync_manual"):
    """Pull activities -> normalize -> merge into the timeline -> audit -> persist.
    Returns the report dict, or None if the connected accounts have no trades yet."""
    start = (_dt.date.today() - _dt.timedelta(days=lookback_days)).isoformat()
    activities = client.get_activities(rec["st_user_id"], rec["st_user_secret"], start=start)
    txns = snaptrade_normalize.normalize_activities(activities)
    if not txns:
        return None
    before = len(auth.get_coach_trades(user_id))
    audit_txns = _merge_user_trades(user_id, txns)  # accumulate the timeline
    report = coach.audit_trades(audit_txns, price_fetcher=prices.fetch_ohlc)
    out = report.to_dict()
    out["n_transactions"] = len(audit_txns)
    # "new" = rows the merge actually ADDED after dedupe — a full 3-year re-pull
    # of unchanged history is 0 new, and the UI says so honestly.
    out["new_transactions"] = max(0, len(audit_txns) - before)
    out["pulled_transactions"] = len(txns)
    out["source"] = "snaptrade"
    _persist_audit(user_id, out, audit_txns, origin=origin)
    # "broker connected" means data actually flowed, not that a portal URL was
    # minted — so the funnel event fires on the FIRST successful sync only.
    if not auth.list_audit(actor=user_id, action="broker_connected", limit=1):
        coach_api.track_event("broker_connected", user_id,
                              {"n_transactions": len(txns)})
    return out


def sync_user(user_id: str):
    """Server-side sync for one connected user (used by the nightly cron).
    Returns the report dict, or None if not configured / not connected / no trades."""
    client = snaptrade_client.from_env()
    if client is None:
        return None
    rec = auth.get_snaptrade_user(user_id)
    if not rec:
        return None
    return _sync_core(user_id, client, rec, origin="sync_auto")


@router.post("/api/snaptrade/sync")
async def snaptrade_sync(user_id: str = Depends(get_current_user_id)):
    guard = _require_user(user_id)
    if guard:
        return guard
    client = snaptrade_client.from_env()
    if client is None:
        return JSONResponse({"configured": False, "error": "SnapTrade not configured."},
                            status_code=503)
    rec = auth.get_snaptrade_user(user_id)
    if not rec:
        return JSONResponse({"error": "Connect a brokerage first."}, status_code=400)
    try:
        out = _sync_core(user_id, client, rec)
    except Exception as exc:  # noqa: BLE001
        log.warning("snaptrade sync failed: %s", exc)
        return JSONResponse({"error": f"Could not read your brokerage activity: {exc}"},
                            status_code=502)
    if out is None:
        return JSONResponse(
            {"error": "No trades found in your connected accounts yet."}, status_code=404)
    return out
