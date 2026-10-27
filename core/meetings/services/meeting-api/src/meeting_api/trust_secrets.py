"""The split trust secrets meeting-api reads (C4 follow-up, 2026-10 deep audit finding #2).

Before this split ONE value, ``INTERNAL_API_SECRET``, was at once the gateway-verified identity
marker (``X-Gateway-Verified``), admin-api's internal key (``/internal/*``), the runtime callback
credential AND the secret every spawned bot carried in its invocation. A bot (it runs a browser on
an attacker-chosen page) therefore held the key to impersonate any user. Now every surface has its
own value, and bots get none of them:

  ==========================  ==============================================  ============================
  env var                     what it proves                                  who holds it
  ==========================  ==============================================  ============================
  GATEWAY_IDENTITY_SECRET     "the gateway verified this x-user-* identity"   gateway, meeting-api,
                                                                              agent-api
  ADMIN_API_INTERNAL_SECRET   caller may use admin-api ``/internal/*``         admin-api (as its
                                                                              INTERNAL_API_SECRET), gateway,
                                                                              meeting-api, agent-api, terminal
  RUNTIME_CALLBACK_SECRET     "the runtime kernel posted this workload event" runtime, meeting-api
  MEETING_TOKEN_SECRET        HS256 key of the per-bot MeetingToken           meeting-api only
  ==========================  ==============================================  ============================

Bots authenticate with their MeetingToken only (bound to their ``connection_id``), see
``callback_auth``.

Fallbacks keep an un-migrated deployment (helm, lite, an old ``.env``) booting: an unset split
secret falls back to ``INTERNAL_API_SECRET`` and an unset ``MEETING_TOKEN_SECRET`` to
``ADMIN_TOKEN``, each with a one-time deprecation warning. Bots no longer receive any of these
values in any mode, so the fallback does not reopen the bot impersonation path; the compose stack
sets every split secret explicitly (``${VAR:?}``).
"""
from __future__ import annotations

import logging
import os
from typing import Mapping, Optional

_log = logging.getLogger("meeting_api.trust_secrets")
_warned: set[str] = set()


def _env(env: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if env is None else env


def _get(env: Mapping[str, str], key: str) -> str:
    return (env.get(key) or "").strip()


def _fallback(env: Mapping[str, str], key: str, legacy: str) -> str:
    value = _get(env, key)
    if value:
        return value
    legacy_value = _get(env, legacy)
    if legacy_value and key not in _warned:
        _warned.add(key)
        _log.warning("%s is unset; falling back to %s (deprecated: set a separate %s)", key, legacy, key)
    return legacy_value


def gateway_identity_secret(env: Optional[Mapping[str, str]] = None) -> str:
    """The X-Gateway-Verified marker value (gateway → meeting-api identity edge)."""
    e = _env(env)
    return _fallback(e, "GATEWAY_IDENTITY_SECRET", "INTERNAL_API_SECRET")


def admin_api_internal_secret(env: Optional[Mapping[str, str]] = None) -> str:
    """The X-Internal-Secret admin-api's /internal/* edge checks."""
    e = _env(env)
    return _fallback(e, "ADMIN_API_INTERNAL_SECRET", "INTERNAL_API_SECRET")


def runtime_callback_secret(env: Optional[Mapping[str, str]] = None) -> str:
    """The X-Internal-Secret the runtime kernel stamps on RuntimeEvent callbacks."""
    e = _env(env)
    return _fallback(e, "RUNTIME_CALLBACK_SECRET", "INTERNAL_API_SECRET")


def meeting_token_secret(env: Optional[Mapping[str, str]] = None) -> str:
    """The MeetingToken HS256 key (mint at spawn, verify at callback + upload)."""
    e = _env(env)
    return _fallback(e, "MEETING_TOKEN_SECRET", "ADMIN_TOKEN")


def bot_legacy_internal_secret(env: Optional[Mapping[str, str]] = None) -> bool:
    """``BOT_LEGACY_INTERNAL_SECRET=true``: compat mode for the STOCK upstream bot image.

    The stock ``vexaai/vexa-bot`` (v0.12.0-rc.10 … v0.12.28) never reads ``invocation.token``; it
    authenticates its lifecycle callback with ``x-internal-secret: <invocation.internalSecret>`` and
    its recording upload with ``Authorization: Bearer <invocation.internalSecret>``. In compat mode
    meeting-api puts the bot's OWN MeetingToken into that ``internalSecret`` slot (never a shared
    deployment secret) and accepts it from the ``x-internal-secret`` header, with the same
    signature + connection binding as the bearer. TEMPORARY: remove once the fork builds its own bot
    image (which sends the bearer)."""
    return _get(_env(env), "BOT_LEGACY_INTERNAL_SECRET").lower() in ("1", "true", "yes", "on")
