"""Outbound email via Resend — env-gated, compliance baked in, never raises.

Enabled ONLY when both RESEND_API_KEY and AGENTICWHALES_EMAIL_FROM are set
(no accidental sends from onboarding@resend.dev). Every email this module
sends carries, at the module level (not per-caller):

- a List-Unsubscribe header AND RFC 8058 one-click (List-Unsubscribe-Post),
  required by Gmail/Yahoo bulk-sender rules;
- a footer with the sender identity, a postal address (CAN-SPAM), and the
  visible unsubscribe link.

Content rule (compliance review): emails are the product's weakest security
boundary — bodies carry counts, streaks, and deep links, never dollar figures
or trade details.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

_RESEND_URL = "https://api.resend.com/emails"


def is_configured() -> bool:
    return bool(os.getenv("RESEND_API_KEY")) and bool(os.getenv("AGENTICWHALES_EMAIL_FROM"))


def _postal() -> str:
    return os.getenv("AGENTICWHALES_EMAIL_POSTAL",
                     "AgenticWhales · address on file with the operator")


def send_email(to: str, subject: str, html: str, *,
               unsubscribe_url: Optional[str] = None,
               text: Optional[str] = None) -> bool:
    """Send one email. Returns False (and logs) on any failure or when email
    is unconfigured — callers always degrade to in-app."""
    if not is_configured():
        log.info("email disabled (RESEND_API_KEY/AGENTICWHALES_EMAIL_FROM unset); "
                 "skipped %r to %s", subject, to)
        return False
    footer = (
        '<hr style="border:none;border-top:1px solid #E7E0D2;margin:24px 0">'
        f'<p style="font-size:12px;color:#6B6657">Sent by AgenticWhales Discipline '
        f'Coach · {_postal()}.<br>Educational, not investment advice — counts and '
        f'trends only; figures stay in the app.'
        + (f'<br><a href="{unsubscribe_url}">Unsubscribe</a> (one click).'
           if unsubscribe_url else "")
        + "</p>"
    )
    headers = {}
    if unsubscribe_url:
        headers["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    payload = {
        "from": os.getenv("AGENTICWHALES_EMAIL_FROM"),
        "to": [to],
        "subject": subject,
        "html": html + footer,
        "headers": headers,
    }
    if text:
        payload["text"] = text
    try:
        import requests
        resp = requests.post(
            _RESEND_URL, json=payload, timeout=15,
            headers={"Authorization": f"Bearer {os.getenv('RESEND_API_KEY')}"})
        if resp.status_code >= 300:
            log.warning("resend send failed (%s): %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as exc:  # noqa: BLE001 — email must never break the caller
        log.warning("resend send failed: %s", exc)
        return False
