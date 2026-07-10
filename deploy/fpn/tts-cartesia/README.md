# tts-cartesia — OpenAI-compatible TTS shim over Cartesia (fpn)

The Vexa bot's speak path (`core/meetings/services/bot/src/tts-playback.ts`) POSTs an OpenAI-style
`/v1/audio/speech` request (header `X-API-Key`, body `{model, input, voice, response_format}`) and
pipes the response into `paplay` as raw PCM s16le 24kHz mono. This service maps that request onto
Cartesia's `POST /tts/bytes` with the same PCM output format and streams the bytes through.

Routes:

- `POST /v1/audio/speech` — auth: `X-API-Key` must equal `TTS_SHIM_TOKEN` (401 otherwise);
  `input` required. The `voice` name is mapped to a Cartesia voice id via `CARTESIA_VOICE_MAP_JSON`
  (a JSON object), falling back to `CARTESIA_DEFAULT_VOICE_ID`.
- `GET /health` — liveness.

Env: `TTS_SHIM_TOKEN`, `CARTESIA_API_KEY`, `CARTESIA_VERSION` (default `2025-04-16`),
`CARTESIA_TTS_MODEL` (default `sonic-3`), `CARTESIA_VOICE_MAP_JSON`, `CARTESIA_DEFAULT_VOICE_ID`.

Image: `ghcr.io/fastproducts-llc/vexa-tts-cartesia` (built by `.github/workflows/fpn-images.yml`);
deployed by `deploy/compose/docker-compose.fpn.yml` as the internal-only `tts-cartesia` service.
