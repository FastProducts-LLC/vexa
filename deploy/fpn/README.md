# deploy/fpn — FastProducts deployment additions

Fork-only deploy assets for the FastProducts-LLC `fpn` branch (see `FORK.md` at the repo root):

- `tts-cartesia/` — the OpenAI-compatible TTS shim over the Cartesia API that backs the
  voice-agent speak path (`docker-compose.fpn.yml` wires it as the `tts-cartesia` service).

Used together with `deploy/compose/docker-compose.fpn.yml` (the overlay over the stock v0.12
compose file) and built to GHCR by `.github/workflows/fpn-images.yml`.
