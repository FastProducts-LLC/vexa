"""The runtime HTTP API — realizes runtime.v1's operations (create/get/list/stop/destroy) + delivers
RuntimeEvents to each workload's callbackUrl. A thin FastAPI surface over the kernel; the control plane
(meeting-api, agent-api) calls this to spawn workloads. The API IS runtime.v1's operation surface.

O-RT-2 additions:
  • /health — 200 when the backend + store are reachable and the scheduler (if wired) is live; 503 otherwise.
  • durable callback delivery — events go through a CallbackQueue (enqueue + retry-until-ack), replacing
    the old fire-once POST. A receiver that 500s is retried on the next sweep until it acks."""
from __future__ import annotations

import hmac
import logging
import os
from typing import Callable, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .callbacks import CallbackQueue
from .kernel import QuotaExceeded, Runtime, StartFailed
from .models import RuntimeEvent, StopReason, WorkloadSpec
from .obs import TraceMiddleware, log_event
from .scheduler import Scheduler

# A health probe returns True when its dependency is reachable. Probes must never raise.
HealthCheck = Callable[[], bool]

logger = logging.getLogger("runtime_kernel.api")

# ── control-plane auth (security finding C5) ────────────────────────────────────────────────────
# The runtime holds the host Docker socket, so its API is a host-root primitive: anything that can
# POST /workloads or /schedule can spawn containers / fire arbitrary HTTP from inside the stack.
# Every route except /health requires ``Authorization: Bearer $RUNTIME_API_TOKEN`` (constant-time
# compare). FAIL CLOSED: with no token configured every protected route answers 503 — the runtime
# never serves an open control plane by accident. ``RUNTIME_API_INSECURE_NO_AUTH=1`` is the explicit,
# loudly-logged opt-out for unit tests / throwaway local stacks only.
TOKEN_ENV = "RUNTIME_API_TOKEN"
INSECURE_ENV = "RUNTIME_API_INSECURE_NO_AUTH"
OPEN_PATHS = frozenset({"/health"})
_UNSET = object()


def _resolve_token(api_token) -> tuple[Optional[str], bool]:
    """(token, insecure_opt_out). An explicit ``api_token`` argument wins over the env."""
    token = os.getenv(TOKEN_ENV, "") if api_token is _UNSET else (api_token or "")
    token = token.strip()
    insecure = os.getenv(INSECURE_ENV, "").strip().lower() in ("1", "true", "yes")
    return (token or None), (insecure and not token)


def _bearer(request: Request) -> str:
    raw = request.headers.get("authorization", "")
    scheme, _, value = raw.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


class StopBody(BaseModel):
    reason: Optional[StopReason] = None


def _queue_deliver(rt: Runtime, queue: CallbackQueue) -> Callable[[RuntimeEvent], None]:
    """Durable delivery: enqueue each event for the workload's callbackUrl. The queue posts
    immediately and keeps anything the receiver hasn't acked, so a later sweep() retries it."""
    def deliver(ev: RuntimeEvent) -> None:
        record = rt.store.get(ev.workloadId)
        url = record.spec.callbackUrl if record else None
        if not url:
            return
        queue.enqueue(url, ev.model_dump(exclude_none=True))
    return deliver


def _default_health_checks(rt: Runtime) -> dict[str, HealthCheck]:
    def backend_ok() -> bool:
        return bool(getattr(rt.backend, "name", None))

    def store_ok() -> bool:
        try:
            rt.store.list()
            return True
        except Exception:
            return False

    return {"backend": backend_ok, "store": store_ok}


def create_app(
    runtime: Optional[Runtime] = None,
    deliver: Optional[Callable[[RuntimeEvent], None]] = None,
    callback_queue: Optional[CallbackQueue] = None,
    health_checks: Optional[dict[str, HealthCheck]] = None,
    scheduler: Optional[Scheduler] = None,
    api_token=_UNSET,
) -> FastAPI:
    rt = runtime or Runtime()
    queue = callback_queue or CallbackQueue()
    sink = deliver or _queue_deliver(rt, queue)
    prior = rt.on_event
    rt.on_event = lambda ev: (prior(ev), sink(ev))  # chain: preserve any existing handler, then deliver

    checks: dict[str, HealthCheck] = dict(_default_health_checks(rt))
    if scheduler is not None:
        # The durable cron is live: probe by listing pending jobs (touches redis without firing).
        checks["scheduler"] = lambda: (scheduler.list(limit=1) is not None)
    if health_checks:
        checks.update(health_checks)

    app = FastAPI(title="vexa-runtime", version="0.12.0")
    app.state.runtime = rt
    app.state.callback_queue = queue
    app.state.scheduler = scheduler
    # Reuse the control-plane caller's X-Trace-Id so workload-spawn logs (logevent.v1) join
    # the same trace as the meeting-api/agent-api request that asked for the workload.
    app.add_middleware(TraceMiddleware)
    dump = lambda s: s.model_dump(exclude_none=True)

    token, insecure = _resolve_token(api_token)
    if token is None and not insecure:
        logger.critical(
            "%s is not set: the runtime control plane will REJECT every request except /health "
            "(fail closed). Set the same token in the runtime and in every caller (meeting-api).",
            TOKEN_ENV,
        )
    elif insecure:
        logger.warning("%s=1: the runtime control plane is UNAUTHENTICATED (tests/local only)", INSECURE_ENV)
    app.state.auth_mode = "token" if token else ("insecure" if insecure else "unconfigured")
    expected = token.encode() if token else b""

    @app.middleware("http")
    async def _require_token(request: Request, call_next):
        if request.url.path in OPEN_PATHS or insecure:
            return await call_next(request)
        if not token:
            return JSONResponse({"detail": "runtime API token not configured"}, status_code=503)
        presented = _bearer(request).encode()
        if not hmac.compare_digest(presented, expected):
            return JSONResponse(
                {"detail": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    @app.get("/health")
    def health():
        results = {}
        for name, probe in checks.items():
            try:
                results[name] = bool(probe())
            except Exception:
                results[name] = False
        healthy = all(results.values())
        # ADDITIVE config.v1 rows (ADR-0026): the declared capability tri-states (scheduler ·
        # bot_spawn · agent_spawn · model_inference, incl. the credentials-file probe). They never
        # affect `status`/`checks` or the status code — existing consumers keep working; an
        # unconfigured capability degrades a FEATURE, not the process.
        from .config_preflight import capability_health

        body = {"status": "ok" if healthy else "degraded", "checks": results,
                "capabilities": capability_health()}
        return JSONResponse(body, status_code=200 if healthy else 503)

    @app.post("/workloads", status_code=201)
    def create(spec: WorkloadSpec):
        # Reject a spec whose env would make the substrate bind a host path / foreign volume / the
        # docker socket (C5) — BEFORE anything is persisted or spawned. Backend-specific.
        validate_env = getattr(rt.backend, "validate_env", None)
        if validate_env is not None:
            try:
                validate_env(spec.env)
            except ValueError as e:
                log_event(
                    "workload_spec_rejected",
                    audience="system",
                    level="warning",
                    span="workloads.create",
                    fields={"workload_id": spec.workloadId, "profile": spec.profile, "error": str(e)},
                )
                raise HTTPException(status_code=400, detail=str(e))
        try:
            status = rt.create(spec)
            # SYSTEM event: a workload was spawned for the calling control-plane request. Only logged
            # on the success path — a workload that failed to START raises StartFailed below, so
            # `workload_spawned` never fires over a dead workload (#718).
            log_event(
                "workload_spawned",
                audience="system",
                span="workloads.create",
                fields={"workload_id": spec.workloadId, "profile": spec.profile},
            )
            return dump(status)
        except StartFailed as e:
            # The backend could not start the workload (e.g. the image is absent). The kernel already
            # recorded the honest stopped/start_failed status + emitted its event; answer a non-201
            # that NAMES the cause (the existing {detail} error convention — no runtime.v1 shape is
            # sealed for errors, same as the 429/400 branches below) so no caller reads a false 201.
            log_event(
                "workload_spawn_failed",
                audience="system",
                level="error",
                span="workloads.create",
                fields={"workload_id": spec.workloadId, "profile": spec.profile, "error": str(e)},
            )
            raise HTTPException(status_code=502, detail=str(e))
        except QuotaExceeded as e:
            log_event(
                "workload_quota_exceeded",
                audience="user",
                level="warning",
                span="workloads.create",
                fields={"workload_id": spec.workloadId, "profile": spec.profile, "error": str(e)},
            )
            raise HTTPException(status_code=429, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.get("/workloads")
    def list_workloads():
        return [dump(s) for s in rt.list()]

    @app.get("/workloads/{workload_id}")
    def get(workload_id: str):
        try:
            return dump(rt.get(workload_id))
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown workload")

    @app.post("/workloads/{workload_id}/stop")
    def stop(workload_id: str, body: StopBody = StopBody()):
        try:
            return dump(rt.stop(workload_id, body.reason or StopReason.stopped))
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown workload")

    @app.delete("/workloads/{workload_id}")
    def destroy(workload_id: str):
        try:
            return dump(rt.destroy(workload_id))
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown workload")

    # ── schedule.v1 — the durable cron over HTTP (the control plane registers routine jobs here) ──
    def _require_scheduler() -> Scheduler:
        if scheduler is None:
            raise HTTPException(status_code=503, detail="scheduler not wired")
        return scheduler

    @app.post("/schedule", status_code=201)
    def schedule_job(spec: dict):
        """Register a schedule.v1 job (one-shot ``execute_at`` or re-arming ``cron``). The job's
        ``request`` is the HTTP call fired when due — for a routine, a unit.v1 Invocation POSTed to
        agent-api ``/invocations``. The runtime knows nothing about units (clean isolation)."""
        sched = _require_scheduler()
        try:
            return sched.schedule(spec)
        except ValueError as e:  # missing request.url / execute_at|cron — fail loud (P18)
            raise HTTPException(status_code=400, detail=str(e))

    @app.get("/schedule")
    def list_jobs(status: Optional[str] = None, limit: int = 50):
        return _require_scheduler().list(status=status, limit=limit)

    @app.delete("/schedule/{job_id}")
    def cancel_job(job_id: str):
        cancelled = _require_scheduler().cancel(job_id)
        if cancelled is None:
            raise HTTPException(status_code=404, detail="unknown job")
        return cancelled

    return app


# ── ASGI entrypoint (P4) ─────────────────────────────────────────────────────────────────────
# ``uvicorn runtime_kernel.api:app`` (the compose CMD) resolves this. Built LAZILY via PEP 562 so
# importing this module never wires a DockerBackend — the app is constructed only when uvicorn
# touches ``api.app`` at startup, with the real Docker backend + env-driven profile registry.
def __getattr__(name: str):
    if name == "app":
        from .__main__ import build_production_app

        return build_production_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
