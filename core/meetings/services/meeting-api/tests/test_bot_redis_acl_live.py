"""Per-meeting bot Redis ACL against a REAL Redis 7 (C4 follow-up, deep audit #5).

Skipped unless ``VEXA_TEST_REDIS_ADMIN_URL`` points at a disposable Redis 7 whose user may run
``ACL SETUSER/DELUSER`` — e.g. the compose layout::

    docker run -d --rm -p 127.0.0.1:16399:6379 redis:7-alpine redis-server \\
        --user default off --user vexa-svc on '>svcpw' '~*' '&*' '+@all'
    VEXA_TEST_REDIS_ADMIN_URL=redis://vexa-svc:svcpw@127.0.0.1:16399/0 uv run pytest tests/test_bot_redis_acl_live.py

Proves, with the URL meeting-api actually hands a bot: its own XADD/PUBLISH/SUBSCRIBE/PING work;
reading ``u:*`` / the shared feed, KEYS/SCAN/CONFIG/ACL/FLUSHALL, and another meeting's channels are
NOPERM; after revoke the credential no longer authenticates.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from meeting_api.bot_spawn import redis_acl

ADMIN_URL = os.getenv("VEXA_TEST_REDIS_ADMIN_URL")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="VEXA_TEST_REDIS_ADMIN_URL not set (needs a real Redis 7)")


def _noperm(exc: BaseException) -> bool:
    return "NOPERM" in str(exc) or "no permissions" in str(exc).lower()


async def test_bot_user_is_scoped_to_its_meeting():
    import redis.asyncio as aioredis
    from redis.exceptions import AuthenticationError, ResponseError

    svc = aioredis.from_url(ADMIN_URL, decode_responses=True)
    acl = redis_acl.BotRedisAcl(svc, base_url=ADMIN_URL)
    conn = str(uuid.uuid4())
    meeting, other = 4242, 4343
    await svc.xadd("u:7:meetings", {"secret": "tenant-7"})
    await svc.set("u:7:profile", "tenant-7")

    url = await acl.provision(meeting_id=meeting, connection_id=conn)
    assert "svcpw" not in url and f"bot-{conn}" in url
    bot = aioredis.from_url(url, decode_responses=True)
    try:
        # allowed: exactly what the bot does
        assert await bot.ping() is True
        assert await bot.xadd("transcription_segments", {"payload": "{}"})
        await bot.publish(f"tc:meeting:{meeting}:mutable", "seg")
        sub = bot.pubsub()
        await sub.subscribe(f"bot_commands:meeting:{meeting}")
        await sub.unsubscribe()
        await sub.aclose()

        denied = [
            lambda: bot.xrange("u:7:meetings"),
            lambda: bot.get("u:7:profile"),
            lambda: bot.xrange("transcription_segments"),
            lambda: bot.xread({"transcription_segments": "0"}),
            lambda: bot.keys("*"),
            lambda: bot.scan(0),
            lambda: bot.config_get("*"),
            lambda: bot.execute_command("ACL", "LIST"),
            lambda: bot.flushall(),
            lambda: bot.publish(f"tc:meeting:{other}:mutable", "forged"),
            lambda: bot.publish(f"bot_commands:meeting:{other}", "{}"),
            lambda: bot.publish("u:7:meetings", "forged"),
            lambda: bot.xadd(f"tc:meeting:{other}", {"type": "session_end"}),
        ]
        for i, call in enumerate(denied):
            with pytest.raises(ResponseError) as ei:
                await call()
            assert _noperm(ei.value), (i, ei.value)

        for channel in (f"tc:meeting:{other}:mutable", "u:7:meetings"):
            s = aioredis.from_url(url, decode_responses=True).pubsub()
            with pytest.raises(ResponseError) as ei:
                await s.subscribe(channel)
                await asyncio.wait_for(s.get_message(timeout=1), 2)
            assert _noperm(ei.value), channel
            await s.aclose()
        s = aioredis.from_url(url, decode_responses=True).pubsub()
        with pytest.raises(ResponseError):
            await s.psubscribe("u:*")
            await asyncio.wait_for(s.get_message(timeout=1), 2)
        await s.aclose()
    finally:
        await bot.aclose()

    await acl.revoke(conn)
    await acl.revoke(conn)  # idempotent
    gone = aioredis.from_url(url, decode_responses=True)
    with pytest.raises((AuthenticationError, ResponseError)):
        await gone.ping()
    await gone.aclose()
    await svc.delete("u:7:meetings", "u:7:profile")
    await svc.aclose()
