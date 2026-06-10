"""LLM-based extraction of transactions from raw text.

Port of robinhood-analyzer/lib/extract.ts. The original chunked the input,
extracted each chunk in parallel, retried failures, and de-duplicated. We
keep the same algorithm but route the LLM call through
``agenticwhales.llm_clients`` (the project invariant) instead of raw fetch.

The extraction system prompt is reused verbatim from extract.ts.
"""

from __future__ import annotations

import base64
import json
import os
import re
from typing import Callable, List, Optional, Tuple

from agenticwhales.llm_clients import create_llm_client

from .models import Transaction
from .parser import chunk_text, dedupe, normalize_transaction

# Reused verbatim from extract.ts EXTRACTION_SYSTEM.
EXTRACTION_SYSTEM = """You are a meticulous financial-document parser. You convert raw text extracted from a Robinhood brokerage statement or transaction-history PDF into clean, structured JSON.

Rules:
- Output ONLY a JSON object: {"transactions": [ ... ]}.
- Each transaction has: date (yyyy-mm-dd if you can parse it, else the raw date string), type, symbol, description, quantity (number), price (number), amount (number).
- "type" should be one of: Buy, Sell, Dividend, Deposit, Withdrawal, Interest, Fee, Option Buy, Option Sell, Transfer, Other.
- "amount" is the signed cash flow from the account holder's perspective: money LEAVING the account (buys, withdrawals, fees) is NEGATIVE; money ENTERING (sells, dividends, deposits, interest) is POSITIVE.
- symbol is the ticker in uppercase, or "" for pure cash events.
- quantity and price are 0 when not applicable.
- Do not invent transactions. Only include rows that clearly represent an activity/transaction. Ignore headers, totals, page numbers, disclosures, and balances.
- If the document contains no recognizable transactions, return {"transactions": []}."""

# Per-chunk input size (chars). Mirrors CHUNK_CHARS.
CHUNK_CHARS = 5000
MAX_CHUNK_ATTEMPTS = 3


def parse_json_loose(text: str) -> object:
    """Extract a JSON object/array from a model response that may include
    prose or code fences. Port of parseJsonLoose() in llm.ts."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start = None
    for i, ch in enumerate(cleaned):
        if ch in "[{":
            start = i
            break
    if start is None:
        raise ValueError("No JSON found in model response")
    open_ch = cleaned[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    for i in range(start, len(cleaned)):
        if cleaned[i] == open_ch:
            depth += 1
        elif cleaned[i] == close_ch:
            depth -= 1
            if depth == 0:
                return json.loads(cleaned[start : i + 1])
    raise ValueError("Unbalanced JSON in model response")


def _invoke_llm(llm, system: str, user, on_usage: Optional[Callable[[int, int], None]] = None) -> str:
    """Invoke a LangChain chat model with a system + user message and return text.

    `user` may be a plain string or a multimodal content-block list (the
    LangChain shape all first-party clients accept). `on_usage(in, out)` is
    called with the response's token counts when available — the hook real
    spend accounting hangs off."""
    from langchain_core.messages import HumanMessage, SystemMessage

    resp = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
    if on_usage is not None:
        usage = getattr(resp, "usage_metadata", None) or {}
        try:
            on_usage(int(usage.get("input_tokens", 0) or 0),
                     int(usage.get("output_tokens", 0) or 0))
        except Exception:  # noqa: BLE001 — accounting must never break extraction
            pass
    content = getattr(resp, "content", resp)
    if isinstance(content, list):
        parts = [
            c.get("text", "") if isinstance(c, dict) else str(c)
            for c in content
        ]
        content = "\n".join(p for p in parts if p)
    text = str(content).strip()
    if not text:
        raise ValueError("Model returned an empty response.")
    return text


def _rows_to_transactions(raw: str) -> List[Transaction]:
    parsed = parse_json_loose(raw)
    rows = parsed.get("transactions", []) if isinstance(parsed, dict) else []
    if not isinstance(rows, list):
        rows = []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        t = normalize_transaction(r)
        if t.type or t.symbol or t.amount != 0:
            out.append(t)
    return out


def _extract_chunk(llm, text: str, on_usage=None) -> List[Transaction]:
    user = f'Extract all transactions from this Robinhood document text:\n\n"""\n{text}\n"""'
    raw = _invoke_llm(llm, EXTRACTION_SYSTEM, user, on_usage)
    return _rows_to_transactions(raw)


def _extract_chunk_with_retry(llm, text: str, on_usage=None) -> List[Transaction]:
    last_err: Optional[Exception] = None
    for attempt in range(1, MAX_CHUNK_ATTEMPTS + 1):
        try:
            return _extract_chunk(llm, text, on_usage)
        except Exception as e:  # noqa: BLE001 - retry any transient failure
            last_err = e
    raise last_err if last_err else RuntimeError("extraction failed")


# --------------------------------------------------------------------------- #
# Vision path — screenshots / photos of trade history, no OCR server needed.
# --------------------------------------------------------------------------- #

# Explicit opt-in via env: key PRESENCE is deliberately not the gate, because
# test environments set placeholder provider keys (conftest.py) and an
# implicit default-on would fire real network calls in CI.
_VISION_DEFAULT_MODELS = {
    "google": "gemini-3-flash-preview",
    "openai": "gpt-5.4-mini",
    "anthropic": "claude-haiku-4-5-20251001",
}


def vision_config() -> Optional[Tuple[str, str]]:
    """(provider, model) for screenshot extraction, or None when not enabled.
    Enabled ONLY by setting AGENTICWHALES_VISION_PROVIDER explicitly."""
    provider = (os.getenv("AGENTICWHALES_VISION_PROVIDER") or "").strip().lower()
    if not provider:
        return None
    model = (os.getenv("AGENTICWHALES_VISION_MODEL") or "").strip() \
        or _VISION_DEFAULT_MODELS.get(provider, "")
    if not model:
        return None
    return provider, model


_VISION_USER_TEXT = (
    "Extract all transactions from this brokerage screenshot or statement "
    "image. Apply the system rules exactly; output only the JSON object."
)


def _extract_image(llm, image: bytes, mime: str, on_usage=None) -> List[Transaction]:
    b64 = base64.b64encode(image).decode("ascii")
    user = [
        {"type": "text", "text": _VISION_USER_TEXT},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
    ]
    raw = _invoke_llm(llm, EXTRACTION_SYSTEM, user, on_usage)
    return _rows_to_transactions(raw)


def extract_transactions_from_image(
    images: List[bytes],
    mime_types: Optional[List[str]] = None,
    *,
    llm=None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    on_warn: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
    on_usage: Optional[Callable[[int, int], None]] = None,
) -> List[Transaction]:
    """Extract transactions directly from screenshot/photo bytes via a vision
    model. One call per image (per-image retry, like text chunks); results are
    de-duplicated across images so an overlapping double-screenshot can't
    double-count a trade. Raises RuntimeError when vision isn't enabled and no
    explicit llm/provider was given."""
    if llm is None:
        if provider is None or model is None:
            cfg = vision_config()
            if cfg is None:
                raise RuntimeError(
                    "vision extraction not enabled (set AGENTICWHALES_VISION_PROVIDER)")
            provider, model = cfg
        llm = create_llm_client(provider=provider, model=model).get_llm()

    mimes = mime_types or ["image/png"] * len(images)
    n = len(images)
    flat: List[Transaction] = []
    failed = 0
    for i, (img, mime) in enumerate(zip(images, mimes), 1):
        last_err: Optional[Exception] = None
        for _ in range(MAX_CHUNK_ATTEMPTS):
            try:
                flat.extend(_extract_image(llm, img, mime or "image/png", on_usage))
                last_err = None
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
        if last_err is not None:
            failed += 1
            if on_warn:
                on_warn(f"Could not read image {i} of {n}: {last_err}")
        if on_progress:
            on_progress(i, n)
    if failed == n and n > 0:
        raise RuntimeError("could not read any of the supplied images")
    return dedupe(flat)


def extract_transactions(
    raw_text: str,
    *,
    llm=None,
    provider: str = "openai",
    model: str = "gpt-5.4-mini",
    base_url: Optional[str] = None,
    on_warn: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
    on_usage: Optional[Callable[[int, int], None]] = None,
    concurrency: int = 1,
    chunk_chars: int = CHUNK_CHARS,
) -> List[Transaction]:
    """Extract a de-duplicated transaction list from raw document text.

    The LLM is injectable via ``llm`` (a LangChain chat model) so tests can
    pass a fake and never touch the network. When ``llm`` is None a client is
    built from ``provider``/``model`` via the standard factory.

    ``on_progress(done, total)`` is called after each chunk so long documents can
    report extraction progress to a UI.

    Unlike the TS original we extract chunks sequentially (no asyncio
    requirement); failures are retried per chunk and, as a last resort,
    skipped with a warning so one bad section can't abort the whole job.
    """
    if llm is None:
        llm = create_llm_client(provider=provider, model=model, base_url=base_url).get_llm()

    chunks = chunk_text(raw_text, chunk_chars)
    n = len(chunks)
    lists: List[List[Transaction]] = [[] for _ in range(n)]
    failed = 0
    completed = 0

    if concurrency and concurrency > 1 and n > 1:
        # Chunks are independent — extract them in parallel to cut wall-clock on
        # large documents (LangChain chat clients are safe across threads).
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=min(concurrency, n)) as ex:
            futs = {ex.submit(_extract_chunk_with_retry, llm, chunks[i], on_usage): i
                    for i in range(n)}
            for fut in as_completed(futs):
                i = futs[fut]
                try:
                    lists[i] = fut.result()
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    if on_warn:
                        on_warn(f"Could not parse section {i + 1} of {n}: {e}")
                completed += 1
                if on_progress:
                    on_progress(completed, n)
    else:
        for i in range(n):
            try:
                lists[i] = _extract_chunk_with_retry(llm, chunks[i], on_usage)
            except Exception as e:  # noqa: BLE001
                failed += 1
                if on_warn:
                    on_warn(f"Could not parse section {i + 1} of {n}: {e}")
            if on_progress:
                on_progress(i + 1, n)

    if failed > 0 and on_warn:
        on_warn(
            f"{failed} of {n} document sections failed to parse — "
            f"some transactions may be missing."
        )

    flat = [t for sub in lists for t in sub]
    return dedupe(flat)
