"""Vision extraction (screenshots -> transactions) — fake LLM, no network."""

import json

import pytest

from agenticwhales.transactions import extract as ex

ROW_A = {"date": "2025-01-06", "type": "Buy", "symbol": "AAPL",
         "quantity": 50, "price": 180, "amount": -9000}
ROW_B = {"date": "2025-01-08", "type": "Sell", "symbol": "AAPL",
         "quantity": 50, "price": 184, "amount": 9200}


class FakeVisionLLM:
    """Captures messages; returns one queued payload per call."""

    def __init__(self, payloads, usage=None):
        self.payloads = list(payloads)
        self.calls = []
        self.usage = usage or {"input_tokens": 1000, "output_tokens": 50}

    def invoke(self, messages):
        self.calls.append(messages)
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload

        class R:
            pass
        r = R()
        r.content = json.dumps({"transactions": payload})
        r.usage_metadata = dict(self.usage)
        return r


def test_vision_gating_is_explicit_env_not_key_presence(monkeypatch):
    # conftest sets GOOGLE_API_KEY=placeholder for every test — that must NOT
    # enable vision, or CI would fire real network calls.
    monkeypatch.delenv("AGENTICWHALES_VISION_PROVIDER", raising=False)
    assert ex.vision_config() is None
    monkeypatch.setenv("AGENTICWHALES_VISION_PROVIDER", "google")
    assert ex.vision_config() == ("google", "gemini-3-flash-preview")
    monkeypatch.setenv("AGENTICWHALES_VISION_MODEL", "custom-vision-1")
    assert ex.vision_config() == ("google", "custom-vision-1")


def test_extract_image_builds_multimodal_message_and_parses():
    llm = FakeVisionLLM([[ROW_A, ROW_B]])
    txns = ex.extract_transactions_from_image([b"\x89PNGfake"], ["image/png"], llm=llm)
    assert len(txns) == 2 and txns[0].symbol == "AAPL"
    # Message shape: [system, human(content blocks)]
    sys_msg, human = llm.calls[0]
    assert "financial-document parser" in str(sys_msg.content)
    blocks = human.content
    assert isinstance(blocks, list) and blocks[0]["type"] == "text"
    assert blocks[1]["type"] == "image_url"
    assert blocks[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_overlapping_screenshots_dedupe():
    llm = FakeVisionLLM([[ROW_A, ROW_B], [ROW_B]])     # second image overlaps
    txns = ex.extract_transactions_from_image([b"img1", b"img2"],
                                              ["image/png", "image/png"], llm=llm)
    assert len(txns) == 2  # the duplicate Sell is dropped


def test_on_usage_reports_token_counts():
    seen = []
    llm = FakeVisionLLM([[ROW_A]])
    ex.extract_transactions_from_image([b"img"], llm=llm,
                                       on_usage=lambda i, o: seen.append((i, o)))
    assert seen == [(1000, 50)]


def test_partial_failure_warns_and_keeps_rest():
    boom = RuntimeError("unreadable")
    llm = FakeVisionLLM([boom, boom, boom, [ROW_A]])   # img1 fails all 3 retries
    warns = []
    txns = ex.extract_transactions_from_image([b"bad", b"good"],
                                              ["image/png", "image/png"],
                                              llm=llm, on_warn=warns.append)
    assert len(txns) == 1
    assert any("image 1 of 2" in w for w in warns)


def test_all_images_failing_raises():
    boom = RuntimeError("unreadable")
    llm = FakeVisionLLM([boom, boom, boom])
    with pytest.raises(RuntimeError):
        ex.extract_transactions_from_image([b"bad"], llm=llm)


def test_disabled_vision_raises_without_explicit_llm(monkeypatch):
    monkeypatch.delenv("AGENTICWHALES_VISION_PROVIDER", raising=False)
    with pytest.raises(RuntimeError, match="not enabled"):
        ex.extract_transactions_from_image([b"img"])
