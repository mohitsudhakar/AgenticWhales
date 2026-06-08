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

    def get_activities(self, user_id: str, user_secret: str, *,
                       start: Optional[str] = None, end: Optional[str] = None) -> List[Dict]:
        q = {"userId": user_id, "userSecret": user_secret}
        if start:
            q["startDate"] = start
        if end:
            q["endDate"] = end
        return self._request("GET", "/activities", query=q)


def from_env() -> Optional[SnapTradeClient]:
    """Build a client from env, or None if SnapTrade isn't configured."""
    cid = os.getenv("SNAPTRADE_CLIENT_ID")
    ck = os.getenv("SNAPTRADE_CONSUMER_KEY")
    return SnapTradeClient(cid, ck) if (cid and ck) else None
