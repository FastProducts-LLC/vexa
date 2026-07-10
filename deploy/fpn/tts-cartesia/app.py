"""tts-cartesia — an OpenAI-compatible TTS shim in front of the Cartesia API (fpn overlay).

The Vexa bot's speak path (``tts-playback.ts``) POSTs an OpenAI-style ``/v1/audio/speech`` request
(header ``X-API-Key``, body ``{model, input, voice, response_format}``) and streams the response
bytes as raw PCM s16le 24kHz mono into ``paplay``. This shim maps that request onto Cartesia's
``POST /tts/bytes`` (same PCM output format) and streams the bytes straight through.

Env:
  TTS_SHIM_TOKEN            required shared secret — requests must carry it as X-API-Key (401 else)
  CARTESIA_API_KEY          Cartesia API key
  CARTESIA_VERSION          Cartesia-Version header (default "2025-04-16")
  CARTESIA_TTS_MODEL        Cartesia model_id (default "sonic-3")
  CARTESIA_DEFAULT_VOICE_ID fallback Cartesia voice id when the request voice is unmapped/absent
  CARTESIA_VOICE_MAP_JSON   optional JSON object mapping request `voice` names -> Cartesia voice ids
"""
from __future__ import annotations

import json
import os

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

CARTESIA_TTS_URL = "https://api.cartesia.ai/tts/bytes"

app = FastAPI(title="vexa-tts-cartesia (fpn)", version="1.0.0")


def _voice_map() -> dict:
    raw = os.getenv("CARTESIA_VOICE_MAP_JSON") or ""
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _resolve_voice_id(requested: str | None) -> str:
    if requested:
        mapped = _voice_map().get(requested)
        if mapped:
            return str(mapped)
        # An exact Cartesia voice id passed straight through would also work, but without a map we
        # cannot tell a friendly name from an id — fall back to the default (below) unless mapped.
    default = os.getenv("CARTESIA_DEFAULT_VOICE_ID") or ""
    if not default:
        raise HTTPException(
            status_code=500,
            detail="CARTESIA_DEFAULT_VOICE_ID is not configured and the requested voice is unmapped",
        )
    return default


@app.get("/health")
async def health():
    return {"status": "ok", "service": "tts-cartesia"}


@app.post("/v1/audio/speech")
async def speech(request: Request, x_api_key: str | None = Header(default=None)):
    shim_token = os.getenv("TTS_SHIM_TOKEN") or ""
    if not shim_token or x_api_key != shim_token:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be an object")
    text = body.get("input")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=422, detail="'input' is required (non-empty string)")
    voice_id = _resolve_voice_id(body.get("voice") if isinstance(body.get("voice"), str) else None)

    cartesia_key = os.getenv("CARTESIA_API_KEY") or ""
    if not cartesia_key:
        raise HTTPException(status_code=500, detail="CARTESIA_API_KEY is not configured")

    upstream_request = {
        "model_id": os.getenv("CARTESIA_TTS_MODEL", "sonic-3"),
        "transcript": text,
        "voice": {"mode": "id", "id": voice_id},
        # The bot's playback pipeline expects raw PCM s16le 24kHz mono — fixed regardless of the
        # OpenAI-style `response_format` hint (paplay is invoked with exactly this format).
        "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 24000},
    }
    headers = {
        "X-API-Key": cartesia_key,
        "Cartesia-Version": os.getenv("CARTESIA_VERSION", "2025-04-16"),
        "Content-Type": "application/json",
    }

    client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=120.0))
    req = client.build_request("POST", CARTESIA_TTS_URL, json=upstream_request, headers=headers)
    upstream = await client.send(req, stream=True)
    if upstream.status_code != 200:
        detail = (await upstream.aread())[:500].decode("utf-8", errors="replace")
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"cartesia error {upstream.status_code}: {detail}")

    async def stream():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(stream(), media_type="application/octet-stream")
