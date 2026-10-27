"""Internal callback trust edge — who may POST lifecycle / workload events into meeting-api.

Two sinks advance a meeting's state machine from the network:

  * ``POST /bots/internal/callback/lifecycle`` — the bot's own ``lifecycle.v1`` callback
    (joining → active → completed/failed). The bot authenticates with ITS MeetingToken only
    (HS256, ``MEETING_TOKEN_SECRET``, minted per spawn) and the token is BOUND to the bot's session:
    its ``connection_id`` claim must equal the event's ``connection_id``. One bot's token therefore
    cannot advance — complete, fail, re-activate — any other meeting.

    Credential carriers: ``Authorization: Bearer <MeetingToken>`` (the fork-built bot). With
    ``BOT_LEGACY_INTERNAL_SECRET=true`` ALSO ``x-internal-secret: <MeetingToken>`` — the STOCK
    upstream bot image only sends ``x-internal-secret: <invocation.internalSecret>``, and in compat
    mode meeting-api puts the bot's own MeetingToken in that slot (see ``trust_secrets``). No shared
    deployment secret is accepted on this route in any mode.
  * ``POST /runtime/callback`` — the runtime kernel's workload-level callback (state/terminal
    events, consumed as destroy evidence). The kernel stamps ``x-internal-secret`` with
    ``RUNTIME_CALLBACK_SECRET`` — a value only runtime + meeting-api hold (not the gateway identity
    marker, not admin-api's internal key, never a bot).

Before C4 both routes accepted ANY body from anything that could open a socket to meeting-api, and
the bot-side credential was ``INTERNAL_API_SECRET`` — the same value that is the gateway identity
marker and admin-api's internal key (2026-10 deep audit, finding #2).

Fail closed: outside ``DEV_MODE=true`` a route whose credential is not configured answers 503;
a request that presents nothing valid (or a token bound to another session) answers 403.
``DEV_MODE=true`` keeps the pre-hardening open edge for local development and the in-process
test suite only.
"""
from __future__ import annotations

import hmac
import os
from typing import Any, Mapping, Optional, Tuple

from .trust_secrets import bot_legacy_internal_secret, meeting_token_secret, runtime_callback_secret

INTERNAL_SECRET_HEADER = "x-internal-secret"
AUTHORIZATION_HEADER = "authorization"

Verdict = Optional[Tuple[int, str]]  # None → proceed; else (http_status, detail)


def dev_mode(env: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if env is None else env
    return (env.get("DEV_MODE") or "false").strip().lower() == "true"


def _eq(provided: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _bearer(headers: Mapping[str, str]) -> str:
    auth = (headers.get(AUTHORIZATION_HEADER) or "").strip()
    if auth[:7].lower() != "bearer ":
        return ""
    return auth[7:].strip()


def check_runtime_callback(headers: Mapping[str, str], *, env: Optional[Mapping[str, str]] = None) -> Verdict:
    """The runtime kernel must present ``x-internal-secret == RUNTIME_CALLBACK_SECRET``."""
    if dev_mode(env):
        return None  # the pre-hardening open edge — local development / in-process tests only
    secret = runtime_callback_secret(env)
    if not secret:
        return 503, "RUNTIME_CALLBACK_SECRET not configured; runtime callbacks are refused"
    if _eq(headers.get(INTERNAL_SECRET_HEADER) or "", secret):
        return None
    return 403, "runtime callback requires a valid X-Internal-Secret"


def verify_bot_credential(
    headers: Mapping[str, str],
    *,
    token_secret: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Tuple[Verdict, Optional[dict[str, Any]]]:
    """Step 1 (before the body is read): the request must carry a valid MeetingToken.

    Returns ``(None, claims)`` on success, ``(verdict, None)`` on refusal, and ``(None, None)`` in
    DEV_MODE (open edge, no binding)."""
    env_map = os.environ if env is None else env
    if dev_mode(env_map):
        return None, None
    key = (token_secret if token_secret is not None else meeting_token_secret(env_map)).strip()
    if not key:
        return (503, "MEETING_TOKEN_SECRET not configured; bot callbacks are refused"), None
    candidates = []
    bearer = _bearer(headers)
    if bearer:
        candidates.append(bearer)
    if bot_legacy_internal_secret(env_map):
        legacy = (headers.get(INTERNAL_SECRET_HEADER) or "").strip()
        if legacy:
            candidates.append(legacy)
    if not candidates:
        return (403, "bot callback requires the bot's MeetingToken"), None
    last_error = "invalid MeetingToken"
    for token in candidates:
        try:
            claims = verify_meeting_token(token, secret=key)
        except ValueError as e:
            last_error = f"invalid MeetingToken: {e}"
            continue
        return None, claims
    return (403, last_error), None


def check_bot_binding(claims: Optional[dict[str, Any]], body: Any) -> Verdict:
    """Step 2 (after the body is read): the token must be bound to the event's session."""
    if claims is None:  # DEV_MODE
        return None
    bound = claims.get("connection_id")
    if not bound:
        # Tokens minted before the binding claim existed are refused rather than trusted for every
        # session; a bot spawned by an older meeting-api never held a MeetingToken it sent anyway.
        return 403, "MeetingToken carries no session binding"
    event_session = body.get("connection_id") if isinstance(body, dict) else None
    if not event_session or not hmac.compare_digest(str(bound).encode(), str(event_session).encode()):
        return 403, "MeetingToken is bound to a different session"
    return None


def check_bot_callback(
    headers: Mapping[str, str],
    body: Any,
    *,
    token_secret: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Verdict:
    """Both steps at once (credential + session binding) — for callers that already hold the body."""
    verdict, claims = verify_bot_credential(headers, token_secret=token_secret, env=env)
    if verdict is not None:
        return verdict
    return check_bot_binding(claims, body)


def verify_meeting_token(token: str, *, secret: str) -> dict[str, Any]:
    """The recordings-upload verifier (HS256 + exp + iss/aud/scope), shared so both edges accept
    exactly the same token."""
    from .recordings.service import _verify_meeting_token

    return _verify_meeting_token(token, secret=secret)
