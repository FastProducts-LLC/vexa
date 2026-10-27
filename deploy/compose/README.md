# deploy/compose — the v0.12 control-plane stack (P4)

`docker-compose.yml` brings up the v0.12 control plane: the infra (`postgres:17-alpine`,
`valkey/valkey:8-alpine`, `minio` + `minio-init`) and the long-running services below, each building its own
slim image from `<service>/Dockerfile`:

| service      | build context                          | host port | entrypoint                         |
|--------------|----------------------------------------|-----------|------------------------------------|
| admin-api    | `core/identity/services/admin-api`     | 18057     | `python -m admin_api`              |
| runtime      | `core/runtime`                         | 18090     | `python -m runtime_kernel`         |
| meeting-api  | `core/meetings/services/meeting-api`   | 18080     | `python -m meeting_api`            |
| agent-api    | `core/agent/services/agent-api`        | 18100     | `uvicorn control_plane.api`        |
| gateway      | `core/gateway/services/gateway`        | 18056     | `python -m gateway`                |
| terminal     | `clients/terminal`                     | 13000     | Next.js custom server              |

Every service answers `GET /health` and carries a compose healthcheck; `depends_on` waits on
`condition: service_healthy` so the bring-up is ordered. The `runtime` mounts
`/var/run/docker.sock` and spawns the bot (`BROWSER_IMAGE=vexaai/vexa-bot:v012`, published — a
reference, never built here; never point it at the published `vexaai/vexa-bot:dev`, which is the
old 0.10 line and incompatible with this stack's `lifecycle.v1`) on demand and the per-dispatch
agent worker (`vexaai/v012-agent-worker:v012`, a `build-only` compose profile); neither is a
long-running compose service.

## Usage

```bash
cp .env.example .env            # edit secrets/ports/DOCKER_GID
docker compose -f deploy/compose/docker-compose.yml build
docker compose -f deploy/compose/docker-compose.yml up -d
# poll until healthy, then:
curl -sf http://localhost:18056/health   # gateway
docker compose -f deploy/compose/docker-compose.yml down -v
```

`.env.example` documents every variable (faithful to the 0.11 `deploy/compose` names: `DB_*`,
`REDIS_URL`, `ADMIN_TOKEN`, `INTERNAL_API_SECRET`, `MINIO_*`, `BROWSER_IMAGE`/`AGENT_IMAGE`,
`DOCKER_GID`, `*_HOST_PORT`).

## Smoke probe — "is this install actually working?"

```bash
make probe                       # from the repo root (compose is the default surface)
```

Drives the ONE full journey through the gateway front door — spawn → schedule → boot → join →
transcribe → live-view → stop — then sweeps every component's logs once. Each stage prints
Expected / Actual / Verdict; a red stage names where the journey broke and fails the command.
With the mock bot as `BROWSER_IMAGE` (`mock-bot:dev`) the journey is a deterministic green,
transcript included; with the real bot it drives a dead synthetic meeting to a truthful named
`join_failure`. See `deploy/compose/probe.sh` (a wrapper over `scripts/probe/journey.sh`).

## Secrets and networks (C4 hardening)

The stack has **no default secrets**. `docker compose` refuses to start until every one of these
is set in `deploy/compose/.env` (`make up` / `make dev` generate missing ones with
`openssl rand -hex 32`; `make _seed_env` alone does just that):

| variable | proves / used for | held by |
|---|---|---|
| `ADMIN_TOKEN` | admin-api's admin key (`X-Admin-API-Key`) | admin-api, terminal/dashboard |
| `GATEWAY_IDENTITY_SECRET` | `X-Gateway-Verified`: "the gateway verified this `x-user-*` identity". Whoever holds it can act as any user | gateway, agent-api |
| `ADMIN_API_INTERNAL_SECRET` | admin-api `/internal/*` (`X-Internal-Secret`); admin-api reads it under the name `INTERNAL_API_SECRET` | admin-api, gateway, meeting-api, agent-api, terminal |
| `RUNTIME_CALLBACK_SECRET` | runtime → meeting-api `/runtime/callback` | runtime, meeting-api |
| `MEETING_TOKEN_SECRET` | HS256 key of the per-bot MeetingToken | meeting-api only |
| `DB_PASSWORD` | postgres | postgres, admin-api, meeting-api |
| `MINIO_SECRET_KEY` | minio root password / meeting-api S3 secret | minio, meeting-api |
| `REDIS_PASSWORD` | password of the `vexa-svc` Valkey ACL user (`redis://vexa-svc:${REDIS_PASSWORD}@redis:6379/0`; hex only) | redis, runtime, meeting-api, gateway, agent-api |
| `NEXTAUTH_SECRET` | terminal (and the deprecated dashboard) session/JWT secret | terminal, dashboard |
| `VEXA_DISPATCH_SIGNING_KEY` | agent-api's per-dispatch worker tokens | agent-api |

Flags: `BOT_REDIS_ACL` (default `true`) and `BOT_LEGACY_INTERNAL_SECRET` (default `false`).

meeting-api additionally refuses to boot (outside `DEV_MODE=true`) when a secret still holds one of
the old published placeholders (`changeme`, `vexa-internal-secret`, `postgres`, `vexa-secret-key`, …),
and when it has no MeetingToken key at all. The gateway refuses to boot without an admin-api
internal key (#526).

### What a bot holds (and nothing more)

Bots run a browser on a page chosen by the API caller, so they are treated as hostile. Before the
2026-10 follow-up every bot received `INTERNAL_API_SECRET`, which was also the gateway identity
marker and admin-api's internal key. Now a bot holds exactly:

* **its MeetingToken** (`invocation.token`): HS256 with `MEETING_TOKEN_SECRET`, claims `meeting_id` and
  `connection_id`. `POST /bots/internal/callback/lifecycle` and `POST /internal/recordings/upload`
  (chunks and signal tapes) accept only this token, and only for the token's own session. No
  deployment secret is accepted on either route.
* **a per-meeting Valkey user** (`BOT_REDIS_ACL=true`): `bot-<connection_id>`, created by meeting-api at
  spawn and deleted when the meeting ends. It may `XADD transcription_segments` (write-only),
  `PUBLISH tc:meeting:<id>:mutable`, `SUBSCRIBE bot_commands:meeting:<id>`, `PING`. Everything else
  is `NOPERM`: other meetings, `u:*`, `KEYS`/`SCAN`/`CONFIG`/`ACL`. The `default` user is off.
  Residual: a bot can still append a forged segment naming another meeting to the shared
  `transcription_segments` stream; the collector routes by payload `meeting_id`.
* its STT/TTS tokens and scoped `BOT_S3_*` userdata credentials (unchanged).

**Stock-bot compat (`BOT_LEGACY_INTERNAL_SECRET`, temporary).** The stock upstream image
(`vexaai/vexa-bot`, v0.12.x) never reads `invocation.token`. It authenticates with
`invocation.internalSecret` only, as `x-internal-secret` on lifecycle callbacks and as a `Bearer` on
uploads. With the flag `true`, meeting-api fills `internalSecret` with **the bot's own MeetingToken**
(not a shared secret) and accepts it from `x-internal-secret`, with the same signature and session
binding. Keep it `false` when `BROWSER_IMAGE` is built from this repo. **Remove the flag once no
stock bot images are in use.**

**Fallbacks.** If a split secret is unset, the code falls back to the old `INTERNAL_API_SECRET`
(and `MEETING_TOKEN_SECRET` to `ADMIN_TOKEN`), logging a deprecation warning, so helm / lite keep
booting. Compose sets every split secret explicitly. Bots never receive any of these values in any
mode.

**Not on main yet:** meeting-api on `main` does not verify the gateway identity marker on `x-user-*`
requests (the `fpn` branch does, `meeting_api/internal_auth.py`). Until that lands, anything that can
reach meeting-api on the `vexa` network can assert an identity. Bots are on `bots`, which does reach
meeting-api, so this is a separate open item.

**Two networks.** `vexa` is the control plane (every long-running service; the runtime with the
Docker socket, postgres, minio, admin-api and agent-api live *only* here). `bots` is the workloads
network: the runtime spawns every meeting bot and agent worker onto it (`DOCKER_NETWORK=
${COMPOSE_PROJECT_NAME}_bots`), and it carries only `redis` and `meeting-api`. A compromised bot can
therefore reach neither the runtime API nor the databases nor the identity plane. Both networks keep
internet egress (bots join meetings, workers call model APIs).

### Rolling an existing stack onto this layout

`DOCKER_NETWORK` is also the discriminator the runtime uses to **re-adopt live workloads** after a
restart: bots started on `${project}_vexa` will not be re-adopted once it points at `${project}_bots`.
Running bots also hold the old shared secret and Redis URL, which the new stack refuses. Do the roll
in a bot-free window:

1. Add the new variables to `.env` (`make _seed_env`). Generate new values; never reuse the old
   `INTERNAL_API_SECRET`, which every bot has held. Set `BOT_LEGACY_INTERNAL_SECRET=true` if
   `BROWSER_IMAGE` is a stock image.
2. Stop every running bot (`GET /bots/status` must be empty) and let agent workers finish.
3. Roll meeting-api, runtime, gateway and agent-api together, then `docker compose up -d`. Compose
   creates `bots`, recreates `redis` with ACL users (the data volume is kept), and re-attaches `redis`
   and `meeting-api` to both networks.
4. Verify:
   * `docker network inspect <project>_bots` lists only redis and meeting-api.
   * `valkey-cli --user vexa-svc --pass … ACL LIST` shows `default off`.
   * Send a bot and confirm it reaches `joining`, and that its `bot-<connection_id>` user disappears
     after it ends.

Follow-up (not done here): the runtime still mounts `/var/run/docker.sock`. On `fpn` it goes through
an allowlisting socket proxy (#20); port that here.

