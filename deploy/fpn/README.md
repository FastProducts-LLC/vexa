# deploy/fpn — FastProducts deployment additions

Fork-only deploy assets for the FastProducts-LLC `fpn` branch (see `FORK.md` at the repo root):

- `tts-cartesia/` — the OpenAI-compatible TTS shim over the Cartesia API that backs the
  voice-agent speak path (`docker-compose.fpn.yml` wires it as the `tts-cartesia` service).
- `stt-hosted/` — the OpenAI-compatible STT shim over hosted providers (Deepgram / Groq) that
  replaces the self-hosted `deploy/transcription` unit (`docker-compose.fpn.yml` wires it as the
  `stt-hosted` service and points meeting-api's `TRANSCRIPTION_SERVICE_URL` at it).

Used together with `deploy/compose/docker-compose.fpn.yml` (the overlay over the stock v0.12
compose file) and built to GHCR by `.github/workflows/fpn-images.yml`.

## Runtime user and filesystem

Both images run as the dedicated unprivileged user `app` (uid/gid **10001**, numeric `USER` in
the Dockerfile) and listen on the unprivileged port 8080. Neither keeps state on disk: inference
is remote, logs go to stdout, and `/app` is root-owned so the service user cannot modify its own
code. The overlay additionally runs them with `user: "10001:10001"`, `read_only: true`, a tmpfs
on `/tmp` (Starlette spools multipart uploads larger than 1 MiB there), `cap_drop: [ALL]` and
`no-new-privileges`. The compose healthcheck is a `python -c urllib` probe and needs nothing extra.

The two shims mount **no named volumes or bind mounts**, so there is nothing on the droplet to
re-own when rolling from the old root images to these. If a volume is ever added (for example a
cache under `$XDG_CACHE_HOME=/home/app/.cache`), an existing root-owned volume needs a one-time
`chown -R 10001:10001` on the host before the non-root container can write to it.
