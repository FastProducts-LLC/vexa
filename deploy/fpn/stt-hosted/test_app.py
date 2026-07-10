"""Contract tests for the stt-hosted shim — no network, providers mocked via httpx.MockTransport.

Run:  pip install -r requirements.txt pytest && pytest -q
"""
from __future__ import annotations

import json
import math

import httpx
import pytest
from fastapi.testclient import TestClient

import app as shim

TOKEN = "test-shim-token"
WAV = b"RIFF....WAVEfmt fake-but-nonempty-audio-bytes"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("STT_SHIM_TOKEN", TOKEN)
    monkeypatch.setenv("GROQ_API_KEY", "gk_test")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg_test")
    yield
    if hasattr(shim.app.state, "upstream_transport"):
        del shim.app.state.upstream_transport


def _client() -> TestClient:
    return TestClient(shim.app)


def _post(client: TestClient, headers: dict | None = None, **form_overrides):
    # Mirror what core/meetings/modules/whisper TranscriptionClient sends.
    data = {
        "model": "whisper-1",
        "response_format": "verbose_json",
        "timestamp_granularities": "word",
        **form_overrides,
    }
    return client.post(
        "/v1/audio/transcriptions",
        files={"file": ("audio.wav", WAV, "audio/wav")},
        data=data,
        headers=headers if headers is not None else {"Authorization": f"Bearer {TOKEN}"},
    )


def _mock_upstream(handler):
    shim.app.state.upstream_transport = httpx.MockTransport(handler)


# ── auth ────────────────────────────────────────────────────────────────────────────────


def test_missing_token_is_401():
    assert _post(_client(), headers={}).status_code == 401


def test_wrong_token_is_401():
    assert _post(_client(), headers={"Authorization": "Bearer nope"}).status_code == 401


def test_x_api_key_accepted(monkeypatch):
    monkeypatch.setenv("STT_PROVIDER", "groq")
    _mock_upstream(lambda req: httpx.Response(200, json={"text": "", "segments": []}))
    resp = _post(_client(), headers={"X-API-Key": TOKEN})
    assert resp.status_code == 200


def test_unset_shim_token_rejects_everything(monkeypatch):
    monkeypatch.delenv("STT_SHIM_TOKEN")
    assert _post(_client()).status_code == 401


# ── groq ────────────────────────────────────────────────────────────────────────────────


def test_groq_request_shape_and_normalization(monkeypatch):
    monkeypatch.setenv("STT_PROVIDER", "groq")
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["auth"] = req.headers.get("Authorization")
        body = req.read()
        seen["body"] = body
        return httpx.Response(
            200,
            json={
                "text": "hello world",
                "language": "en",
                "duration": 2.5,
                # OpenAI/Groq style: words TOP-LEVEL (no probability), segments without words.
                "words": [
                    {"word": "hello", "start": 0.1, "end": 0.5},
                    {"word": "world", "start": 0.6, "end": 1.0},
                ],
                "segments": [
                    {
                        "id": 0,
                        "start": 0.0,
                        "end": 2.5,
                        "text": " hello world",
                        "avg_logprob": -0.2,
                        "compression_ratio": 1.3,
                        "no_speech_prob": 0.01,
                    }
                ],
            },
        )

    _mock_upstream(handler)
    resp = _post(_client(), language="en", prompt="prior context")
    assert resp.status_code == 200
    out = resp.json()

    assert seen["url"].endswith("/v1/audio/transcriptions")
    assert seen["auth"] == "Bearer gk_test"
    # The forwarded form must carry Groq's model + both granularities, never "whisper-1".
    assert b"whisper-large-v3-turbo" in seen["body"]
    assert b"whisper-1" not in seen["body"]
    assert seen["body"].count(b"timestamp_granularities[]") == 2
    assert b"prior context" in seen["body"]

    assert out["text"] == "hello world"
    assert out["language"] == "en"
    assert out["duration"] == 2.5
    seg = out["segments"][0]
    # Every confidence field the client's isLowConfidenceSegment reads must be present.
    assert seg["avg_logprob"] == -0.2
    assert seg["no_speech_prob"] == 0.01
    assert seg["compression_ratio"] == 1.3
    assert seg["audio_start"] == 0.0 and seg["audio_end"] == 2.5
    # Top-level words re-nested into the segment, probability defaulted.
    assert [w["word"] for w in seg["words"]] == ["hello", "world"]
    assert all(w["probability"] == 1.0 for w in seg["words"])


def test_groq_silence_returns_empty(monkeypatch):
    monkeypatch.setenv("STT_PROVIDER", "groq")
    _mock_upstream(lambda req: httpx.Response(200, json={"text": "", "segments": []}))
    out = _post(_client()).json()
    assert out["text"] == "" and out["segments"] == [] and out["duration"] == 0.0


# ── deepgram ────────────────────────────────────────────────────────────────────────────


def test_deepgram_segment_synthesis(monkeypatch):
    monkeypatch.setenv("STT_PROVIDER", "deepgram")

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers.get("Authorization") == "Token dg_test"
        assert req.url.params["model"] == "nova-3"
        assert req.read() == WAV  # raw body passthrough, not multipart
        return httpx.Response(
            200,
            json={
                "metadata": {"duration": 6.0},
                "results": {
                    "channels": [
                        {
                            "detected_language": "en",
                            "alternatives": [
                                {
                                    "transcript": "hello there. yes.",
                                    "confidence": 0.98,
                                    "words": [
                                        {"word": "hello", "punctuated_word": "Hello", "start": 0.1, "end": 0.4, "confidence": 0.99},
                                        {"word": "there", "punctuated_word": "there.", "start": 0.5, "end": 0.9, "confidence": 0.97},
                                        # 2.1s gap -> new segment
                                        {"word": "yes", "punctuated_word": "Yes.", "start": 3.0, "end": 3.3, "confidence": 0.5},
                                    ],
                                }
                            ],
                        }
                    ]
                },
            },
        )

    _mock_upstream(handler)
    out = _post(_client()).json()

    assert out["language"] == "en"
    assert out["duration"] == 6.0
    assert len(out["segments"]) == 2
    first, second = out["segments"]
    assert first["text"].strip() == "Hello there."
    assert second["text"].strip() == "Yes."
    # confidence -> whisper-ish confidence fields the client's filter can read
    assert first["avg_logprob"] == pytest.approx(math.log(0.98), abs=1e-6)
    assert first["no_speech_prob"] == pytest.approx(0.02, abs=1e-6)
    assert second["avg_logprob"] == pytest.approx(math.log(0.5), abs=1e-6)
    assert first["words"][0]["probability"] == 0.99
    assert out["text"] == "Hello there. Yes."


def test_default_provider_is_deepgram(monkeypatch):
    monkeypatch.delenv("STT_PROVIDER", raising=False)
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        return httpx.Response(
            200, json={"metadata": {"duration": 0.0}, "results": {"channels": [{"alternatives": [{"transcript": "", "words": []}]}]}}
        )

    _mock_upstream(handler)
    assert _post(_client()).status_code == 200
    assert seen["path"] == "/v1/listen"


def test_deepgram_empty_audio_result(monkeypatch):
    monkeypatch.setenv("STT_PROVIDER", "deepgram")
    _mock_upstream(
        lambda req: httpx.Response(
            200, json={"metadata": {"duration": 1.0}, "results": {"channels": [{"alternatives": [{"transcript": "", "words": []}]}]}}
        )
    )
    out = _post(_client()).json()
    assert out["text"] == "" and out["segments"] == []


# ── failure vocabulary (matches the whisper client's classifyHttp) ─────────────────────


@pytest.mark.parametrize(
    "upstream_status,shim_status",
    [(402, 402), (401, 401), (403, 401), (429, 429), (400, 400), (500, 502), (503, 502)],
)
def test_upstream_error_mapping(monkeypatch, upstream_status, shim_status):
    monkeypatch.setenv("STT_PROVIDER", "groq")
    _mock_upstream(lambda req: httpx.Response(upstream_status, json={"error": "x"}))
    assert _post(_client()).status_code == shim_status


def test_upstream_timeout_is_504(monkeypatch):
    monkeypatch.setenv("STT_PROVIDER", "groq")

    def handler(req: httpx.Request):
        raise httpx.ReadTimeout("slow upstream")

    _mock_upstream(handler)
    assert _post(_client()).status_code == 504


# ── surface guards ─────────────────────────────────────────────────────────────────────


def test_translate_task_rejected():
    assert _post(_client(), task="translate").status_code == 400


def test_unknown_provider_is_500(monkeypatch):
    monkeypatch.setenv("STT_PROVIDER", "whisperx")
    assert _post(_client()).status_code == 500


def test_health():
    resp = TestClient(shim.app).get("/health")
    assert resp.status_code == 200
    assert resp.json()["service"] == "stt-hosted"
