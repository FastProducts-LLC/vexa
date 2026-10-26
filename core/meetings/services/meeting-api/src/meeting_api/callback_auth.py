"""Internal callback trust edge — who may POST lifecycle / workload events into meeting-api.

Two sinks advance a meeting's state machine from the network:

  * ``POST /bots/internal/callback/lifecycle`` — the bot's own ``lifecycle.v1`` callback
    (joining → active → completed/failed). The bot image already sends
    ``x-internal-secret: <invocation.internalSecret>`` (0.11 convention) and carries its
    MeetingToken (``invocation.token``, HS256 signed with ``ADMIN_TOKEN``), which it now also
    presents as ``Authorization: Bearer``. Either credential is accepted here so a bot built
    before the bearer was added keeps working, and a later change can stop handing bots the
    deployment-wide internal secret at all.
  * ``POST /runtime/callback`` — the runtime kernel's workload-level callback (state/terminal
    events, consumed as destroy evidence). The kernel stamps ``x-internal-secret`` with its own
    ``INTERNAL_API_SECRET``.

Before this guard both routes accepted ANY body from anything that could open a socket to
meeting-api (every container on the compose network, including spawned bots and agent workers):
a forged ``completed`` ended a live meeting, a forged ``destroyed`` failed it, a forged ``active``
confused the reaper. Finding C4 of the 2026-10 audit.

Fail closed: outside ``DEV_MODE=true`` a route whose credential is not configured answers 503;
a request that presents nothing valid answers 403. ``DEV_MODE=true`` keeps the pre-hardening
open edge for local development and the in-process test suite only.
"""
from __future__ import annotations

import hmac
import os
from typing import Any, Mapping, Optional, Tuple

INTERNAL_SECRET_HEADER = "x-internal-secret"
AUTHORIZATION_HEADER = "authorization"

Verdict = Optional[Tuple[int, str]]  # None → proceed; else (http_status, detail)


def dev_mode(env: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if env is None else env
    return (env.get("DEV_MODE") or "false").strip().lower() == "true"


def _internal_secret(env: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if env is None else env
    return (env.get("INTERNAL_API_SECRET") or "").strip()


def _eq(provided: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _bearer(headers: Mapping[str, str]) -> str:
    auth = (headers.get(AUTHORIZATION_HEADER) or "").strip()
    if auth[:7].lower() != "bearer ":
        return ""
    return auth[7:].strip()


def check_runtime_callback(headers: Mapping[str, str], *, env: Optional[Mapping[str, str]] = None) -> Verdict:
    """The runtime kernel must present ``x-internal-secret == INTERNAL_API_SECRET``."""
    if dev_mode(env):
        return None  # the pre-hardening open edge — local development / in-process tests only
    secret = _internal_secret(env)
    if not secret:
        return 503, "INTERNAL_API_SECRET not configured; runtime callbacks are refused"
    if _eq(headers.get(INTERNAL_SECRET_HEADER) or "", secret):
        return None
    return 403, "runtime callback requires a valid X-Internal-Secret"


def check_bot_callback(
    headers: Mapping[str, str],
    *,
    token_secret: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Verdict:
    """The bot must present EITHER ``x-internal-secret == INTERNAL_API_SECRET`` OR a valid
    MeetingToken bearer (signed with ``token_secret`` / ``ADMIN_TOKEN``, unexpired, issued by
    meeting-api). Both credentials ride in the bot's own invocation, nothing else on the network
    holds them."""
    env_map = os.environ if env is None else env
    if dev_mode(env_map):
        return None  # the pre-hardening open edge — local development / in-process tests only
    secret = _internal_secret(env_map)
    token_secret = (token_secret if token_secret is not None else env_map.get("ADMIN_TOKEN") or "").strip()
    if not secret and not token_secret:
        return 503, "neither INTERNAL_API_SECRET nor ADMIN_TOKEN is configured; bot callbacks are refused"
    provided = headers.get(INTERNAL_SECRET_HEADER) or ""
    if provided and _eq(provided, secret):
        return None
    token = _bearer(headers)
    if token and token_secret:
        try:
            claims = verify_meeting_token(token, secret=token_secret)
        except ValueError as e:
            return 403, f"invalid MeetingToken: {e}"
        if claims.get("iss") not in (None, "meeting-api"):
            return 403, "invalid MeetingToken: issuer"
        return None
    return 403, "bot callback requires X-Internal-Secret or a MeetingToken bearer"


def verify_meeting_token(token: str, *, secret: str) -> dict[str, Any]:
    """The recordings-upload verifier (HS256 + exp), shared so both edges accept the same token."""
    from .recordings.service import _verify_meeting_token

    return _verify_meeting_token(token, secret=secret)
