# FastProducts fork of Vexa (fpn)

This is FastProducts-LLC's deployment fork of [Vexa-ai/vexa](https://github.com/Vexa-ai/vexa).

- **Upstream pin:** `Vexa-ai/vexa` `main` @ `ad0b8caebba5442a290f1b95f6ba3cf5796a9cd6`
- **Branch:** `fpn` — the pinned upstream SHA plus exactly two commits (the patch policy below).
- **Default branch:** unchanged from upstream (`main`); `fpn` is the deployed branch.

## Patch policy

`fpn` carries exactly two commits on top of the upstream pin, and is **rebase-only**: to take a
new upstream version, reset `fpn` to the new upstream SHA and re-apply (rebase) the two commits —
never merge, never accumulate history. Update the pin recorded here and in the commit message.

1. **`meeting-api: wire acts.v1 speak/config control-plane`** — product code, intended for
   upstreaming to Vexa-ai/vexa:
   - `core/meetings/services/meeting-api/src/meeting_api/lifecycle/acts_router.py` (new):
     handlers for `POST/DELETE /bots/{platform}/{native_meeting_id}/speak` and
     `PUT /bots/{platform}/{native_meeting_id}/config` — the routes the gateway already forwarded
     but meeting-api never served (they were WAIVED in the api.v1 conformance gate). They publish
     the sealed acts.v1 `speak` / `speak_stop` / `reconfigure` acts on
     `bot_commands:meeting:{id}` via the same `CommandPublisher` port as the stop route.
   - `bot_spawn`: `voice_agent_enabled` accepted on `POST /bots` → persisted in `meeting.data`
     (the speak routes 409 without it), emitted as invocation.v1 `voiceAgentEnabled`, and the TTS
     creds (`TTS_SERVICE_URL` / `TTS_SERVICE_TOKEN` env → bot env `TTS_SERVICE_URL` /
     `TTS_API_TOKEN`) threaded into the voice-agent workload spec's `extra_env`.
   - gateway: the missing `DELETE .../speak` forward.
   - tests: `tests/test_acts_route.py`; the two closed conformance waivers removed.

2. **`deploy: fpn overlay + Cartesia TTS shim + hosted STT shim + GHCR CI`** — deploy-only, never upstreamed:
   - `deploy/fpn/tts-cartesia/` — OpenAI-compatible `/v1/audio/speech` shim over Cartesia
     (`POST /tts/bytes`, raw PCM s16le 24kHz mono streamed through; auth via `X-API-Key` ==
     `TTS_SHIM_TOKEN`).
   - `deploy/fpn/stt-hosted/` — OpenAI-compatible `/v1/audio/transcriptions` shim over hosted STT
     providers (Deepgram by default, Groq real-Whisper optional) — a drop-in for
     `core/meetings/services/transcription` that replaces the separate `deploy/transcription`
     CPU/GPU unit; auth via `X-API-Key`/Bearer == `STT_SHIM_TOKEN`; contract tests alongside.
   - `deploy/compose/docker-compose.fpn.yml` — overlay over the stock compose file: fork images
     for meeting-api/gateway, the `tts-cartesia` + `stt-hosted` services (meeting-api's
     `TRANSCRIPTION_SERVICE_URL` defaults to the shim), the external `fpn_backend` network
     (gateway only, alias `vexa-gateway` — meeting-api trusts gateway-injected identity headers, so
     it stays on the internal `vexa` network), resource limits + bounded logging everywhere,
     terminal/mcp/agent-api disabled.
   - `.github/workflows/fpn-images.yml` — this file (see below).
   - this `FORK.md`.

## Images

Every push to `fpn` builds and publishes four images to GHCR (`.github/workflows/fpn-images.yml`),
tagged `:<commit sha>` (immutable) and `:latest`:

| Image | Context | Dockerfile |
|---|---|---|
| `ghcr.io/fastproducts-llc/vexa-meeting-api` | repo root | `core/meetings/services/meeting-api/Dockerfile` |
| `ghcr.io/fastproducts-llc/vexa-gateway` | `core/gateway/services/gateway` | `core/gateway/services/gateway/Dockerfile` |
| `ghcr.io/fastproducts-llc/vexa-tts-cartesia` | `deploy/fpn/tts-cartesia` | `deploy/fpn/tts-cartesia/Dockerfile` |
| `ghcr.io/fastproducts-llc/vexa-stt-hosted` | `deploy/fpn/stt-hosted` | `deploy/fpn/stt-hosted/Dockerfile` |

Build contexts mirror upstream's `release-images.yml` matrix (meeting-api needs the carve root —
its Dockerfile vendors the sealed contract schemas by path). All other services keep upstream's
published Docker Hub images (`vexaai/*:v012`), including the bot (`vexaai/vexa-bot:v012`).

## Deploy

From `deploy/compose/`:

```
docker network create fpn_backend   # once
docker compose -p vexa-v012 -f docker-compose.yml -f docker-compose.fpn.yml --env-file .env up -d --wait
```

Overlay env (see the header of `docker-compose.fpn.yml`): `FPN_VEXA_SHA`, `TTS_SERVICE_URL`,
`TTS_SERVICE_TOKEN` (= `TTS_SHIM_TOKEN`), `CARTESIA_API_KEY`, `CARTESIA_VOICE_MAP_JSON`,
`CARTESIA_DEFAULT_VOICE_ID`, plus the stock `.env`.
