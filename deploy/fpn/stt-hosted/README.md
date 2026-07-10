# stt-hosted — OpenAI-compatible STT shim over hosted providers (fpn)

The bot's whisper client (`core/meetings/modules/whisper`) POSTs OpenAI-style
`/v1/audio/transcriptions` multipart requests (16 kHz mono WAV windows, `Authorization: Bearer`,
`response_format=verbose_json` with word timestamps) and expects faster-whisper-shaped segments
back — including the `avg_logprob` / `no_speech_prob` / `compression_ratio` confidence fields its
low-confidence hallucination filter reads. This service is a drop-in for
`core/meetings/services/transcription` (same surface, same auth, same failure vocabulary) that
forwards each window to a hosted provider instead of running local inference:

- **deepgram** (default) — Deepgram's pre-recorded `/v1/listen` (`DEEPGRAM_STT_MODEL`, default
  `nova-3`) — the same vendor the Recall voice loop already uses, so prod carries one STT key.
  Deepgram returns word confidences, not Whisper logprobs, so the shim synthesizes segments from
  word gaps (`STT_SEGMENT_GAP_S`, default 0.8) and maps confidence → `avg_logprob = ln(mean)`,
  `no_speech_prob = 1 − mean`, `compression_ratio = 1.0`.
- **groq** — Groq's OpenAI-compatible audio API running real Whisper
  (`GROQ_STT_MODEL`, default `whisper-large-v3-turbo`). Confidence fields arrive genuine; the
  shim only re-nests the top-level word list into per-segment `words`.

Routes:

- `POST /v1/audio/transcriptions` — auth: `X-API-Key` or `Authorization: Bearer` must equal
  `STT_SHIM_TOKEN` (401 otherwise). Provider failures map onto the status vocabulary the whisper
  client classifies: 402 payment_required, 401 unauthorized, 429 rate_limited (retryable),
  502/504 unavailable/timeout (retryable).
- `GET /health` — liveness (reports the active provider).

Env: `STT_SHIM_TOKEN`, `STT_PROVIDER` (`deepgram` | `groq`), `DEEPGRAM_API_KEY`,
`DEEPGRAM_STT_MODEL`, `GROQ_API_KEY`, `GROQ_STT_MODEL`, `STT_SEGMENT_GAP_S`; `GROQ_BASE_URL` /
`DEEPGRAM_BASE_URL` exist for tests.

Image: `ghcr.io/fastproducts-llc/vexa-stt-hosted` (built by `.github/workflows/fpn-images.yml`);
deployed by `deploy/compose/docker-compose.fpn.yml` as the internal-only `stt-hosted` service —
the overlay defaults `TRANSCRIPTION_SERVICE_URL` to it, replacing the separate
`deploy/transcription` CPU/GPU unit.

## Cost shape

The bot resubmits a growing turn window every ~2 s while someone speaks (`SUBMIT_TICK_MS`,
turns capped at 28 s), so billed audio is roughly 5–7× active-speech time. At Deepgram nova-3
pre-recorded rates that lands around $1.30–1.80 per hour of continuous speech (Groq
`whisper-large-v3-turbo` would be ~$0.25–0.30).

## Verify

```bash
pip install -r requirements.txt pytest && pytest -q   # contract tests, providers mocked
```
