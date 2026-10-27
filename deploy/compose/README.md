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

On fpn the runtime no longer mounts the Docker socket: it reaches Docker through the allowlisting
`docker-proxy` (see *fpn Docker proxy* below), on its own internal network that bots are not on.

## fpn Docker proxy

The fpn overlay routes runtime through `DOCKER_HOST=tcp://docker-proxy:2375` on the internal
`vexa-v012_docker_api` network. Only `docker-proxy` mounts the Docker socket; it publishes no
ports. Runtime's volume list uses `!override` (Compose 2.24.4+) to retain the credential-probe
mount while removing the socket, and `group_add: !reset []` removes the Docker group.
Unix socket URLs remain supported; TCP URLs use HTTP and HTTP(S) URLs pass through unchanged.

The pinned `tecnativa/docker-socket-proxy:0.3.0` image runs HAProxy directly with the repo-owned
[`docker-proxy/haproxy.cfg`](docker-proxy/haproxy.cfg), bypassing the image's template entrypoint.
Section flags cannot enforce this boundary: `CONTAINERS=1` also permits nested container
operations such as `POST /containers/<id>/exec`, even with `EXEC=0`. No section flags are used.
The explicit method/path allowlist permits only:

| Method | Path | Runtime use |
| --- | --- | --- |
| GET | `/containers/json` | Discover workloads |
| POST | `/containers/create` | Create workloads |
| GET | `/containers/<id-or-name>/json` | Find workloads and inspect exit status |
| POST | `/containers/<id-or-name>/(start\|stop\|kill)` | Drive workload lifecycle |
| DELETE | `/containers/<id-or-name>` | Cleanup workloads and name conflicts |
| GET | `/images/<ref>/json` | Check image availability |
| POST | `/images/create` | Pull worker images |

Each path accepts an optional `/v<version>` prefix. HAProxy's `path` excludes the query string,
so list filters, pull parameters and `?force=true` do not affect matching. Container identifiers
accept letters, digits, underscores, dots and hyphens; image references also allow `/` and `:`.
All other requests receive **403**, including exec, logs, archive, attach, restart, volumes,
networks, system metadata, events, builds and image pushes/deletes.

The proxy does not inspect request bodies; runtime's `_assert_safe_host_config` and mount
allowlist remain the guards against privileged/host-mounted workloads. Relocating vexa off
the shared droplet is the complete fix.

Both the socket and configuration mounts are read-only. The proxy uses a read-only root
filesystem, a small `/run` tmpfs, no-new-privileges and no Linux capabilities. It keeps the
image's default root identity to access the socket; binding port 2375 requires no privileged-port
capability. The server timeout is 600 seconds to accommodate image pulls.

From the repo root, `bash deploy/compose/tests/check_fpn_config.sh` checks the merged Compose
configuration without starting services. `bash deploy/compose/tests/probe_docker_proxy.sh`
checks allowed and denied requests against a real daemon, including versioned paths and queries.
Run the probe only on an explicitly authorized local test daemon: it pulls Alpine, starts a
temporary proxy with a loopback port, creates a test workload and removes its labeled containers.
Expected output is `CHECK-FPN-CONFIG OK` and `PROBE OK (<n> checks)`, respectively.

The running stack is managed by hand under Compose project `vexa-v012`. Rollout must include
a runtime image containing the URL helper change and the merged base + fpn overlay. On the
host after rollout, replace `<runtime>` with that project's runtime container name and
`<id>` with a test container ID:

```bash
docker exec <runtime> python -c "import os,requests;print(requests.get(os.environ['DOCKER_HOST'].replace('tcp://','http://')+'/containers/json').status_code)"
# Expected: 200
docker exec <runtime> python -c "import os,requests;print(requests.get(os.environ['DOCKER_HOST'].replace('tcp://','http://')+'/volumes').status_code)"
# Expected: 403
docker exec <runtime> python -c "import os,requests;print(requests.post(os.environ['DOCKER_HOST'].replace('tcp://','http://')+'/containers/<id>/exec',json={'Cmd':['true']}).status_code)"
# Expected: 403
```
