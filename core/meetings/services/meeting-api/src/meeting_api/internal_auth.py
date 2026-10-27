"""Gateway → meeting-api trust edge: a request that asserts a caller identity must prove it came
from the gateway.

meeting-api scopes every user route by the gateway-injected ``x-user-*`` headers (``x-user-id``,
``x-user-limits``, ``x-user-workspaces``, ``x-user-webhook-*`` …). The gateway strips any
client-supplied copies before injecting its own, but anything else that can reach meeting-api over
the network (a shared docker network, a published port) could otherwise send ``x-user-id: <victim>``
directly. So: a request carrying ANY ``x-user-*`` header must also carry the gateway's
``X-Gateway-Verified`` marker equal to ``GATEWAY_IDENTITY_SECRET`` (constant-time compare; falls back
to the legacy ``INTERNAL_API_SECRET`` when unset — see trust_secrets) — the SAME
marker agent-api requires (the gateway stamps it on its forwards and strips client-sent copies). Requests without identity headers are untouched
(``/health``, the bot's lifecycle callback, the MeetingToken-authed recordings upload) — and every
identity-scoped route already 401s when ``x-user-id`` is absent.

Fail closed: ``GATEWAY_IDENTITY_SECRET`` (and the legacy ``INTERNAL_API_SECRET``) unset ⇒ 503 for identity-bearing requests, unless
``DEV_MODE=true`` (then the edge is open, the pre-hardening behavior, for local dev only).
"""
from __future__ import annotations

import hmac
import json
import os
from typing import Optional, Tuple

IDENTITY_HEADER_PREFIX = "x-user-"
GATEWAY_VERIFIED_HEADER = "x-gateway-verified"


def dev_mode() -> bool:
    return (os.getenv("DEV_MODE") or "false").strip().lower() == "true"


def check_identity_request(headers) -> Optional[Tuple[int, str]]:
    """``None`` when the request may proceed, else ``(status, detail)``. ``headers`` is any
    case-insensitive mapping with ``.keys()``/``.get()`` (Starlette ``Headers``)."""
    if not any(k.lower().startswith(IDENTITY_HEADER_PREFIX) for k in headers.keys()):
        return None
    from .trust_secrets import gateway_identity_secret

    secret = gateway_identity_secret()
    if not secret:
        if dev_mode():
            return None
        return 503, "GATEWAY_IDENTITY_SECRET not configured; identity-bearing requests are refused"
    provided = headers.get(GATEWAY_VERIFIED_HEADER) or ""
    if not hmac.compare_digest(provided.encode("utf-8"), secret.encode("utf-8")):
        return 401, "gateway-verified identity required"
    return None


class InternalIdentityMiddleware:
    """Pure-ASGI middleware applying :func:`check_identity_request` to every HTTP + websocket
    request (pure ASGI so it never buffers or wraps streaming responses)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        from starlette.datastructures import Headers

        verdict = check_identity_request(Headers(scope=scope))
        if verdict is None:
            return await self.app(scope, receive, send)
        status, detail = verdict
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"detail": detail}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii"))],
        })
        await send({"type": "http.response.body", "body": body})
