# deploy/compose — the v0.12 control-plane stack (P4)

`docker-compose.yml` brings up the v0.12 control plane: the infra (`postgres:17-alpine`,
`redis:7-alpine`, `minio` + `minio-init`) and the long-running services below, each building its own
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

## Secrets and networks (C4 hardening)

The stack has **no default secrets**. `docker compose` refuses to start until every one of these
is set in `deploy/compose/.env` (`make up` generates missing ones with `openssl rand -hex 32`;
`make _seed_env` alone does just that). The fpn overlay (`docker-compose.fpn.yml`) requires the
same values.

| variable | used by |
|---|---|
| `ADMIN_TOKEN` | admin-api auth, MeetingToken signing (meeting-api), terminal admin key |
| `INTERNAL_API_SECRET` | the internal service edge: gateway ⇄ admin-api / meeting-api / agent-api, the runtime's workload callbacks, the bot's lifecycle callback |
| `RUNTIME_API_TOKEN` | the runtime control-plane bearer (C5) |
| `DB_PASSWORD` | postgres, admin-api, meeting-api |
| `MINIO_SECRET_KEY` | minio root password, meeting-api S3 secret |
| `REDIS_PASSWORD` | redis `--requirepass`; every `REDIS_URL` / `VEXA_REDIS_URL` is `redis://:${REDIS_PASSWORD}@redis:6379/0` (URL-safe characters only — use hex) |
| `NEXTAUTH_SECRET` | terminal session/JWT secret |
| `VEXA_DISPATCH_SIGNING_KEY` | agent-api's per-dispatch worker tokens |

meeting-api additionally refuses to boot (outside `DEV_MODE=true`) when a secret still holds one of
the old published placeholders (`changeme`, `vexa-internal-secret`, `postgres`, `vexa-secret-key`,
`vexa-runtime-dev-token`, …).

**Internal callbacks are authenticated.** `POST /runtime/callback` needs
`X-Internal-Secret == INTERNAL_API_SECRET` (the runtime stamps it on every RuntimeEvent);
`POST /bots/internal/callback/lifecycle` needs the bot's `X-Internal-Secret` *or* its MeetingToken
as `Authorization: Bearer`. Both fail closed (503) when unconfigured outside `DEV_MODE`.

**Two networks.** `vexa` is the control plane (every long-running service; the runtime with the
Docker socket, postgres, minio, admin-api, agent-api and the gateway live *only* here). `bots` is the
workloads network: the runtime spawns every meeting bot and agent worker onto it (`DOCKER_NETWORK=
${COMPOSE_PROJECT_NAME}_bots`), and it carries only `redis`, `meeting-api` and, on fpn, the
`stt-hosted` / `tts-cartesia` shims. A compromised bot can therefore reach neither the runtime API nor
the databases nor the identity plane. Both networks keep internet egress (bots join meetings, workers
call model APIs).

### Rolling an existing stack onto this layout

`DOCKER_NETWORK` is also the discriminator the runtime uses to **re-adopt live workloads** after a
restart: bots started on `${project}_vexa` will not be re-adopted once it points at `${project}_bots`.
Do the roll in a bot-free window:

1. Add the new variables to `.env` (`make _seed_env`), and pick a hex `REDIS_PASSWORD`.
2. Stop every running bot (`GET /bots/status` must be empty) and let agent workers finish.
3. `docker compose -f docker-compose.yml -f docker-compose.fpn.yml up -d` — compose creates `bots`,
   recreates `redis` with `--requirepass` (the data volume is kept), and re-attaches `redis`,
   `meeting-api`, `stt-hosted` and `tts-cartesia` to both networks.
4. Verify: `docker network inspect <project>_bots` lists only those four services; send a bot and
   confirm it appears on `_bots` and reaches `joining`.

Follow-up (not done here): the runtime still mounts `/var/run/docker.sock`; fronting it with a
socket proxy that allows only the container create/inspect/stop/remove calls the kernel uses would
remove the last host-takeover path from the control plane.

