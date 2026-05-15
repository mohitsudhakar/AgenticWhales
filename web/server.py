"""FastAPI app — REST + WebSocket front for the analysis runner."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agenticwhales import portfolio
from agenticwhales.llm_clients.model_catalog import MODEL_OPTIONS
from agenticwhales.universe import universe_for_api

from . import auth, batch_storage, storage
from .auth import authenticate_websocket, get_current_user_id
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Sessions and batches now live in Supabase Postgres (or the in-memory
    # fallback when Supabase isn't configured) — nothing to set up on disk.
    yield


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


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    config_tag = _supabase_runtime_config_tag()
    if config_tag:
        # Inject before </head> so the classic script runs synchronously,
        # which guarantees the global is set before supabase-client.js (which
        # is a deferred ES module) evaluates.
        html = html.replace("</head>", config_tag + "</head>", 1)
    return HTMLResponse(html)


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
    return {
        "id": s["id"],
        "ticker": s["ticker"],
        "analysis_date": s["analysis_date"],
        "status": s["status"],
        "created_at": s["created_at"],
        "completed_at": s.get("completed_at"),
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


def main() -> None:
    """`python -m web` entrypoint."""
    import os
    import uvicorn

    host = os.getenv("AGENTICWHALES_WEB_HOST") or os.getenv("TRADINGAGENTS_WEB_HOST", "0.0.0.0")
    port = int(os.getenv("AGENTICWHALES_WEB_PORT") or os.getenv("TRADINGAGENTS_WEB_PORT", "8080"))
    uvicorn.run("web.server:app", host=host, port=port, reload=False)
