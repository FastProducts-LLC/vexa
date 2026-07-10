"""stt-hosted — an OpenAI-compatible STT shim over hosted providers (fpn overlay).

The bot's whisper client (core/meetings/modules/whisper) POSTs an OpenAI-style
``/v1/audio/transcriptions`` multipart request (16 kHz mono WAV windows, bearer token,
``response_format=verbose_json`` with word timestamps) and expects faster-whisper-shaped
segments back — including the confidence fields (``avg_logprob`` / ``no_speech_prob`` /
``compression_ratio``) its low-confidence hallucination filter keys off. This shim serves
that exact surface (a drop-in for core/meetings/services/transcription) but forwards the
audio window to a hosted provider instead of running local inference:

  deepgram (default)  Deepgram's pre-recorded ``/v1/listen``. Deepgram returns word-level
                      confidences, not Whisper logprobs, so the shim synthesizes segments from
                      word gaps and maps confidence -> avg_logprob = ln(mean confidence),
                      no_speech_prob = 1 - mean confidence, compression_ratio = 1.0.
  groq                Groq's OpenAI-compatible audio API running real Whisper — the verbose_json
                      response carries genuine Whisper confidence fields; the shim only re-nests
                      the top-level word list into per-segment ``words``.

Env:
  STT_SHIM_TOKEN      required shared secret — requests must carry it as X-API-Key or
                      ``Authorization: Bearer`` (401 else; the whisper client sends Bearer)
  STT_PROVIDER        "deepgram" (default) or "groq"
  DEEPGRAM_API_KEY    Deepgram API key (provider deepgram)
  DEEPGRAM_STT_MODEL  Deepgram model (default "nova-3")
  DEEPGRAM_BASE_URL   override for tests (default "https://api.deepgram.com")
  GROQ_API_KEY        Groq API key (provider groq)
  GROQ_STT_MODEL      Groq model id (default "whisper-large-v3-turbo")
  GROQ_BASE_URL       override for tests (default "https://api.groq.com/openai")
  STT_SEGMENT_GAP_S   deepgram only: silence gap that splits synthesized segments (default 0.8)
"""
from __future__ import annotations

import math
import os
from typing import Any, Optional

import httpx
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile

GROQ_DEFAULT_BASE = "https://api.groq.com/openai"
DEEPGRAM_DEFAULT_BASE = "https://api.deepgram.com"

app = FastAPI(title="vexa-stt-hosted (fpn)", version="1.0.0")


def _provider() -> str:
    return (os.getenv("STT_PROVIDER") or "deepgram").strip().lower()


def _verify_token(request: Request, x_api_key: Optional[str]) -> None:
    token = (os.getenv("STT_SHIM_TOKEN") or "").strip()
    if token:
        if x_api_key == token:
            return
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth.removeprefix("Bearer ").strip() == token:
            return
    raise HTTPException(status_code=401, detail="invalid or missing API token")


def _upstream_client() -> httpx.AsyncClient:
    # Tests inject a MockTransport via app.state; production builds a real client. The whisper
    # client aborts at 30s, so stay under that with margin for our own response serialization.
    transport = getattr(app.state, "upstream_transport", None)
    return httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(5.0, read=25.0))


def _upstream_error(status: int, body: str) -> HTTPException:
    """Map a provider failure onto the status vocabulary the whisper client classifies
    (402 payment_required, 401 unauthorized, 429 rate_limited retryable, 5xx retryable)."""
    detail = f"upstream stt error {status}: {body[:300]}"
    if status == 402:
        return HTTPException(status_code=402, detail=detail)
    if status in (401, 403):
        return HTTPException(status_code=401, detail=detail)
    if status == 429:
        return HTTPException(status_code=429, detail=detail, headers={"Retry-After": "1"})
    if 400 <= status < 500:
        return HTTPException(status_code=400, detail=detail)
    return HTTPException(status_code=502, detail=detail)


def _nest_words(segments: list[dict], words: list[dict]) -> None:
    """OpenAI-style verbose_json returns words as a TOP-LEVEL list; faster-whisper (what the
    whisper client was written against) nests them per segment. Re-nest by time overlap."""
    for seg in segments:
        seg_words = [
            {
                "word": w.get("word", ""),
                "start": float(w.get("start") or 0.0),
                "end": float(w.get("end") or 0.0),
                # OpenAI/Groq word entries carry no probability; the client types it but never
                # gates on it — report full confidence rather than inventing one.
                "probability": float(w.get("probability", 1.0)),
            }
            for w in words
            if float(w.get("start") or 0.0) < float(seg["end"]) and float(w.get("end") or 0.0) > float(seg["start"])
        ]
        if seg_words:
            seg["words"] = seg_words


def _normalize_verbose_json(payload: dict, fallback_language: Optional[str]) -> dict:
    segments_out: list[dict] = []
    for idx, s in enumerate(payload.get("segments") or []):
        start = float(s.get("start") or 0.0)
        end = float(s.get("end") or 0.0)
        seg: dict[str, Any] = {
            "id": s.get("id", idx),
            "seek": s.get("seek", 0),
            "start": start,
            "end": end,
            "text": s.get("text") or "",
            "tokens": s.get("tokens") or [],
            "temperature": float(s.get("temperature", 0.0)),
            "avg_logprob": float(s.get("avg_logprob", 0.0)),
            "compression_ratio": float(s.get("compression_ratio", 1.0)),
            "no_speech_prob": float(s.get("no_speech_prob", 0.0)),
            "audio_start": start,
            "audio_end": end,
        }
        nested = s.get("words")
        if nested:
            seg["words"] = [
                {
                    "word": w.get("word", ""),
                    "start": float(w.get("start") or 0.0),
                    "end": float(w.get("end") or 0.0),
                    "probability": float(w.get("probability", 1.0)),
                }
                for w in nested
            ]
        segments_out.append(seg)

    top_words = payload.get("words") or []
    if top_words and segments_out and not any("words" in s for s in segments_out):
        _nest_words(segments_out, top_words)

    duration = float(payload.get("duration") or (segments_out[-1]["end"] if segments_out else 0.0))
    return {
        "text": (payload.get("text") or "").strip(),
        "language": payload.get("language") or fallback_language or "unknown",
        "language_probability": float(payload.get("language_probability", 1.0)),
        "duration": duration,
        "segments": segments_out,
    }


async def _transcribe_groq(
    audio: bytes,
    filename: str,
    content_type: str,
    language: Optional[str],
    prompt: Optional[str],
) -> dict:
    api_key = (os.getenv("GROQ_API_KEY") or "").strip()
    if not api_key:
        raise HTTPException(status_code=500, detail="GROQ_API_KEY is not configured")
    base = (os.getenv("GROQ_BASE_URL") or GROQ_DEFAULT_BASE).rstrip("/")

    # A list value renders as a repeated multipart field (both granularities), which is how
    # the OpenAI audio API expects timestamp_granularities[] to arrive.
    data: dict[str, Any] = {
        "model": os.getenv("GROQ_STT_MODEL", "whisper-large-v3-turbo"),
        "response_format": "verbose_json",
        "temperature": "0",
        "timestamp_granularities[]": ["word", "segment"],
    }
    if language:
        data["language"] = language
    if prompt:
        data["prompt"] = prompt

    async with _upstream_client() as client:
        try:
            resp = await client.post(
                f"{base}/v1/audio/transcriptions",
                data=data,
                files={"file": (filename, audio, content_type)},
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="upstream stt timeout (groq)")
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"upstream stt unreachable (groq): {e}")
    if resp.status_code != 200:
        raise _upstream_error(resp.status_code, resp.text)
    return _normalize_verbose_json(resp.json(), language)


def _segments_from_words(words: list[dict], gap_s: float, max_speech_s: float) -> list[dict]:
    """Synthesize faster-whisper-shaped segments from Deepgram's flat word list: split where
    the inter-word silence exceeds gap_s, or a segment would outgrow max_speech_s."""
    segments: list[dict] = []
    group: list[dict] = []

    def flush() -> None:
        if not group:
            return
        confidences = [float(w.get("confidence", 1.0)) for w in group]
        mean_conf = sum(confidences) / len(confidences)
        text = " ".join((w.get("punctuated_word") or w.get("word") or "").strip() for w in group).strip()
        start = float(group[0].get("start") or 0.0)
        end = float(group[-1].get("end") or 0.0)
        segments.append(
            {
                "id": len(segments),
                "seek": 0,
                "start": start,
                "end": end,
                "text": f" {text}",
                "tokens": [],
                "temperature": 0.0,
                # confidence -> logprob-ish so the client's isLowConfidenceSegment thresholds
                # (-1.0 / -1.3) stay meaningful: conf 1.0 -> 0, 0.5 -> -0.69, 0.25 -> -1.39.
                "avg_logprob": math.log(max(mean_conf, 1e-3)),
                "compression_ratio": 1.0,
                "no_speech_prob": 1.0 - mean_conf,
                "audio_start": start,
                "audio_end": end,
                "words": [
                    {
                        "word": (w.get("punctuated_word") or w.get("word") or ""),
                        "start": float(w.get("start") or 0.0),
                        "end": float(w.get("end") or 0.0),
                        "probability": float(w.get("confidence", 1.0)),
                    }
                    for w in group
                ],
            }
        )
        group.clear()

    for w in words:
        if group:
            gap = float(w.get("start") or 0.0) - float(group[-1].get("end") or 0.0)
            length = float(w.get("end") or 0.0) - float(group[0].get("start") or 0.0)
            if gap > gap_s or length > max_speech_s:
                flush()
        group.append(w)
    flush()
    return segments


async def _transcribe_deepgram(
    audio: bytes,
    content_type: str,
    language: Optional[str],
    max_speech_s: float,
) -> dict:
    api_key = (os.getenv("DEEPGRAM_API_KEY") or "").strip()
    if not api_key:
        raise HTTPException(status_code=500, detail="DEEPGRAM_API_KEY is not configured")
    base = (os.getenv("DEEPGRAM_BASE_URL") or DEEPGRAM_DEFAULT_BASE).rstrip("/")

    params: dict[str, str] = {
        "model": os.getenv("DEEPGRAM_STT_MODEL", "nova-3"),
        "smart_format": "true",
        "punctuate": "true",
    }
    if language:
        params["language"] = language
    else:
        params["detect_language"] = "true"

    async with _upstream_client() as client:
        try:
            resp = await client.post(
                f"{base}/v1/listen",
                params=params,
                content=audio,
                headers={"Authorization": f"Token {api_key}", "Content-Type": content_type},
            )
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="upstream stt timeout (deepgram)")
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"upstream stt unreachable (deepgram): {e}")
    if resp.status_code != 200:
        raise _upstream_error(resp.status_code, resp.text)

    payload = resp.json()
    channels = ((payload.get("results") or {}).get("channels")) or [{}]
    channel = channels[0] or {}
    alt = (channel.get("alternatives") or [{}])[0] or {}
    words = alt.get("words") or []

    gap_s = float(os.getenv("STT_SEGMENT_GAP_S") or 0.8)
    segments = _segments_from_words(words, gap_s=gap_s, max_speech_s=max_speech_s)
    duration = float(((payload.get("metadata") or {}).get("duration")) or (segments[-1]["end"] if segments else 0.0))
    return {
        "text": " ".join(s["text"].strip() for s in segments).strip(),
        "language": channel.get("detected_language") or language or "unknown",
        "language_probability": float(channel.get("language_confidence", 1.0)),
        "duration": duration,
        "segments": segments,
    }


@app.get("/health")
async def health():
    return {"status": "ok", "service": "stt-hosted", "provider": _provider()}


@app.get("/")
async def root():
    return {
        "service": "vexa-stt-hosted (fpn)",
        "provider": _provider(),
        "endpoints": {"transcribe": "/v1/audio/transcriptions", "health": "/health"},
    }


@app.post("/v1/audio/transcriptions")
async def transcribe_audio(
    request: Request,
    file: UploadFile = File(...),
    model: str = Form("whisper-1"),
    temperature: str = Form("0"),
    language: Optional[str] = Form(None),
    prompt: Optional[str] = Form(None),
    response_format: str = Form("verbose_json"),
    timestamp_granularities: str = Form("segment"),
    max_speech_duration_s: Optional[str] = Form(None),
    min_silence_duration_ms: Optional[str] = Form(None),
    transcription_tier: Optional[str] = Form(None),
    task: str = Form("transcribe"),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
):
    _verify_token(request, x_api_key)
    if task != "transcribe":
        raise HTTPException(status_code=400, detail=f"task {task!r} is not supported (transcribe only)")

    audio = await file.read()
    if not audio:
        raise HTTPException(status_code=400, detail="empty audio file")

    try:
        max_speech_s = float(max_speech_duration_s) if max_speech_duration_s else 15.0
    except ValueError:
        max_speech_s = 15.0

    provider = _provider()
    if provider == "groq":
        return await _transcribe_groq(
            audio,
            filename=file.filename or "audio.wav",
            content_type=file.content_type or "audio/wav",
            language=language,
            prompt=prompt,
        )
    if provider == "deepgram":
        return await _transcribe_deepgram(
            audio,
            content_type=file.content_type or "audio/wav",
            language=language,
            max_speech_s=max_speech_s,
        )
    raise HTTPException(status_code=500, detail=f"unknown STT_PROVIDER {provider!r} (deepgram | groq)")
