"""Per-meeting Redis ACL users for spawned bots (C4 follow-up, 2026-10 deep audit finding #5).

A bot used to receive meeting-api's own ``REDIS_URL``, so with one shared ``requirepass`` any bot
could read or publish every tenant's channels and streams (``tc:*``, ``u:*``, ``bm:*`` …). With
``BOT_REDIS_ACL=true`` every spawn instead gets a Redis 7 ACL user of its own that can do exactly
what the bot code does, for its own meeting only:

  * ``XADD transcription_segments``            — write-only (``%W~``): no XRANGE/XREAD of the feed
  * ``PUBLISH tc:meeting:{meeting_id}:mutable``  — its own live transcript channel
  * ``SUBSCRIBE bot_commands:meeting:{meeting_id}`` — its own command channel (leave / speak …)
  * ``PING`` (reachability probe), ``UNSUBSCRIBE``, ``QUIT``

Everything else is NOPERM: other meetings' channels, ``u:*``, KEYS/SCAN, CONFIG, ACL, FLUSH*.
(Bot Redis usage verified against the fork source AND the stock ``vexaai/vexa-bot`` v0.12.0-rc.10 /
v012 dist adapters: transcript-redis, acts-redis, the v012 PING probe.)

The user is named ``bot-<connection_id>`` and gets a fresh random password; meeting-api creates it
at spawn (``ACL SETUSER``) and deletes it when the meeting reaches a terminal state (``ACL
DELUSER``). meeting-api's own Redis user therefore needs ``+acl|setuser +acl|deluser`` (the
compose ``vexa-svc`` user has ``+@all``).

Residual (documented, not fixed here): ``transcription_segments`` is ONE stream for all meetings
and the collector routes each entry by the ``meeting_id`` inside its payload, so a compromised bot
can still XADD a forged segment naming another meeting. Closing that needs per-meeting streams or a
signed payload in the collector — the stock bot can do neither.

ACL users live in memory: a Redis restart drops them (live bots then fail their reconnect and end
``failed``, the same as losing Redis did before). Not persisted on purpose — they are per-run.
"""
from __future__ import annotations

import os
import secrets
from typing import Optional
from urllib.parse import quote, urlsplit, urlunsplit

TRANSCRIPTION_STREAM = "transcription_segments"
BOT_COMMANDS = ["xadd", "publish", "subscribe", "unsubscribe", "ping", "quit"]


def enabled(env: Optional[dict] = None) -> bool:
    env = os.environ if env is None else env
    return (env.get("BOT_REDIS_ACL") or "").strip().lower() in ("1", "true", "yes", "on")


def username_for(connection_id: str) -> str:
    return f"bot-{connection_id}"


def acl_rules(meeting_id: int | str, password: str) -> list[str]:
    """The ``ACL SETUSER`` rule list for one bot run (``reset`` first, so a re-provision is exact)."""
    return [
        "reset",
        "on",
        f">{password}",
        f"%W~{TRANSCRIPTION_STREAM}",
        f"&tc:meeting:{meeting_id}:mutable",
        f"&bot_commands:meeting:{meeting_id}",
        *[f"+{c}" for c in BOT_COMMANDS],
    ]


def bot_url(base_url: str, username: str, password: str) -> str:
    """``base_url`` (meeting-api's own REDIS_URL) with its credentials replaced by the bot's."""
    parts = urlsplit(base_url)
    host = parts.hostname or "redis"
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    netloc = f"{quote(username, safe='')}:{quote(password, safe='')}@{host}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme or "redis", netloc, parts.path, parts.query, parts.fragment))


class BotRedisAcl:
    """Creates / deletes the per-bot ACL users over meeting-api's own Redis connection."""

    def __init__(self, client=None, *, base_url: Optional[str] = None):
        self._client = client
        self._base_url = base_url

    @property
    def base_url(self) -> str:
        return self._base_url or os.getenv("REDIS_URL", "redis://redis:6379/0")

    def _redis(self):
        if self._client is None:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(self.base_url, decode_responses=True)
        return self._client

    async def provision(self, *, meeting_id: int | str, connection_id: str) -> str:
        """Create the bot's user and return the Redis URL to put in its invocation. Raises on any
        Redis error — the caller fails the spawn rather than hand out a broader credential."""
        username = username_for(connection_id)
        password = secrets.token_hex(24)
        await self._redis().execute_command("ACL", "SETUSER", username, *acl_rules(meeting_id, password))
        return bot_url(self.base_url, username, password)

    async def revoke(self, connection_id: str) -> None:
        """Delete the bot's user (idempotent: deleting an absent user is a no-op in Redis)."""
        await self._redis().execute_command("ACL", "DELUSER", username_for(connection_id))


_default: Optional[BotRedisAcl] = None


def default() -> BotRedisAcl:
    global _default
    if _default is None:
        _default = BotRedisAcl()
    return _default


def set_default(acl: Optional[BotRedisAcl]) -> None:
    """Test seam / composition root override."""
    global _default
    _default = acl
