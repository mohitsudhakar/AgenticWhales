"""LLM-based extraction of transactions from raw text.

Port of robinhood-analyzer/lib/extract.ts. The original chunked the input,
extracted each chunk in parallel, retried failures, and de-duplicated. We
keep the same algorithm but route the LLM call through
``agenticwhales.llm_clients`` (the project invariant) instead of raw fetch.

The extraction system prompt is reused verbatim from extract.ts.
"""

from __future__ import annotations

import json
import re
from typing import Callable, List, Optional

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


def _invoke_llm(llm, system: str, user: str) -> str:
    """Invoke a LangChain chat model with a system + user message and return text."""
    from langchain_core.messages import HumanMessage, SystemMessage

    resp = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
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


def _extract_chunk(llm, text: str) -> List[Transaction]:
    user = f'Extract all transactions from this Robinhood document text:\n\n"""\n{text}\n"""'
    raw = _invoke_llm(llm, EXTRACTION_SYSTEM, user)
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


def _extract_chunk_with_retry(llm, text: str) -> List[Transaction]:
    last_err: Optional[Exception] = None
    for attempt in range(1, MAX_CHUNK_ATTEMPTS + 1):
        try:
            return _extract_chunk(llm, text)
        except Exception as e:  # noqa: BLE001 - retry any transient failure
            last_err = e
    raise last_err if last_err else RuntimeError("extraction failed")


def extract_transactions(
    raw_text: str,
    *,
    llm=None,
    provider: str = "openai",
    model: str = "gpt-5.4-mini",
    base_url: Optional[str] = None,
    on_warn: Optional[Callable[[str], None]] = None,
) -> List[Transaction]:
    """Extract a de-duplicated transaction list from raw document text.

    The LLM is injectable via ``llm`` (a LangChain chat model) so tests can
    pass a fake and never touch the network. When ``llm`` is None a client is
    built from ``provider``/``model`` via the standard factory.

    Unlike the TS original we extract chunks sequentially (no asyncio
    requirement); failures are retried per chunk and, as a last resort,
    skipped with a warning so one bad section can't abort the whole job.
    """
    if llm is None:
        llm = create_llm_client(provider=provider, model=model, base_url=base_url).get_llm()

    chunks = chunk_text(raw_text, CHUNK_CHARS)
    lists: List[List[Transaction]] = []
    failed = 0
    for i, chunk in enumerate(chunks):
        try:
            lists.append(_extract_chunk_with_retry(llm, chunk))
        except Exception as e:  # noqa: BLE001
            failed += 1
            if on_warn:
                on_warn(f"Could not parse section {i + 1} of {len(chunks)}: {e}")
            lists.append([])

    if failed > 0 and on_warn:
        on_warn(
            f"{failed} of {len(chunks)} document sections failed to parse — "
            f"some transactions may be missing."
        )

    flat = [t for sub in lists for t in sub]
    return dedupe(flat)
