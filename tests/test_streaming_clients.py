"""Coverage for agenticwhales/streaming.py WS client loop. A fake `websockets`
module is injected so connect/auth/subscribe/consume run with no network. The
stub client's playback path and the Alpaca reconnect/heartbeat branches are
exercised directly.
"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from agenticwhales import streaming as st
from agenticwhales.streaming import (
    AlpacaAuthError,
    AlpacaEquityClient,
    EventKind,
    StreamingEvent,
    InMemoryStreamClient,
)


# ---------------------------------------------------------------------------
# fake websockets module
# ---------------------------------------------------------------------------

class _FakeWS:
    def __init__(self, recv_msgs, client=None):
        self.sent = []
        self._recv = list(recv_msgs)
        self.client = client

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        if not self._recv:
            await asyncio.sleep(10)  # hang → wait_for times out
        item = self._recv.pop(0)
        if item == "HANG":
            await asyncio.sleep(10)
        return item


class _FakeConnect:
    def __init__(self, ws):
        self.ws = ws

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, *a):
        return False


@pytest.fixture
def fake_ws_module(monkeypatch):
    holder = {}

    import types
    mod = types.ModuleType("websockets")

    def _connect(url, ping_interval=None):
        return _FakeConnect(holder["ws"])

    mod.connect = _connect
    monkeypatch.setitem(sys.modules, "websockets", mod)
    return holder


@pytest.fixture(autouse=True)
def _no_alpaca_env(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)


def _client(**kw):
    base = dict(key_id="k", secret_key="s", heartbeat_timeout_s=0.05)
    base.update(kw)
    return AlpacaEquityClient(["AAPL"], **base)


# ===========================================================================
# _connect_and_consume
# ===========================================================================

def test_connect_consume_streams_trade(fake_ws_module):
    c = _client()
    fake_ws_module["ws"] = _FakeWS([
        json.dumps({"T": "success"}),                              # auth ok
        json.dumps([{"T": "subscription"}]),                       # sub ack
        json.dumps([{"T": "t", "S": "AAPL", "p": 150.0, "s": 5}]),  # a trade
        "HANG",                                                    # → heartbeat timeout → return
    ])
    q: asyncio.Queue = asyncio.Queue()
    asyncio.run(c._connect_and_consume(q))
    assert c._ws is not None
    assert not q.empty()
    ev = q.get_nowait()
    assert ev.symbol == "AAPL" and ev.kind == EventKind.TRADE
    # auth + subscribe were both sent
    assert any("auth" in s for s in fake_ws_module["ws"].sent)
    assert any("subscribe" in s for s in fake_ws_module["ws"].sent)


def test_connect_consume_auth_failure(fake_ws_module):
    c = _client()
    fake_ws_module["ws"] = _FakeWS([json.dumps({"T": "error", "msg": "bad key"})])
    with pytest.raises(AlpacaAuthError):
        asyncio.run(c._connect_and_consume(asyncio.Queue()))


# ===========================================================================
# run()
# ===========================================================================

def test_run_raises_without_keys():
    c = AlpacaEquityClient(["AAPL"], key_id="", secret_key="")
    with pytest.raises(AlpacaAuthError):
        asyncio.run(c.run(asyncio.Queue()))


def test_run_single_pass_then_stop(monkeypatch):
    c = _client()

    async def _consume(queue):
        c.stop()  # stop set → while loop exits after this pass

    monkeypatch.setattr(c, "_connect_and_consume", _consume)
    asyncio.run(c.run(asyncio.Queue()))  # completes cleanly


def test_run_reconnect_branch_returns_on_stop(monkeypatch):
    c = _client()
    c.stop()  # pre-set so the except-branch wait_for returns immediately

    async def _boom(queue):
        raise RuntimeError("ws dropped")

    monkeypatch.setattr(c, "_connect_and_consume", _boom)
    asyncio.run(c.run(asyncio.Queue()))  # exception → reconnect wait → return


# ===========================================================================
# update_subscriptions + _send_subscribe
# ===========================================================================

def test_update_subscriptions_noop_when_same():
    c = _client()
    # constructed with ["AAPL"]; same set → no resubscribe path
    asyncio.run(c.update_subscriptions(["aapl"]))
    assert c._symbols == ["AAPL"]


def test_update_subscriptions_resubscribes_when_ws_open():
    c = _client()
    ws = _FakeWS([])
    c._ws = ws
    asyncio.run(c.update_subscriptions(["AAPL", "NVDA"]))
    assert c._symbols == ["AAPL", "NVDA"]
    assert any("subscribe" in s for s in ws.sent)


def test_send_subscribe_includes_all_channels():
    c = _client(channels=("quotes", "trades"))
    ws = _FakeWS([])
    asyncio.run(c._send_subscribe(ws))
    payload = json.loads(ws.sent[0])
    assert payload["action"] == "subscribe"
    assert payload["quotes"] == ["AAPL"] and payload["trades"] == ["AAPL"]


# ===========================================================================
# InMemoryStreamClient
# ===========================================================================

def test_stub_plays_back_events():
    ev = StreamingEvent(source="stub", kind=EventKind.TRADE, symbol="AAPL",
                        received_at=1.0, payload={"p": 1})
    stub = InMemoryStreamClient([ev, ev])

    async def go():
        q: asyncio.Queue = asyncio.Queue()
        task = asyncio.create_task(stub.run(q))
        await asyncio.sleep(0.01)
        stub.stop()
        await task
        return q

    q = asyncio.run(go())
    assert q.qsize() == 2


def test_stub_stops_before_playback():
    stub = InMemoryStreamClient([StreamingEvent(source="stub", kind=EventKind.QUOTE,
                                            symbol="AAPL", received_at=1.0)])
    stub.stop()  # pre-stopped → run returns on first iteration
    q: asyncio.Queue = asyncio.Queue()
    asyncio.run(stub.run(q))
    assert q.empty()


def test_stub_update_subscriptions():
    stub = InMemoryStreamClient([])
    asyncio.run(stub.update_subscriptions(["AAPL", "NVDA"]))
    assert stub._subscriptions == ["AAPL", "NVDA"]
