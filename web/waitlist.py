"""Waitlist signup handling.

Durable storage is the app's normal dual-mode store (Postgres when Supabase is
configured, in-memory otherwise) — see ``web.auth`` — so signups are captured
with **no new credentials**, and you can browse/export them from Supabase Studio
(a spreadsheet view + CSV download, i.e. "something like a Google Sheet").

An OPTIONAL live Google Sheet mirror is supported via a Google Apps Script Web
App: set ``WAITLIST_SHEET_WEBHOOK_URL`` to the deployed script URL and each
signup is POSTed to it (best-effort; a failure never blocks the signup). This
avoids managing a Google service-account JSON on the server.

Apps Script (paste into Extensions → Apps Script on your Sheet, Deploy → Web
app, execute as you, access "Anyone"):

    function doPost(e) {
      var sheet = SpreadsheetApp.getActiveSheet();
      var d = JSON.parse(e.postData.contents);
      sheet.appendRow([new Date(), d.email, d.name, d.company, d.note, d.source]);
      return ContentService.createTextOutput("ok");
    }
"""

from __future__ import annotations

import json as _json
import os
import re
import time
import uuid
from typing import Any, Dict, Optional

from . import auth

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Field length caps — defensive, keep junk/abuse out of storage + the Sheet.
_MAX_EMAIL = 254
_MAX_NAME = 120
_MAX_COMPANY = 160
_MAX_NOTE = 1000


def _clean(value: Optional[str], cap: int) -> str:
    return (value or "").strip()[:cap]


def is_valid_email(email: str) -> bool:
    e = (email or "").strip()
    return bool(e) and len(e) <= _MAX_EMAIL and _EMAIL_RE.match(e) is not None


def add_signup(
    *,
    email: str,
    name: str = "",
    company: str = "",
    note: str = "",
    source: str = "landing",
    when: Optional[float] = None,
) -> Dict[str, Any]:
    """Validate + persist a waitlist signup. Idempotent on email (re-signing
    updates the existing row, preserving the original created_at). Returns the
    stored row. Raises ValueError on an invalid email."""
    email_norm = (email or "").strip().lower()
    if not is_valid_email(email_norm):
        raise ValueError("A valid email is required.")

    existing = auth.get_waitlist_signup(email_norm)
    now_iso = auth._ts_iso(when if when is not None else time.time())

    row = {
        "id": existing.get("id") if existing else uuid.uuid4().hex,
        "email": email_norm,
        "name": _clean(name, _MAX_NAME),
        "company": _clean(company, _MAX_COMPANY),
        "note": _clean(note, _MAX_NOTE),
        "source": _clean(source, 60) or "landing",
        # Preserve the first-seen timestamp on re-signup; always refresh updated_at.
        "created_at": (existing.get("created_at") if existing else now_iso) or now_iso,
        "updated_at": now_iso,
    }
    auth.save_waitlist_signup(row)
    _mirror_to_sheet(row)
    return row


def _mirror_to_sheet(row: Dict[str, Any]) -> None:
    """Best-effort POST to the optional Apps Script webhook. Never raises."""
    url = os.getenv("WAITLIST_SHEET_WEBHOOK_URL")
    if not url:
        return
    try:
        import requests
        requests.post(
            url,
            data=_json.dumps({
                "email": row["email"],
                "name": row.get("name", ""),
                "company": row.get("company", ""),
                "note": row.get("note", ""),
                "source": row.get("source", ""),
                "created_at": row.get("created_at", ""),
            }),
            headers={"Content-Type": "application/json"},
            timeout=6,
        )
    except Exception:
        # The durable store already has the signup; the Sheet is a convenience
        # mirror. A transient webhook failure must not fail the user's signup.
        pass


def to_csv(rows) -> str:
    """Render signups as CSV text for the admin export."""
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["created_at", "email", "name", "company", "note", "source"])
    for r in rows:
        w.writerow([
            r.get("created_at", ""), r.get("email", ""), r.get("name", ""),
            r.get("company", ""), r.get("note", ""), r.get("source", ""),
        ])
    return buf.getvalue()


# --- public-facing vanity counter -------------------------------------------
#
# The landing page shows social proof, not the raw signup count:
#   - always at least 100 ("100+ already joined") for early credibility, and
#   - once real signups cross DOUBLE_THRESHOLD, we display 2x the real number.
# This is a marketing display value only; the admin export + DB always hold the
# true figure.

DISPLAY_FLOOR = 100
DOUBLE_THRESHOLD = 50


def display_count(real_count: int) -> int:
    """Map the true signup count to the number shown in the UI.

    - below the doubling threshold: floored at DISPLAY_FLOOR (so it reads
      "100+" while the list is still small).
    - at/above the threshold: 2x the real count, still never below the floor.
    """
    n = max(0, int(real_count))
    shown = n * 2 if n >= DOUBLE_THRESHOLD else n
    return max(DISPLAY_FLOOR, shown)
