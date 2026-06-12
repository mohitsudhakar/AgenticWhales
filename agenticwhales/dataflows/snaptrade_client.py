"""Minimal signed SnapTrade REST client.

SnapTrade's official SDK has a typing-incompatibility on Python 3.12, so we sign
requests ourselves. The signing scheme is copied verbatim from the SDK's
``request_after_hook.compute_request_signature``:

    sig_object = {"content": body-or-None, "path": "/api/v1"+subpath, "query": qs}
    Signature  = base64(HMAC_SHA256(consumer_key, json.dumps(sig_object, sorted, compact)))

Read-only by design — we only register users, open the connection portal, and
read accounts/activities. We never place orders.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests

_BASE = "https://api.snaptrade.com/api/v1"


def compute_signature(resource_path: str, consumer_key: str, body: Any) -> str:
    """Verbatim port of the SnapTrade SDK request signer."""
    subpath, _, query = resource_path.partition("?")
    sig_object = {
        "content": None if (body is None or body == {}) else body,
        "path": "/api/v1%s" % subpath,
        "query": query,
    }
    sig_content = json.dumps(sig_object, separators=(",", ":"), sort_keys=True)
    digest = hmac.new(consumer_key.encode(), sig_content.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


class SnapTradeClient:
    def __init__(self, client_id: str, consumer_key: str):
        self.client_id = client_id
        self.consumer_key = consumer_key

    def _request(self, method: str, path: str, *, query: Optional[Dict] = None,
                 body: Any = None) -> Any:
        q = dict(query or {})
        q["clientId"] = self.client_id
        q["timestamp"] = str(int(time.time()))
        qs = urlencode(q)
        resource_path = f"{path}?{qs}"
        sig = compute_signature(resource_path, self.consumer_key, body)
        headers = {"Signature": sig, "Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body)
        resp = requests.request(method, f"{_BASE}{path}?{qs}", headers=headers,
                                data=data, timeout=30)
        resp.raise_for_status()
        return resp.json() if resp.text else {}

    # --- the handful of endpoints the coach needs (all read-only) ---
    def register_user(self, user_id: str) -> Dict:
        return self._request("POST", "/snapTrade/registerUser", body={"userId": user_id})

    def login_portal(self, user_id: str, user_secret: str, **opts) -> Dict:
        return self._request("POST", "/snapTrade/login",
                             query={"userId": user_id, "userSecret": user_secret},
                             body=opts or None)

    def list_accounts(self, user_id: str, user_secret: str) -> List[Dict]:
        return self._request("GET", "/accounts",
                             query={"userId": user_id, "userSecret": user_secret})

    def get_account_activities(self, user_id: str, user_secret: str,
                               account_id: str, *, start: Optional[str] = None,
                               end: Optional[str] = None) -> List[Dict]:
        """Paged activities for ONE account (the endpoint SnapTrade kept).
        Handles both the paginated ``{"data": [...], "pagination": {...}}``
        shape and a bare list, defensively."""
        q: Dict[str, str] = {"userId": user_id, "userSecret": user_secret,
                             "limit": "500"}
        if start:
            q["startDate"] = start
        if end:
            q["endDate"] = end
        out: List[Dict] = []
        offset = 0
        while True:
            page = self._request("GET", f"/accounts/{account_id}/activities",
                                 query={**q, "offset": str(offset)})
            rows = page.get("data") if isinstance(page, dict) else page
            if not isinstance(rows, list):
                rows = []
            out.extend(rows)
            pagination = page.get("pagination") if isinstance(page, dict) else None
            total = (pagination or {}).get("total")
            if not rows or total is None or offset + len(rows) >= int(total):
                break
            offset += len(rows)
            if offset > 100_000:   # hard stop against a misbehaving API
                break
        return out

    def get_activities(self, user_id: str, user_secret: str, *,
                       start: Optional[str] = None, end: Optional[str] = None) -> List[Dict]:
        """All activities across the user's connected accounts.

        SnapTrade RETIRED the global ``GET /activities`` endpoint (it returns
        410 Gone as of 2026); activities are per-account now. Same public
        signature as before: list accounts, fan out, concatenate. One broken
        account doesn't sink the others — we only raise if every account
        failed and nothing was retrieved."""
        accounts = self.list_accounts(user_id, user_secret) or []
        out: List[Dict] = []
        first_error: Optional[Exception] = None
        for account in accounts:
            account_id = account.get("id")
            if not account_id:
                continue
            try:
                out.extend(self.get_account_activities(
                    user_id, user_secret, account_id, start=start, end=end))
            except Exception as exc:  # noqa: BLE001 — isolate per-account failures
                first_error = first_error or exc
        if first_error is not None and not out:
            raise first_error
        return out


def from_env() -> Optional[SnapTradeClient]:
    """Build a client from env, or None if SnapTrade isn't configured."""
    cid = os.getenv("SNAPTRADE_CLIENT_ID")
    ck = os.getenv("SNAPTRADE_CONSUMER_KEY")
    return SnapTradeClient(cid, ck) if (cid and ck) else None
