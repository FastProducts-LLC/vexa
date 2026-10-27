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
`REDIS_URL`, `ADMIN_TOKEN`, the C4 split secrets, `MINIO_*`, `BROWSER_IMAGE`/`AGENT_IMAGE`,
`DOCKER_GID`, `*_HOST_PORT`).

## Secrets and networks (C4 hardening)

The stack has **no default secrets**. `docker compose` refuses to start until every one of these
is set in `deploy/compose/.env` (`make up` generates missing ones with `openssl rand -hex 32`;
`make _seed_env` alone does just that). The fpn overlay (`docker-compose.fpn.yml`) requires the
same values.

| variable | proves / used for | held by |
|---|---|---|
| `ADMIN_TOKEN` | admin-api's admin key (`X-Admin-API-Key`) | admin-api, terminal |
| `GATEWAY_IDENTITY_SECRET` | `X-Gateway-Verified`: "the gateway verified this `x-user-*` identity". Whoever holds it can act as any user | gateway, meeting-api, agent-api |
| `ADMIN_API_INTERNAL_SECRET` | admin-api `/internal/*` (`X-Internal-Secret`); admin-api reads it under the name `INTERNAL_API_SECRET` | admin-api, gateway, meeting-api, agent-api, terminal |
| `RUNTIME_CALLBACK_SECRET` | runtime → meeting-api `/runtime/callback` | runtime, meeting-api |
| `MEETING_TOKEN_SECRET` | HS256 key of the per-bot MeetingToken | meeting-api only |
| `RUNTIME_API_TOKEN` | the runtime control-plane bearer (C5) | runtime, meeting-api, agent-api |
| `DB_PASSWORD` | postgres | postgres, admin-api, meeting-api |
| `MINIO_SECRET_KEY` | minio root password / meeting-api S3 secret | minio, meeting-api |
| `REDIS_PASSWORD` | password of the `vexa-svc` Redis ACL user (`redis://vexa-svc:${REDIS_PASSWORD}@redis:6379/0`; hex only — it is embedded in a URL) | redis, runtime, meeting-api, gateway, agent-api |
| `NEXTAUTH_SECRET` | terminal session/JWT secret | terminal |
| `VEXA_DISPATCH_SIGNING_KEY` | agent-api's per-dispatch worker tokens | agent-api |

Flags: `BOT_REDIS_ACL` (default `true`) and `BOT_LEGACY_INTERNAL_SECRET` (default `false`; **required**
in the fpn overlay — see below).

meeting-api additionally refuses to boot (outside `DEV_MODE=true`) when a secret still holds one of
the old published placeholders (`changeme`, `vexa-internal-secret`, `postgres`, `vexa-secret-key`,
`vexa-runtime-dev-token`, …), and when it has no MeetingToken key at all.

### What a bot holds (and nothing more)

Spawned bots run a browser on a page chosen by the API caller, so they are treated as hostile. Before
the 2026-10 follow-up every bot received `INTERNAL_API_SECRET`, which was also the gateway identity
marker and admin-api's internal key: one compromised bot could read every tenant's transcripts and
recordings. Now a bot holds exactly:

* **its MeetingToken** (`invocation.token`): HS256 with `MEETING_TOKEN_SECRET`, claims `meeting_id` and
  `connection_id`. `POST /bots/internal/callback/lifecycle` and `POST /internal/recordings/upload`
  accept only this token, and only for the token's own session (`connection_id` must match the
  event / upload). Token A cannot complete, fail or upload into session B. No deployment secret is
  accepted on either route.
* **a per-meeting Redis user** (`BOT_REDIS_ACL=true`): `bot-<connection_id>`, created by meeting-api at
  spawn with `ACL SETUSER` and deleted when the meeting ends. It may `XADD transcription_segments`
  (write-only), `PUBLISH tc:meeting:<id>:mutable`, `SUBSCRIBE bot_commands:meeting:<id>`, `PING`.
  Everything else is `NOPERM`: other meetings' channels, `u:*`, reads of the shared feed,
  `KEYS`/`SCAN`/`CONFIG`/`ACL`/`FLUSH*`. The `default` Redis user is off. Residual: the collector
  routes `transcription_segments` entries by the `meeting_id` in the payload, so a bot can still append
  a forged segment for another meeting to that one shared stream. Closing that needs per-meeting
  streams or signed payloads in the collector.
* its STT/TTS shim tokens (unchanged).

**Stock-bot compat (`BOT_LEGACY_INTERNAL_SECRET`, temporary).** The stock upstream image
(`vexaai/vexa-bot:v0.12.0-rc.10`, `:v012` = v0.12.28) never reads `invocation.token`. It sends
`x-internal-secret: <invocation.internalSecret>` on lifecycle callbacks and
`Authorization: Bearer <invocation.internalSecret>` on recording uploads. With the flag `true`,
meeting-api fills `internalSecret` with **the bot's own MeetingToken** (not a shared secret) and
accepts it from `x-internal-secret`, with the same signature and session binding. A stock bot's
credential therefore opens only its own lifecycle and upload. It is refused as an
`X-Gateway-Verified` marker, on admin-api `/internal/*`, and on `/runtime/callback`. Set it `true`
while `BROWSER_IMAGE` is a stock image and `false` for a bot built from this repo (which sends the
bearer). **Remove the flag once the fork ships its own bot image.**

**Fallbacks.** If a split secret is unset, the code falls back to the old `INTERNAL_API_SECRET`
(and `MEETING_TOKEN_SECRET` to `ADMIN_TOKEN`), logging a deprecation warning, so helm / lite / an old
`.env` keep booting. Compose sets every split secret explicitly and passes `INTERNAL_API_SECRET` to no
service. Even in fallback mode bots never receive any of these values.

**Two networks.** `vexa` is the control plane: every long-running service. The runtime, postgres,
minio, admin-api, agent-api and the gateway live *only* here. `bots` is the workloads network: the
runtime spawns every meeting bot and agent worker onto it (`DOCKER_NETWORK=
${COMPOSE_PROJECT_NAME}_bots`), and it carries only `redis`, `meeting-api` and, on fpn, the
`stt-hosted` / `tts-cartesia` shims. A compromised bot can therefore reach neither the runtime API nor
the databases nor the identity plane. Both networks keep internet egress (bots join meetings, workers
call model APIs).

On fpn the runtime no longer mounts the Docker socket: it reaches Docker through the allowlisting
`docker-proxy` (see *fpn Docker proxy* below), on its own internal network that bots are not on.

### Rolling an existing stack onto this layout

`DOCKER_NETWORK` is also the discriminator the runtime uses to **re-adopt live workloads** after a
restart: bots started on `${project}_vexa` will not be re-adopted once it points at `${project}_bots`.
Bots already running also hold the old `INTERNAL_API_SECRET` and the old Redis URL, which the new
meeting-api / Redis refuse. Do the roll in a **bot-free window**:

1. Add the new variables to `.env` (`make _seed_env` generates the empty ones). All of them are new
   random values: `GATEWAY_IDENTITY_SECRET`, `ADMIN_API_INTERNAL_SECRET`, `RUNTIME_CALLBACK_SECRET`,
   `MEETING_TOKEN_SECRET`, `REDIS_PASSWORD` (hex), `NEXTAUTH_SECRET`, `VEXA_DISPATCH_SIGNING_KEY`. Do
   **not** reuse the current `INTERNAL_API_SECRET` for any of them: every bot ever spawned has held
   that value. Set `BOT_LEGACY_INTERNAL_SECRET=true` while `BROWSER_IMAGE` is a stock image.
2. Stop every running bot (`GET /bots/status` must be empty) and let agent workers finish.
3. Roll meeting-api, runtime and gateway images from the same `FPN_VEXA_SHA` together: an old gateway
   would stamp a marker the new meeting-api refuses, and an old runtime would stamp the wrong callback
   secret. Then `docker compose -f docker-compose.yml -f docker-compose.fpn.yml up -d`. Compose creates
   `bots`, recreates `redis` with ACL users (the data volume is kept), and re-attaches `redis`,
   `meeting-api`, `stt-hosted` and `tts-cartesia` to both networks. admin-api (still the stock image on
   fpn) gets `ADMIN_API_INTERNAL_SECRET` under its `INTERNAL_API_SECRET` name, so it needs no new image.
4. Verify:
   * `docker network inspect <project>_bots` lists only those four services.
   * `redis-cli --user vexa-svc --pass … ACL LIST` shows `default off`.
   * Send a bot and confirm it reaches `joining`. While it runs, `ACL LIST` shows a `bot-<connection_id>`
     user, and it disappears after the bot ends.
   * meeting-api logs no `lifecycle_callback_refused` / `runtime_callback_refused`.
5. Remove `INTERNAL_API_SECRET` from `.env` once nothing external still uses it.

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
