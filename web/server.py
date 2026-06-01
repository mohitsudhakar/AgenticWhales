"""FastAPI app — REST + WebSocket front for the analysis runner."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agenticwhales import conviction_decay, portfolio
from agenticwhales.llm_clients.model_catalog import MODEL_OPTIONS
from agenticwhales.universe import universe_for_api

from . import admin, auth, batch_storage, storage, waitlist
from .auth import authenticate_websocket, get_current_user_id, require_admin
from .batch_runner import BatchRunner, build_batch
from .runner import (
    ANALYST_AGENT_NAMES,
    ANALYST_ORDER,
    FIXED_TEAMS,
    SECTION_AGENT,
    SessionRunner,
    build_session,
    config_signature,
)

load_dotenv()
load_dotenv(".env.enterprise", override=False)

# Providers exposed in the new-analysis / basket dropdowns.
# Temporarily limited to Google and DeepSeek — re-enable any of the commented
# entries below to bring them back. The model catalog and LLM clients for
# every provider stay wired up, so flipping a line back on is the only step.
PROVIDERS = [
    # {"key": "openai", "label": "OpenAI", "url": "https://api.openai.com/v1"},
    {"key": "google", "label": "Google", "url": None},
    # {"key": "anthropic", "label": "Anthropic", "url": "https://api.anthropic.com/"},
    # {"key": "xai", "label": "xAI", "url": "https://api.x.ai/v1"},
    {"key": "deepseek", "label": "DeepSeek", "url": "https://api.deepseek.com"},
    # {"key": "qwen", "label": "Qwen", "url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    # {"key": "glm", "label": "GLM", "url": "https://open.bigmodel.cn/api/paas/v4/"},
    # {"key": "ollama", "label": "Ollama", "url": "http://localhost:11434/v1"},
]

LANGUAGES = [
    "English", "Chinese", "Japanese", "Korean", "Hindi", "Spanish",
    "Portuguese", "French", "German", "Arabic", "Russian",
]

_runners: Dict[str, SessionRunner] = {}
_runners_lock = asyncio.Lock()
_batch_runners: Dict[str, BatchRunner] = {}
_batch_runners_lock = asyncio.Lock()


def _register_session_runner(runner: SessionRunner) -> None:
    """Register a SessionRunner spawned by a BatchRunner so /api/sessions works."""
    _runners[runner.session["id"]] = runner


_STALE_RUNNING_CUTOFF_SECONDS = int(
    os.getenv("AGENTICWHALES_STALE_RUNNING_DELETE_CUTOFF_SECONDS", str(24 * 60 * 60))
)
_STALE_RUNNING_SWEEP_INTERVAL_SECONDS = int(
    os.getenv("AGENTICWHALES_STALE_RUNNING_SWEEP_INTERVAL_SECONDS", str(60 * 60))
)


async def _stale_running_sweep_loop() -> None:
    """Background task: every hour, hard-delete sessions that have been
    flagged `running` / `pending` / `composing_report` for longer than the
    24-hour cutoff. Runs alongside the FastAPI app via the lifespan; no
    APScheduler dependency.

    The function `auth.delete_stuck_running_sessions` is idempotent — if no
    rows match, it returns 0 and the loop just sleeps again.
    """
    import logging
    log = logging.getLogger("web.server.stale_sweep")
    # Boot sweep — run once immediately so a freshly-started server cleans
    # up any in-flight rows orphaned by the previous process crash.
    try:
        deleted = auth.delete_stuck_running_sessions(
            older_than_seconds=_STALE_RUNNING_CUTOFF_SECONDS, limit=500,
        )
        if deleted:
            log.warning("boot sweep: deleted %d stuck running sessions", deleted)
    except Exception as exc:
        log.exception("boot sweep failed: %s", exc)
    while True:
        try:
            await asyncio.sleep(_STALE_RUNNING_SWEEP_INTERVAL_SECONDS)
            deleted = auth.delete_stuck_running_sessions(
                older_than_seconds=_STALE_RUNNING_CUTOFF_SECONDS, limit=500,
            )
            if deleted:
                log.warning("hourly sweep: deleted %d stuck running sessions", deleted)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.exception("hourly sweep failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Sessions and batches now live in Supabase Postgres (or the in-memory
    # fallback when Supabase isn't configured) — nothing to set up on disk.
    # Spawn a single background sweeper that nukes any session that's been
    # flagged "running" for more than a day. The legitimate slowest run
    # finishes in <5 min, so a 24-hour holdover is always orphan state from
    # a pod crash or a deploy mid-flight.
    sweep_task = asyncio.create_task(_stale_running_sweep_loop())
    try:
        yield
    finally:
        sweep_task.cancel()
        try:
            await sweep_task
        except (asyncio.CancelledError, Exception):
            pass


app = FastAPI(title="AgenticWhales Web", lifespan=lifespan)
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _supabase_runtime_config_tag() -> str:
    """Inline `<script>` tag that hands the Supabase URL + anon key to the
    browser at request time. Reads from env so dev/prod can be swapped without
    rebuilding static assets. Returns an empty string when either var is unset
    (in which case supabase-client.js falls back to its placeholder sentinel
    and the welcome modal degrades to guest mode)."""
    url = os.getenv("AGENTICWHALES_SUPABASE_URL")
    key = os.getenv("AGENTICWHALES_SUPABASE_ANON_KEY")
    if not (url and key):
        return ""
    # json.dumps gives us safe JS string literals (escapes quotes, newlines, etc.)
    return (
        f"<script>window.__AGENTICWHALES_SUPABASE_CONFIG = "
        f"{{url: {json.dumps(url)}, anonKey: {json.dumps(key)}}};</script>"
    )


def _render_html(filename: str) -> HTMLResponse:
    """Read a static HTML page and inject the Supabase config tag before
    </head> so supabase-client.js sees the global on first evaluation."""
    html = (STATIC_DIR / filename).read_text(encoding="utf-8")
    config_tag = _supabase_runtime_config_tag()
    if config_tag:
        html = html.replace("</head>", config_tag + "</head>", 1)
    return HTMLResponse(html)


@app.get("/", response_class=HTMLResponse)
async def root_page() -> HTMLResponse:
    """Root serves the public marketing landing page. Its 'Try it today' CTAs
    link to /signin, where the Google sign-in + disclaimer gate lives. Static,
    no auth, no data — safe to share the bare URL with prospects."""
    return _render_html("welcome.html")


@app.get("/signin", response_class=HTMLResponse)
async def signin_page() -> HTMLResponse:
    """Sign-in / disclaimer gate. landing.js does the conditional redirect to
    /fund once Supabase reports a signed-in user — and Google OAuth returns to
    THIS path (redirectTo = origin + pathname), so it must be a stable URL that
    serves landing.html. Must NOT be a server-side 307 (it would race against
    /fund's own 'redirect to /signin when signed out' gate → reload loop)."""
    return _render_html("landing.html")


@app.get("/fund", response_class=HTMLResponse)
async def fund_page() -> HTMLResponse:
    """Fund dashboard: live debate, analyses transcripts, book, journal, lab."""
    return _render_html("fund.html")


@app.get("/analyze", response_class=HTMLResponse)
async def analyze_page() -> HTMLResponse:
    """Power-user surface: one-shot analyses + batches with full model picker."""
    return _render_html("index.html")


@app.get("/welcome", response_class=HTMLResponse)
async def welcome_page() -> HTMLResponse:
    """Alias for the marketing landing page (same content as /). Kept so any
    previously-shared /welcome links keep working."""
    return _render_html("welcome.html")


# --------------------------------------------------------------------------
# Waitlist — public signup + admin-only export. Stored in the app's normal
# dual-mode store (Supabase or in-memory), with an optional live Google Sheet
# mirror via WAITLIST_SHEET_WEBHOOK_URL. No new credentials required.
# --------------------------------------------------------------------------

class WaitlistPayload(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    name: str = Field("", max_length=120)
    company: str = Field("", max_length=160)
    note: str = Field("", max_length=1000)
    source: str = Field("landing", max_length=60)


@app.post("/api/waitlist")
async def join_waitlist(payload: WaitlistPayload) -> Dict[str, Any]:
    """Public: join the waitlist. Idempotent on email. Returns a small JSON
    ack (never the stored row's internal id)."""
    try:
        waitlist.add_signup(
            email=payload.email, name=payload.name, company=payload.company,
            note=payload.note, source=payload.source or "landing",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "message": "You're on the list — we'll be in touch."}


@app.get("/api/waitlist/count")
async def waitlist_count() -> Dict[str, Any]:
    """Public: social-proof counter for the landing page. `display` is the
    vanity figure shown in the UI (floored at 100, doubled past the threshold);
    `count` is the true figure (kept for the admin/debug surface)."""
    real = auth.count_waitlist_signups()
    return {"count": real, "display": waitlist.display_count(real)}


@app.get("/api/waitlist/export.csv")
async def waitlist_export(user_id: str = Depends(require_admin)) -> PlainTextResponse:
    """Admin-only: download every signup as CSV (the 'spreadsheet')."""
    csv_text = waitlist.to_csv(auth.list_waitlist_signups())
    return PlainTextResponse(
        csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="waitlist.csv"'},
    )


@app.get("/usage", response_class=HTMLResponse)
async def usage_page() -> HTMLResponse:
    """Standalone admin-only usage dashboard. The page itself is served to
    anyone (it has to be, so the unauthenticated state can show a sign-in
    prompt), but the data fetch behind it is gated by require_admin."""
    return _render_html("usage.html")


@app.get("/healthz")
async def healthz() -> Dict[str, Any]:
    """Liveness — always 200 while the process is up. No auth, no DB."""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> JSONResponse:
    """Readiness — 200 only when the DB is reachable. Body shape is stable
    regardless of outcome so probes can parse it either way."""
    checks: Dict[str, Any] = {}
    db_ok = True
    try:
        # Cheap reachability probe: reading the active compliance version
        # round-trips to Postgres when configured, and is a pure in-memory
        # read otherwise (which is also "ready").
        auth.active_compliance_version()
        checks["db"] = "ok"
    except Exception as exc:  # noqa: BLE001
        db_ok = False
        checks["db"] = f"error: {exc}"
    ready = db_ok
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"ready": ready, "checks": checks},
    )


# Defaults applied to the new-analysis and basket forms. Driven by env vars
# so future model launches (Gemini 4, Claude Opus 5, ...) are a one-line
# operator change — no code edits or redeploys needed.
DEFAULT_PROVIDER = os.getenv("AGENTICWHALES_DEFAULT_PROVIDER", "google")
DEFAULT_DEEP_MODEL = os.getenv("AGENTICWHALES_DEFAULT_DEEP_MODEL", "gemini-3.1-pro-preview")
DEFAULT_QUICK_MODEL = os.getenv("AGENTICWHALES_DEFAULT_QUICK_MODEL", "gemini-3-flash-preview")


@app.get("/api/config")
async def get_config() -> Dict[str, Any]:
    teams: List[Dict[str, Any]] = [
        {"name": "Analyst Team", "agents": [ANALYST_AGENT_NAMES[a] for a in ANALYST_ORDER]}
    ]
    teams.extend({"name": name, "agents": list(agents)} for name, agents in FIXED_TEAMS)
    return {
        "providers": PROVIDERS,
        "models": MODEL_OPTIONS,
        "analysts": [{"key": a, "label": ANALYST_AGENT_NAMES[a]} for a in ANALYST_ORDER],
        "teams": teams,
        "section_agent": SECTION_AGENT,
        "languages": LANGUAGES,
        "universe": universe_for_api(),
        "defaults": {
            "provider": DEFAULT_PROVIDER,
            "deep_model": DEFAULT_DEEP_MODEL,
            "quick_model": DEFAULT_QUICK_MODEL,
        },
    }


def _summary(s: Dict[str, Any]) -> Dict[str, Any]:
    # Include pm_decision in the list payload so /fund's Analyses + Recent
    # Activity tables can render the verdict / price target without an N+1
    # round-trip per row. The full session is still fetched on demand for
    # the detail view.
    return {
        "id": s["id"],
        "ticker": s["ticker"],
        "analysis_date": s["analysis_date"],
        "status": s["status"],
        "created_at": s["created_at"],
        "completed_at": s.get("completed_at"),
        "pm_decision": s.get("pm_decision") or s.get("portfolio_decision"),
        "failure_reason": s.get("failure_reason"),
    }


@app.get("/api/sessions")
async def list_sessions(user_id: str = Depends(get_current_user_id)) -> List[Dict[str, Any]]:
    # In-memory runners may have an in-flight session that hasn't been
    # persisted to disk yet — merge them with what's on disk so the sidebar
    # never blanks out a freshly-created session.
    on_disk = storage.list_all(user_id=user_id)
    in_memory = {
        sid: r.session
        for sid, r in _runners.items()
        if r.session.get("user_id") == user_id
    }
    seen = {s["id"] for s in on_disk}
    merged = on_disk + [s for sid, s in in_memory.items() if sid not in seen]
    merged.sort(key=lambda s: s.get("created_at", 0), reverse=True)
    return [_summary(s) for s in merged]


class CreateSessionPayload(BaseModel):
    ticker: str = Field(min_length=1)
    analysis_date: str = Field(min_length=8)
    llm_provider: str
    backend_url: Optional[str] = None
    quick_think_llm: str
    deep_think_llm: str
    research_depth: int = 1
    analysts: List[str] = []
    google_thinking_level: Optional[str] = None
    openai_reasoning_effort: Optional[str] = None
    anthropic_effort: Optional[str] = None
    output_language: str = "English"


# Repeat-analysis cache. Both knobs are env-driven so prod can dial caching
# without a code change.
#   AGENTICWHALES_CACHE_ENABLED      "true"/"false" (default: true)
#   AGENTICWHALES_CACHE_TTL_MINUTES  integer minutes (default: 30)
def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


CACHE_ENABLED = _env_bool("AGENTICWHALES_CACHE_ENABLED", True)
try:
    CACHE_TTL_MINUTES = max(1, int(os.getenv("AGENTICWHALES_CACHE_TTL_MINUTES", "30")))
except ValueError:
    CACHE_TTL_MINUTES = 30


@app.post("/api/sessions")
async def create_session(
    payload: CreateSessionPayload,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    sig = config_signature(payload.model_dump())

    # Cache check: if this user ran the same ticker+date with the same config
    # within CACHE_TTL_MINUTES and it completed successfully, hand them the
    # existing session id. Saves LLM cost + doesn't burn quota. Disabled
    # globally when AGENTICWHALES_CACHE_ENABLED is false.
    if CACHE_ENABLED:
        cached = auth.find_cached_session(
            user_id=user_id,
            ticker=payload.ticker,
            analysis_date=payload.analysis_date,
            config_sig=sig,
            ttl_minutes=CACHE_TTL_MINUTES,
        )
        if cached:
            summary = _summary(cached)
            summary["cached"] = True
            return summary

    session = build_session(payload.model_dump())
    session["user_id"] = user_id
    # Stamp the signature so future cache lookups can match it back.
    session.setdefault("config", {})["__sig"] = sig
    storage.save(session)
    loop = asyncio.get_running_loop()
    runner = SessionRunner(session, loop)
    async with _runners_lock:
        _runners[session["id"]] = runner
    runner.start()
    return _summary(session)


def _ensure_owner(obj: Optional[Dict[str, Any]], user_id: str) -> None:
    """404 if the object is missing or owned by someone else. We deliberately
    don't return 403 — leaks the existence of the row."""
    if not obj or obj.get("user_id") != user_id:
        raise HTTPException(404, "Not found")


@app.get("/api/sessions/{sid}")
async def get_session(sid: str, user_id: str = Depends(get_current_user_id)) -> Dict[str, Any]:
    runner = _runners.get(sid)
    if runner:
        _ensure_owner(runner.session, user_id)
        return runner.snapshot()
    s = storage.load(sid)
    _ensure_owner(s, user_id)
    return s


@app.delete("/api/sessions/{sid}")
async def delete_session(sid: str, user_id: str = Depends(get_current_user_id)) -> Dict[str, Any]:
    runner = _runners.get(sid)
    if runner:
        _ensure_owner(runner.session, user_id)
        if runner.session.get("status") == "running":
            raise HTTPException(409, "Cannot delete a running session")
    else:
        _ensure_owner(storage.load(sid), user_id)
    async with _runners_lock:
        _runners.pop(sid, None)
    storage.delete(sid)
    return {"deleted": True}


@app.post("/api/sessions/{sid}/cancel")
async def cancel_session(sid: str, user_id: str = Depends(get_current_user_id)) -> Dict[str, Any]:
    runner = _runners.get(sid)
    if not runner:
        # No in-flight runner — either it already finished or the process restarted.
        # Both cases are 409 (nothing actively running to cancel).
        _ensure_owner(storage.load(sid), user_id)
        raise HTTPException(409, "Session is not running")
    _ensure_owner(runner.session, user_id)
    if not runner.cancel():
        raise HTTPException(409, "Session is not in a cancellable state")
    return runner.snapshot()


@app.websocket("/api/sessions/{sid}/stream")
async def stream(ws: WebSocket, sid: str, token: Optional[str] = Query(None)) -> None:
    await ws.accept()
    user_id = await authenticate_websocket(ws, token)
    if not user_id:
        return
    runner = _runners.get(sid)
    if not runner:
        s = storage.load(sid)
        if not s or s.get("user_id") != user_id:
            await ws.close(code=4404)
            return
        await ws.send_json({"type": "session", "session": s})
        await ws.close()
        return
    if runner.session.get("user_id") != user_id:
        await ws.close(code=4404)
        return

    queue = runner.subscribe()
    await ws.send_json({"type": "session", "session": runner.snapshot()})
    try:
        while True:
            event = await queue.get()
            await ws.send_json(event)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        runner.unsubscribe(queue)


def _batch_summary(b: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": b["id"],
        "analysis_date": b["analysis_date"],
        "status": b["status"],
        "created_at": b["created_at"],
        "completed_at": b.get("completed_at"),
        "ticker_count": len(b.get("items", [])),
        "tickers": [it["ticker"] for it in b.get("items", [])],
    }


@app.get("/api/batches")
async def list_batches(user_id: str = Depends(get_current_user_id)) -> List[Dict[str, Any]]:
    on_disk = batch_storage.list_all(user_id=user_id)
    in_memory = {
        bid: r.batch
        for bid, r in _batch_runners.items()
        if r.batch.get("user_id") == user_id
    }
    seen = {b["id"] for b in on_disk}
    merged = on_disk + [b for bid, b in in_memory.items() if bid not in seen]
    merged.sort(key=lambda b: b.get("created_at", 0), reverse=True)
    return [_batch_summary(b) for b in merged]


class CreateBatchPayload(BaseModel):
    tickers: List[str] = Field(min_length=1)
    analysis_date: str = Field(min_length=8)
    llm_provider: str
    backend_url: Optional[str] = None
    quick_think_llm: str
    deep_think_llm: str
    research_depth: int = 1
    analysts: List[str] = []
    google_thinking_level: Optional[str] = None
    openai_reasoning_effort: Optional[str] = None
    anthropic_effort: Optional[str] = None
    output_language: str = "English"
    max_concurrency: int = Field(4, ge=1, le=16)


@app.post("/api/batches")
async def create_batch(
    payload: CreateBatchPayload,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    batch = build_batch(payload.model_dump())
    batch["user_id"] = user_id
    batch_storage.save(batch)
    loop = asyncio.get_running_loop()
    runner = BatchRunner(batch, loop, register_session=_register_session_runner)
    async with _batch_runners_lock:
        _batch_runners[batch["id"]] = runner
    runner.start()
    return _batch_summary(batch)


@app.get("/api/batches/{bid}")
async def get_batch(bid: str, user_id: str = Depends(get_current_user_id)) -> Dict[str, Any]:
    runner = _batch_runners.get(bid)
    if runner:
        _ensure_owner(runner.batch, user_id)
        return runner.snapshot()
    b = batch_storage.load(bid)
    _ensure_owner(b, user_id)
    return b


@app.delete("/api/batches/{bid}")
async def delete_batch(bid: str, user_id: str = Depends(get_current_user_id)) -> Dict[str, Any]:
    runner = _batch_runners.get(bid)
    if runner:
        _ensure_owner(runner.batch, user_id)
        if runner.batch.get("status") in ("running", "composing_report", "pending"):
            raise HTTPException(409, "Cannot delete a running batch")
    else:
        _ensure_owner(batch_storage.load(bid), user_id)
    async with _batch_runners_lock:
        _batch_runners.pop(bid, None)
    batch_storage.delete(bid)
    return {"deleted": True}


@app.post("/api/batches/{bid}/cancel")
async def cancel_batch(bid: str, user_id: str = Depends(get_current_user_id)) -> Dict[str, Any]:
    runner = _batch_runners.get(bid)
    if not runner:
        _ensure_owner(batch_storage.load(bid), user_id)
        raise HTTPException(409, "Batch is not running")
    _ensure_owner(runner.batch, user_id)
    if not runner.cancel():
        raise HTTPException(409, "Batch is not in a cancellable state")
    return runner.snapshot()


@app.websocket("/api/batches/{bid}/stream")
async def stream_batch(ws: WebSocket, bid: str, token: Optional[str] = Query(None)) -> None:
    await ws.accept()
    user_id = await authenticate_websocket(ws, token)
    if not user_id:
        return
    runner = _batch_runners.get(bid)
    if not runner:
        b = batch_storage.load(bid)
        if not b or b.get("user_id") != user_id:
            await ws.close(code=4404)
            return
        await ws.send_json({"type": "batch", "batch": b})
        await ws.close()
        return
    if runner.batch.get("user_id") != user_id:
        await ws.close(code=4404)
        return

    queue = runner.subscribe()
    await ws.send_json({"type": "batch", "batch": runner.snapshot()})
    try:
        while True:
            event = await queue.get()
            await ws.send_json(event)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        runner.unsubscribe(queue)


@app.get("/api/portfolio")
async def get_portfolio(user_id: str = Depends(get_current_user_id)) -> Dict[str, Any]:
    # Portfolio remains a single shared file for now; per-user portfolios
    # would be the next step. The dependency ensures only authed callers
    # can read it.
    return {"positions": portfolio.load_all()}


class PortfolioPayload(BaseModel):
    positions: Dict[str, Dict[str, Any]] = Field(default_factory=dict)


@app.put("/api/portfolio")
async def put_portfolio(
    payload: PortfolioPayload,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    portfolio.save_all(payload.positions)
    return {"positions": portfolio.load_all()}


@app.get("/api/usage/me")
async def usage_me(user_id: str = Depends(require_admin)) -> Dict[str, Any]:
    """Tiny probe the /usage page uses to decide whether to render the
    dashboard or a 'not authorised' message. 200 = caller is admin;
    403 otherwise."""
    return {"user_id": user_id, "admin_email": auth.ADMIN_EMAIL}


@app.get("/api/usage/dashboard")
async def usage_dashboard(user_id: str = Depends(require_admin)) -> Dict[str, Any]:
    return admin.build_dashboard()


# ============================================================================
# COMPLIANCE & AUDIT
# ============================================================================
#
# Server-driven attestation gate the /fund client polls at boot. The full
# version lives in auth.py (versioned legal docs, per-user signed rows). The
# routes here just expose the helpers.

_LEGAL_DOCS = {
    "disclaimer": {
        "title": "Disclaimer",
        "body": (
            "AgenticWhales is paper-trading only — no real broker, no real money. "
            "Nothing surfaced by the multi-agent debate constitutes financial, "
            "investment, legal, or tax advice. Decisions you take outside this "
            "system are yours alone."
        ),
    },
    "privacy": {
        "title": "Privacy Policy",
        "body": (
            "We store your Google account ID, display name, email, usage "
            "counters, and the analyses you run. We do not store your Google "
            "password and do not sell personal data. Full policy at /privacy "
            "or via the link below."
        ),
    },
    "terms": {
        "title": "Terms of Use",
        "body": (
            "AgenticWhales is provided as-is for research and education. You "
            "agree not to abuse provider APIs, bypass quotas, or use the tool "
            "for market manipulation. Continued use after a Terms update "
            "constitutes acceptance of the revised terms."
        ),
    },
}


@app.get("/api/compliance/docs")
async def get_compliance_docs() -> Dict[str, Any]:
    return {"version": auth.active_compliance_version(), "docs": _LEGAL_DOCS}


@app.get("/api/audit/compliance-ack")
async def get_compliance_ack(user_id: str = Depends(get_current_user_id)) -> Dict[str, Any]:
    version = auth.active_compliance_version()
    row = auth.latest_active_attestation_for_user(user_id) if user_id else None
    base = {
        "version": version,
        "docs": _LEGAL_DOCS,
        "disclaimer_text": _LEGAL_DOCS["disclaimer"]["body"],
    }
    if row:
        return {**base, "needs_attestation": False, "user_version": row.get("version"),
                "attestation_id": row.get("id"), "created_at": row.get("created_at")}
    return {**base, "needs_attestation": True, "user_version": None}


def require_active_attestation(user_id: str, attestation_id: Optional[str]) -> Dict[str, Any]:
    """Resolve + validate the compliance attestation that authorizes an action.

    Resolution order:
      1. If ``attestation_id`` is supplied and loads, it must belong to
         ``user_id`` (else 404) and match the active disclaimer version
         (else 412). A valid row is returned.
      2. Otherwise (no id, or the id didn't load) fall back to the user's
         most recent non-revoked attestation for the active version.
      3. If neither yields a valid row, raise 412 with a machine-parseable
         ``compliance_required`` code so the client can pop the modal.
    """
    active = auth.active_compliance_version()

    def _needs(message: str) -> HTTPException:
        return HTTPException(
            status_code=412,
            detail={"code": "compliance_required", "message": message,
                    "active_version": active},
        )

    if attestation_id:
        row = auth.load_compliance_attestation(attestation_id)
        if row:
            if row.get("user_id") != user_id:
                raise HTTPException(status_code=404, detail="Attestation not found.")
            if row.get("version") != active:
                raise _needs("Disclaimer has been updated; please re-accept.")
            return row

    latest = auth.latest_active_attestation_for_user(user_id)
    if latest:
        return latest
    raise _needs("Compliance attestation required — please accept the current disclaimer.")


class ComplianceAckPayload(BaseModel):
    version: Optional[str] = None
    ack_paper_only: bool = False
    ack_not_advice: bool = False
    ack_jurisdiction: bool = False


@app.post("/api/audit/compliance-ack")
async def post_compliance_ack(
    payload: ComplianceAckPayload,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    import time as _time
    import uuid as _uuid
    if not (payload.ack_paper_only and payload.ack_not_advice and payload.ack_jurisdiction):
        raise HTTPException(400, {"code": "missing_acks", "message": "All three acknowledgements are required."})
    active = auth.active_compliance_version()
    # A client that posts a stale/mismatched version (the classic "v1" vs
    # "v1.0" bug) is rejected with 409 + the active version so it can correct.
    if payload.version and payload.version != active:
        raise HTTPException(
            409,
            {"code": "version_mismatch", "message": f"Disclaimer version is {active}.",
             "active_version": active},
        )
    version = active
    row = {
        "id": _uuid.uuid4().hex,
        "user_id": user_id,
        "version": version,
        "ack_paper_only": True,
        "ack_not_advice": True,
        "ack_jurisdiction": True,
        "disclaimer_text": _LEGAL_DOCS["disclaimer"]["body"],
        "jurisdiction": None,
        "created_at": auth._ts_iso(_time.time()),
        "revoked_at": None,
    }
    auth.save_compliance_attestation(row)
    # `id` is the canonical field; `attestation_id` kept for back-compat.
    return {"id": row["id"], "attestation_id": row["id"],
            "version": version, "created_at": row["created_at"]}


# ============================================================================
# PAPER ACCOUNT / POSITIONS / ORDERS
# ============================================================================

@app.get("/api/paper/account")
async def get_paper_account(user_id: str = Depends(get_current_user_id)) -> Dict[str, Any]:
    acct = auth.load_paper_account(user_id) if user_id and user_id != auth.ANONYMOUS_USER_ID else None
    if acct:
        return acct
    # Default starter account when the user has none yet.
    return {
        "user_id": user_id, "nav": 100000.0, "cash": 100000.0,
        "realized_pnl": 0.0, "unrealized_pnl": 0.0, "starting_nav": 100000.0,
    }


@app.get("/api/paper/positions")
async def get_paper_positions(user_id: str = Depends(get_current_user_id)) -> List[Dict[str, Any]]:
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return []
    return auth.list_paper_positions(user_id)


@app.get("/api/paper/orders")
async def get_paper_orders(
    limit: int = Query(50, ge=1, le=500),
    user_id: str = Depends(get_current_user_id),
) -> List[Dict[str, Any]]:
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return []
    return auth.list_paper_orders(user_id, limit=limit)


@app.get("/api/paper/calibration")
async def get_paper_calibration(user_id: str = Depends(get_current_user_id)) -> Dict[str, Any]:
    # Brier score lives in auth.py if you wire it; until then we no-op.
    return {"brier": None, "samples": 0}


@app.get("/api/paper/conviction")
async def get_paper_conviction(
    ticker: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    user_id: str = Depends(get_current_user_id),
) -> List[Dict[str, Any]]:
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return []
    return auth.list_conviction_scores(user_id, ticker=ticker, limit=limit)


@app.get("/api/paper/conviction/timeseries")
async def get_paper_conviction_timeseries(
    half_life_days: float = Query(5.0, ge=0.5, le=180),
    limit: int = Query(200, ge=1, le=1000),
    ticker: Optional[str] = Query(None),
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    if not user_id:
        return {"points": []}
    scores = auth.list_conviction_scores(user_id, ticker=ticker, limit=limit)
    # project_timeseries decays + sorts ascending by ts. It drops rows missing
    # score/recorded_at, so re-key the source rows by timestamp to carry their
    # fields (ticker, rating, …) through onto each decayed point.
    by_ts = {conviction_decay._coerce_dt(
                s.get("recorded_at") or s.get("created_at")): s
             for s in scores
             if (s.get("recorded_at") or s.get("created_at")) is not None}
    points = []
    for p in conviction_decay.project_timeseries(scores, half_life_days=half_life_days):
        src = by_ts.get(p.ts, {})
        points.append({
            **src,
            "ts": p.ts.isoformat(),
            "raw_score": p.raw_score,
            "decayed_score": p.decayed_score,
        })
    return {"points": points, "half_life_days": half_life_days}


@app.post("/api/paper/outcomes/resolve")
async def post_resolve_outcomes(
    limit: int = Query(200, ge=1, le=1000),
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, int]:
    # Stub — real resolver lives in runner.py / scheduler.py.
    return {"resolved": 0}


# ============================================================================
# RISK
# ============================================================================

@app.get("/api/risk/events")
async def get_risk_events(
    limit: int = Query(50, ge=1, le=500),
    user_id: str = Depends(get_current_user_id),
) -> List[Dict[str, Any]]:
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return []
    return auth.list_risk_events(user_id, limit=limit)


@app.get("/api/risk/limits")
async def get_risk_limits(user_id: str = Depends(get_current_user_id)) -> Dict[str, Any]:
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return auth._default_risk_limits_row(user_id)
    row = auth.load_risk_limits(user_id)
    return row or auth._default_risk_limits_row(user_id)


class RiskLimitsPayload(BaseModel):
    max_position_pct: Optional[float] = None
    max_daily_drawdown_pct: Optional[float] = None
    kelly_fraction_cap: Optional[float] = None
    max_slippage_bps: Optional[int] = None
    daily_spend_cap_usd: Optional[float] = None
    monthly_spend_cap_usd: Optional[float] = None
    behavioral_cooldown: Optional[bool] = None


@app.put("/api/risk/limits")
async def put_risk_limits(
    payload: RiskLimitsPayload,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        raise HTTPException(401, "Sign in required.")
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    return auth.upsert_risk_limits(user_id, **fields)


class KillSwitchPayload(BaseModel):
    enabled: bool


@app.post("/api/risk/kill-switch")
async def post_kill_switch(
    payload: KillSwitchPayload,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        raise HTTPException(401, "Sign in required.")
    return auth.upsert_risk_limits(user_id, kill_switch=payload.enabled)


# ============================================================================
# MULTI-TIMEFRAME ORCHESTRATION (Phase 3)
# ============================================================================

def _recipe_to_dict(recipe: Any) -> Dict[str, Any]:
    """Accept either a Recipe pydantic model or a plain dict."""
    if hasattr(recipe, "model_dump"):
        return recipe.model_dump(mode="json")
    return dict(recipe)


def _fire_one_session(recipe: Dict[str, Any], fire_id: str, timeframe: str,
                      *, skip_post_decision: bool, loop=None) -> Dict[str, Any]:
    """Build + run a single per-timeframe session via SessionRunner.

    SessionRunner is patched to a stub in tests; in prod it runs the real
    graph. The runner mutates the session dict in place (status, pm_decision)."""
    session = {
        "id": f"{recipe['id']}-{fire_id}-{timeframe}",
        "user_id": recipe.get("user_id"),
        "ticker": (recipe.get("tickers") or ["?"])[0],
        "timeframe": timeframe,
        "recipe_id": recipe["id"],
        "status": "pending",
    }
    if skip_post_decision:
        session["skip_post_decision"] = True
    runner = SessionRunner(session, loop)
    runner.start()
    auth.save_session(runner.session)
    return runner.session


def _run_recipe_multitf(recipe: Dict[str, Any], fire_id: str, loop=None) -> Dict[str, Any]:
    """Fan a recipe out across its timeframes, merge the per-TF PM decisions
    onto a lead session, and log a cross-timeframe disagreement row.

    - One session per timeframe; non-lead sessions carry skip_post_decision so
      only the merged decision triggers the downstream hook.
    - The lead (first timeframe) gets the per-TF map under multitf_decisions,
      keeps its own pm_decision as the merged decision, and has its
      skip_post_decision flag stripped so the hook runs once on the merge.
    - A disagreement_log row records whether the timeframes agreed.
    """
    timeframes = recipe.get("timeframes") or ["1d"]
    ticker = (recipe.get("tickers") or ["?"])[0]
    user_id = recipe.get("user_id")
    rid = recipe["id"]

    sessions: List[Dict[str, Any]] = [
        _fire_one_session(recipe, fire_id, tf, skip_post_decision=True, loop=loop)
        for tf in timeframes
    ]

    lead = sessions[0]
    decisions = {s["timeframe"]: s.get("pm_decision") for s in sessions}
    lead["multitf_decisions"] = decisions
    # The lead's own pm_decision stands as the merged decision; the hook should
    # now run against it, so drop the per-TF skip flag.
    lead.pop("skip_post_decision", None)
    auth.save_session(lead)

    # Agreement = every timeframe produced the same rating.
    ratings = [str((d or {}).get("rating", "")).lower() for d in decisions.values()]
    distinct = {r for r in ratings if r}
    agreement = len(distinct) <= 1
    auth.insert_disagreement_log({
        "session_id": f"{rid}-{fire_id}-lead",
        "user_id": user_id,
        "recipe_id": rid,
        "ticker": ticker,
        "kind": "multitf",
        "rating_agreement": agreement,
        "similarity": 1.0 if agreement else 0.0,
        "detail": {"ratings": dict(zip(timeframes, ratings))},
    })
    return {"lead": lead, "sessions": sessions}


def _run_recipe_session(recipe: Any, fire_id: str, loop=None) -> Dict[str, Any]:
    """Fire a recipe. Single-timeframe recipes use the direct path; recipes
    with 2+ timeframes fan out through the multi-TF orchestrator."""
    rd = _recipe_to_dict(recipe)
    timeframes = rd.get("timeframes") or ["1d"]
    if len(timeframes) > 1:
        return _run_recipe_multitf(rd, fire_id, loop=loop)
    session = _fire_one_session(
        rd, fire_id, timeframes[0], skip_post_decision=False, loop=loop)
    return {"lead": session, "sessions": [session]}


# ============================================================================
# RECIPES (recurring analyses)
# ============================================================================

@app.get("/api/recipes")
async def get_recipes(user_id: str = Depends(get_current_user_id)) -> List[Dict[str, Any]]:
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return []
    return auth.list_recipes(user_id)


class CreateRecipePayload(BaseModel):
    name: str
    tickers: List[str] = Field(default_factory=list)
    analysts: List[str] = Field(default_factory=list)
    llm_provider: str
    quick_model: str
    deep_model: str
    bull_model: Optional[str] = None
    bear_model: Optional[str] = None
    schedule_kind: str = "manual"
    schedule_expr: Optional[str] = None
    output_policy: str = "notify"
    conviction_threshold: int = 7
    max_daily_token_cost_usd: float = 5.0
    market_hours_only: bool = True
    # Optional: populated when a recipe is created from a compiled NL strategy.
    trigger_conditions: Optional[Dict[str, Any]] = None
    source_thesis: Optional[str] = None


@app.post("/api/recipes")
async def post_recipe(
    payload: CreateRecipePayload,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    import time as _time
    import uuid as _uuid
    from agenticwhales.recipes import HeterogeneityError, validate_heterogeneity

    if not user_id:
        raise HTTPException(401, "Sign in required to save recurring theses.")

    data = payload.model_dump()
    # Bull and Bear must come from different model families so the debate is a
    # genuine adversarial check, not correlated ensembling. Default each to the
    # deep/quick model when the client didn't specify a distinct pair.
    bull = (data.get("bull_model") or data.get("deep_model") or "").strip()
    bear = (data.get("bear_model") or data.get("quick_model") or "").strip()
    try:
        validate_heterogeneity(bull, bear)
    except HeterogeneityError as e:
        raise HTTPException(400, str(e))
    data["bull_model"] = bull
    data["bear_model"] = bear

    recipe = {
        "id": _uuid.uuid4().hex,
        "user_id": user_id,
        "status": "active",
        "created_at": auth._ts_iso(_time.time()),
        "last_run_at": None,
        **data,
    }
    auth.save_recipe(recipe)
    return recipe


@app.delete("/api/recipes/{rid}")
async def delete_recipe_route(rid: str, user_id: str = Depends(get_current_user_id)) -> Dict[str, Any]:
    r = auth.load_recipe(rid)
    if not r:
        raise HTTPException(404, "Recipe not found.")
    if r.get("user_id") != user_id:
        raise HTTPException(403, "Not your recipe.")
    return {"deleted": auth.delete_recipe(rid)}


@app.get("/api/recipes/{rid}/sessions")
async def get_recipe_sessions(
    rid: str, limit: int = Query(20, ge=1, le=200),
    user_id: str = Depends(get_current_user_id),
) -> List[Dict[str, Any]]:
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return []
    all_sessions = storage.list_all(user_id=user_id)
    matched = [s for s in all_sessions if s.get("recipe_id") == rid]
    matched.sort(key=lambda s: s.get("created_at", 0), reverse=True)
    return [_summary(s) for s in matched[:limit]]


@app.get("/api/recipes/{rid}/usage")
async def get_recipe_usage(rid: str, user_id: str = Depends(get_current_user_id)) -> Dict[str, Any]:
    from datetime import date
    today = date.today().isoformat()
    row = auth.load_recipe_usage(rid, today)
    return row or {"recipe_id": rid, "usage_date": today, "cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0}


@app.post("/api/recipes/{rid}/trigger-now")
async def trigger_recipe(rid: str, user_id: str = Depends(get_current_user_id)) -> Dict[str, Any]:
    r = auth.load_recipe(rid)
    if not r or r.get("user_id") != user_id:
        raise HTTPException(404, "Recipe not found.")
    # Real fire path lives in scheduler.py; this is a thin acknowledgement
    # so the UI's "Run now" button stops 404'ing.
    return {"triggered": True, "recipe_id": rid}


@app.post("/api/recipes/{rid}/pause")
async def pause_recipe(rid: str, user_id: str = Depends(get_current_user_id)) -> Dict[str, Any]:
    r = auth.load_recipe(rid)
    if not r or r.get("user_id") != user_id:
        raise HTTPException(404, "Recipe not found.")
    auth.update_recipe_status(rid, "paused")
    return {"status": "paused"}


@app.post("/api/recipes/{rid}/resume")
async def resume_recipe(rid: str, user_id: str = Depends(get_current_user_id)) -> Dict[str, Any]:
    r = auth.load_recipe(rid)
    if not r or r.get("user_id") != user_id:
        raise HTTPException(404, "Recipe not found.")
    auth.update_recipe_status(rid, "active")
    return {"status": "active"}


@app.post("/api/recipes/{rid}/kill")
async def kill_recipe(rid: str, user_id: str = Depends(get_current_user_id)) -> Dict[str, Any]:
    r = auth.load_recipe(rid)
    if not r or r.get("user_id") != user_id:
        raise HTTPException(404, "Recipe not found.")
    auth.update_recipe_status(rid, "killed")
    return {"status": "killed"}


# ============================================================================
# JOURNAL
# ============================================================================

@app.get("/api/journal/entries")
async def get_journal_entries(
    limit: int = Query(100, ge=1, le=500),
    include_drafts: bool = Query(True),
    user_id: str = Depends(get_current_user_id),
) -> List[Dict[str, Any]]:
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        return []
    return auth.list_journal_entries(user_id, limit=limit, include_drafts=include_drafts)


class JournalEntryPayload(BaseModel):
    body: str = Field(min_length=1)
    kind: str = "note"
    session_id: Optional[str] = None
    is_draft: bool = False


@app.post("/api/journal/entries")
async def post_journal_entry(
    payload: JournalEntryPayload,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    import time as _time
    import uuid as _uuid
    if not user_id or user_id == auth.ANONYMOUS_USER_ID:
        raise HTTPException(401, "Sign in required.")
    row = {
        "id": _uuid.uuid4().hex, "user_id": user_id,
        "body": payload.body, "kind": payload.kind,
        "session_id": payload.session_id, "is_draft": payload.is_draft,
        "created_at": auth._ts_iso(_time.time()),
    }
    auth.save_journal_entry(row)
    return row


@app.put("/api/journal/entries/{eid}")
async def put_journal_entry(
    eid: str, payload: JournalEntryPayload,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    existing = auth.load_journal_entry(eid)
    if not existing or existing.get("user_id") != user_id:
        raise HTTPException(404, "Entry not found.")
    existing.update({
        "body": payload.body, "kind": payload.kind,
        "session_id": payload.session_id, "is_draft": payload.is_draft,
    })
    auth.save_journal_entry(existing)
    return existing


@app.delete("/api/journal/entries/{eid}")
async def delete_journal_entry_route(eid: str, user_id: str = Depends(get_current_user_id)) -> Dict[str, Any]:
    existing = auth.load_journal_entry(eid)
    if not existing or existing.get("user_id") != user_id:
        raise HTTPException(404, "Entry not found.")
    return {"deleted": auth.delete_journal_entry(eid)}


# Ask-the-fund templates: minimal stub list so the UI's buttons render. Real
# answers would query the corpus. Frontend expects a bare array with
# `template_id` + `question` keys, not a `{templates: [...]}` envelope.
_ASK_TEMPLATES = [
    {"template_id": "best_call",    "question": "What was my best call?"},
    {"template_id": "worst_miss",   "question": "What was my worst miss?"},
    {"template_id": "lessons",      "question": "Top 3 lessons from my override reasons"},
    {"template_id": "rating_drift", "question": "How has my rating distribution drifted?"},
]


@app.get("/api/journal/ask/templates")
async def get_ask_templates() -> List[Dict[str, Any]]:
    return _ASK_TEMPLATES


class AskPayload(BaseModel):
    template_id: str


@app.post("/api/journal/ask")
async def post_ask(payload: AskPayload, user_id: str = Depends(get_current_user_id)) -> Dict[str, Any]:
    tpl = next((t for t in _ASK_TEMPLATES if t["template_id"] == payload.template_id), None)
    if not tpl:
        raise HTTPException(404, "Template not found.")
    return {
        "question": tpl["question"],
        "markdown": "_Not enough data yet — run more analyses and resolve outcomes to populate this answer._",
        "confidence": "low",
        "data_points": 0,
    }


# ============================================================================
# BEHAVIORAL FINDINGS
# ============================================================================

@app.get("/api/behavioral/findings")
async def get_behavioral_findings(
    limit: int = Query(20, ge=1, le=200),
    user_id: str = Depends(get_current_user_id),
) -> List[Dict[str, Any]]:
    # Real implementation walks the user's session history for patterns.
    return []


class BehavioralUpdatePayload(BaseModel):
    pattern: str
    created_at: str
    action: str  # "dismiss" | "acknowledge"


@app.post("/api/behavioral/findings/update")
async def post_behavioral_update(
    payload: BehavioralUpdatePayload,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    return {"ok": True, "action": payload.action}


@app.post("/api/behavioral/scan")
async def post_behavioral_scan(user_id: str = Depends(get_current_user_id)) -> Dict[str, Any]:
    return {"new_findings": 0}


# ============================================================================
# STREAMING + DISAGREEMENT + PROMPT EVALS
# ============================================================================

@app.get("/api/streaming/events")
async def get_streaming_events(
    limit: int = Query(20, ge=1, le=200),
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    if not user_id:
        return {"events": []}
    rows = auth.list_audit(
        action="streaming.fire", target_user_id=user_id, limit=limit)
    events = []
    for r in rows:
        md = r.get("metadata") or {}
        events.append({
            "recipe_id": md.get("recipe_id"),
            "symbol": md.get("symbol"),
            "reason": md.get("reason"),
            "fire_id": md.get("fire_id"),
            "ts": r.get("ts"),
        })
    return {"events": events}


@app.get("/api/disagreement")
async def get_disagreement(
    limit: int = Query(50, ge=1, le=500),
    user_id: str = Depends(get_current_user_id),
) -> List[Dict[str, Any]]:
    return []


@app.get("/api/prompt-evals")
async def get_prompt_evals(
    limit: int = Query(50, ge=1, le=500),
    user_id: str = Depends(get_current_user_id),
) -> List[Dict[str, Any]]:
    return []


# ============================================================================
# CALIBRATION
# ============================================================================

@app.get("/api/calibration")
async def get_calibration(user_id: str = Depends(get_current_user_id)) -> Dict[str, Any]:
    return {"available": False, "brier": None, "n_samples": 0}


class CalibrationOptInPayload(BaseModel):
    apply: bool
    regime: str = "all"


@app.post("/api/calibration/opt-in")
async def post_calibration_opt_in(
    payload: CalibrationOptInPayload,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    return {"applied": payload.apply, "regime": payload.regime}


@app.post("/api/calibration/fit")
async def post_calibration_fit(user_id: str = Depends(get_current_user_id)) -> Dict[str, Any]:
    return {"fitted": False, "reason": "not_enough_samples"}


# ============================================================================
# ABLATION / EXPLAIN
# ============================================================================

@app.get("/api/sessions/{sid}/ablation")
async def get_session_ablation(sid: str, user_id: str = Depends(get_current_user_id)) -> Dict[str, Any]:
    s = storage.load(sid)
    if not s or s.get("user_id") != user_id:
        raise HTTPException(404, "Session not found.")
    # Real ablation lives in agenticwhales.ablation; this is a no-op until wired.
    return {"session_id": sid, "method": "(ablation not yet wired in slim server)", "contributions": []}


# ============================================================================
# BACKTEST
# ============================================================================

class BacktestPayload(BaseModel):
    ticker: str
    from_date: str
    to_date: str
    starting_cash: float = 100000.0
    kelly_cap: float = 0.10


def _run_backtest_sync(symbol, from_date, to_date, starting_cash, kelly_cap, decision_fn=None):
    """Run the real replay engine and shape the result for the UI. Raises
    HTTPException(422) on data problems (bad ticker, empty window)."""
    from agenticwhales.backtest import run_backtest

    try:
        result = run_backtest(
            symbol, from_date, to_date,
            decision_fn=decision_fn,
            starting_cash=starting_cash,
            kelly_cap=kelly_cap,
        )
    except Exception as e:  # noqa: BLE001 — yfinance / empty-window / look-ahead
        raise HTTPException(status_code=422, detail=f"Backtest failed: {e}")

    total_return_pct = (
        (result.final_nav - result.starting_cash) / result.starting_cash * 100.0
        if result.starting_cash else 0.0
    )
    return {
        "symbol": result.symbol,
        "from_date": result.from_date.isoformat(),
        "to_date": result.to_date.isoformat(),
        "starting_cash": result.starting_cash,
        "final_nav": round(result.final_nav, 2),
        "total_return_pct": round(total_return_pct, 2),
        "total_decisions": result.total_decisions,
        "closed_trades": result.closed_trades,
        "hit_rate": result.hit_rate,
        "brier": result.brier,
        "max_drawdown_pct": round(result.max_drawdown_pct * 100.0, 2),
        "equity_curve": result.equity_curve,
        "trades": result.trades,
    }


@app.post("/api/backtest/run")
async def post_backtest_run(
    payload: BacktestPayload,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """Run the deterministic momentum-stub backtest on real OHLCV history."""
    return await asyncio.to_thread(
        _run_backtest_sync,
        payload.ticker, payload.from_date, payload.to_date,
        payload.starting_cash, payload.kelly_cap,
    )


class StrategyBacktestPayload(BaseModel):
    thesis: str = Field(min_length=3, max_length=1000)
    ticker: str = Field(min_length=1, max_length=12)
    from_date: str
    to_date: str
    starting_cash: float = 100000.0
    kelly_cap: float = 0.10


@app.post("/api/strategy/backtest")
async def post_strategy_backtest(
    payload: StrategyBacktestPayload,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """Compile a plain-English thesis into a strategy, then backtest it on real
    historical market data. Returns the compiled rules + backtest metrics so the
    Strategy Lab can show both what it understood and how it would have done."""
    from agenticwhales.strategy import compile_strategy, strategy_decision_generator

    def _work():
        spec = compile_strategy(
            payload.thesis, provider=DEFAULT_PROVIDER, model=DEFAULT_QUICK_MODEL,
        )
        gen = strategy_decision_generator(spec)
        bt = _run_backtest_sync(
            payload.ticker, payload.from_date, payload.to_date,
            payload.starting_cash, payload.kelly_cap, decision_fn=gen,
        )
        return {"strategy": spec.to_dict(), "backtest": bt}

    try:
        return await asyncio.to_thread(_work)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — compile failure
        raise HTTPException(status_code=422, detail=f"Strategy compile failed: {e}")


# ============================================================================
# SIGNALS — external-data analyzers surfaced on /fund as sidebar tabs.
# Read-only, disclosure/analysis only. Never submit orders.
# ============================================================================


class XRecsPayload(BaseModel):
    handle: str = Field(min_length=1, max_length=64)
    max_results: int = Field(default=30, ge=5, le=100)


@app.post("/api/signals/x-recs")
async def post_x_recs(
    payload: XRecsPayload,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """Fetch an X handle's recent tweets and extract structured trade recs."""
    from agenticwhales.dataflows.x_trades import (
        XTradesError,
        extract_trade_recs,
        fetch_user_tweets,
    )

    handle = payload.handle.strip().lstrip("@")
    try:
        tweets = fetch_user_tweets(handle, max_results=payload.max_results)
    except XTradesError as e:
        raise HTTPException(status_code=502, detail=str(e))
    if not tweets:
        return {"handle": handle, "tweets": [], "recommendations": []}
    try:
        recs = extract_trade_recs(
            handle, tweets,
            provider=DEFAULT_PROVIDER, model=DEFAULT_QUICK_MODEL,
        )
    except XTradesError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"handle": handle, "tweets": tweets, "recommendations": recs}


class CongressPayload(BaseModel):
    ticker: str = Field(min_length=1, max_length=10)
    limit: int = Field(default=50, ge=1, le=200)


@app.post("/api/signals/congress")
async def post_congress(
    payload: CongressPayload,
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """Fetch disclosed U.S. Congress trades for a ticker."""
    from agenticwhales.dataflows.congress_trades import (
        CongressTradesError,
        fetch_congress_trades,
    )

    ticker = payload.ticker.strip().upper()
    try:
        records = fetch_congress_trades(ticker, limit=payload.limit)
    except CongressTradesError as e:
        raise HTTPException(status_code=502, detail=str(e))
    buys = sum(
        1 for r in records
        if "buy" in r["transaction"].lower() or "purchase" in r["transaction"].lower()
    )
    sells = sum(
        1 for r in records
        if "sell" in r["transaction"].lower() or "sale" in r["transaction"].lower()
    )
    return {"ticker": ticker, "trades": records, "buys": buys, "sells": sells}


@app.post("/api/signals/transactions")
async def post_transactions(
    file: UploadFile = File(...),
    run_llm: str = Form("false"),
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """Parse a brokerage CSV and run the FIFO/behavioral analyzer.

    ``run_llm=true`` adds the 4-lens LLM behavioral review (slower, uses
    provider quota); otherwise only the deterministic metrics + flags run.
    """
    from agenticwhales.transactions import analyze_transactions, parse_transactions_csv

    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="replace")
    txns = parse_transactions_csv(text)
    if not txns:
        raise HTTPException(
            status_code=400,
            detail="No transactions parsed from the uploaded file.",
        )
    do_llm = str(run_llm).strip().lower() in ("1", "true", "yes", "on")
    result = analyze_transactions(
        txns, run_llm=do_llm,
        provider=DEFAULT_PROVIDER, model=DEFAULT_QUICK_MODEL,
    )
    payload = result.model_dump()

    # Persist for the resolved user so the cognitive journal can analyze
    # behaviour across sessions. In local/unconfigured dev this is the shared
    # "anonymous" bucket; with Supabase configured it's the real uid.
    saved = 0
    if user_id:
        import time as _time
        import uuid as _uuid
        batch_id = _uuid.uuid4().hex
        now = auth._ts_iso(_time.time())
        rows = []
        for t in txns:
            rows.append({
                "id": _uuid.uuid4().hex,
                "user_id": user_id,
                "batch_id": batch_id,
                "source": "csv_upload",
                "txn_date": t.date or "",
                "type": t.type or "Other",
                "symbol": t.symbol or "",
                "description": t.description or "",
                "quantity": float(t.quantity or 0),
                "price": float(t.price or 0),
                "amount": float(t.amount or 0),
                "created_at": now,
            })
        try:
            saved = auth.save_transactions(rows)
        except Exception as exc:  # noqa: BLE001
            payload.setdefault("warnings", []).append(f"Could not save to history: {exc}")
        payload["batch_id"] = batch_id
    payload["saved_count"] = saved
    payload["persisted"] = saved > 0
    return payload


@app.get("/api/transactions")
async def get_transactions(
    limit: int = Query(2000, ge=1, le=5000),
    user_id: str = Depends(get_current_user_id),
) -> List[Dict[str, Any]]:
    """The resolved user's saved brokerage transaction history."""
    if not user_id:
        return []
    return auth.list_transactions(user_id, limit=limit)


@app.get("/api/transactions/metrics")
async def get_transactions_metrics(
    user_id: str = Depends(get_current_user_id),
) -> Dict[str, Any]:
    """Recompute deterministic metrics over the user's full saved history."""
    if not user_id:
        return {"transactions": [], "metrics": {}, "analysis": {}, "warnings": []}
    from agenticwhales.transactions import analyze_transactions
    from agenticwhales.transactions.models import Transaction

    saved = auth.list_transactions(user_id)
    if not saved:
        return {"transactions": [], "metrics": {}, "analysis": {}, "warnings": [], "count": 0}
    txns = [
        Transaction(
            date=r.get("txn_date", ""), type=r.get("type", "Other"),
            symbol=r.get("symbol", ""), description=r.get("description", ""),
            quantity=r.get("quantity", 0), price=r.get("price", 0), amount=r.get("amount", 0),
        )
        for r in saved
    ]
    result = analyze_transactions(txns, run_llm=False)
    payload = result.model_dump()
    payload["count"] = len(txns)
    return payload


def main() -> None:
    """`python -m web` entrypoint."""
    import os
    import uvicorn

    host = os.getenv("AGENTICWHALES_WEB_HOST") or os.getenv("TRADINGAGENTS_WEB_HOST", "0.0.0.0")
    port = int(os.getenv("AGENTICWHALES_WEB_PORT") or os.getenv("TRADINGAGENTS_WEB_PORT", "8080"))
    uvicorn.run("web.server:app", host=host, port=port, reload=False)
