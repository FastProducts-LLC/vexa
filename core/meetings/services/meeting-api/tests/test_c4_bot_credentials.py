"""C4 follow-up (deep audit #2 / #5): what a spawned bot is handed, and what each split secret opens.

  * a bot's invocation carries NO deployment secret — ``internalSecret`` is absent by default;
  * ``BOT_LEGACY_INTERNAL_SECRET=true`` (stock-bot compat) fills ``internalSecret`` with the bot's
    OWN MeetingToken — never INTERNAL_API_SECRET / the gateway identity / admin / runtime secrets;
  * the MeetingToken is signed with MEETING_TOKEN_SECRET and bound to the spawn's connection_id;
  * ``BOT_REDIS_ACL=true`` gives the bot a per-meeting Redis user (never meeting-api's own URL), and
    the user is deleted when the meeting goes terminal / the spawn fails;
  * the gateway identity edge accepts ONLY GATEWAY_IDENTITY_SECRET once it is set — not the legacy
    INTERNAL_API_SECRET, not admin-api's internal key, not the runtime callback secret, not a token.
"""
from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from meeting_api import create_app
from meeting_api.bot_spawn import SpawnFailed, request_bot
from meeting_api.bot_spawn import redis_acl
from meeting_api.bot_spawn.fakes import FakeRuntimeClient, InMemoryMeetingRepo
from meeting_api.recordings.service import _verify_meeting_token

USER = 7
MTS = "mts-0123"
SERVICE_REDIS = "redis://vexa-svc:svc-pw@redis:6379/0"
SECRETS = {
    "INTERNAL_API_SECRET": "legacy-internal",
    "GATEWAY_IDENTITY_SECRET": "gw-identity",
    "ADMIN_API_INTERNAL_SECRET": "admin-internal",
    "RUNTIME_CALLBACK_SECRET": "runtime-callback",
    "ADMIN_TOKEN": "admin-token",
    "MEETING_TOKEN_SECRET": MTS,
}


@pytest.fixture
def split(monkeypatch):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)


def _claims(token: str) -> dict:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


async def _spawn(runtime=None, repo=None, native="abc-defg-hij"):
    runtime = runtime or FakeRuntimeClient()
    await request_bot(
        repo or InMemoryMeetingRepo(), runtime, user_id=USER, platform="google_meet",
        native_meeting_id=native, bot_name="VexaBot",
        redis_url=SERVICE_REDIS, meeting_api_url="http://meeting-api:8080",
    )
    spec = runtime.specs[-1]
    return json.loads(spec["env"]["VEXA_BOT_CONFIG"]), spec


def _no_secret_anywhere(spec: dict) -> None:
    blob = json.dumps(spec)
    for name, value in SECRETS.items():
        assert value not in blob, f"{name} leaked into the bot workload spec"


# ── invocation contents ─────────────────────────────────────────────────────────────────────────

async def test_bot_invocation_has_no_shared_secret(split):
    inv, spec = await _spawn()
    assert "internalSecret" not in inv
    _no_secret_anywhere(spec)
    claims = _verify_meeting_token(inv["token"], secret=MTS)
    assert claims["connection_id"] == inv["connectionId"]
    assert claims["meeting_id"] == inv["meeting_id"]


async def test_meeting_token_is_signed_with_meeting_token_secret_not_admin_token(split):
    inv, _ = await _spawn()
    with pytest.raises(ValueError):
        _verify_meeting_token(inv["token"], secret=SECRETS["ADMIN_TOKEN"])


async def test_meeting_token_falls_back_to_admin_token(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-only")
    monkeypatch.delenv("MEETING_TOKEN_SECRET", raising=False)
    inv, _ = await _spawn()
    assert _verify_meeting_token(inv["token"], secret="admin-only")["connection_id"] == inv["connectionId"]


async def test_compat_mode_puts_the_bots_own_token_in_internal_secret(split, monkeypatch):
    monkeypatch.setenv("BOT_LEGACY_INTERNAL_SECRET", "true")
    inv, spec = await _spawn()
    assert inv["internalSecret"] == inv["token"]
    _no_secret_anywhere(spec)


async def test_each_spawn_gets_a_distinct_bound_token(split, monkeypatch):
    monkeypatch.setenv("BOT_LEGACY_INTERNAL_SECRET", "true")
    runtime = FakeRuntimeClient()
    repo = InMemoryMeetingRepo()
    a, _ = await _spawn(runtime, repo, native="aaa-bbbb-ccc")
    b, _ = await _spawn(runtime, repo, native="ddd-eeee-fff")
    assert a["internalSecret"] != b["internalSecret"]
    assert _claims(a["token"])["connection_id"] != _claims(b["token"])["connection_id"]


# ── per-meeting Redis ACL ───────────────────────────────────────────────────────────────────────

class _FakeAclRedis:
    def __init__(self, fail=False):
        self.calls: list[tuple] = []
        self.fail = fail

    async def execute_command(self, *args):
        if self.fail:
            raise ConnectionError("redis down")
        self.calls.append(args)
        return "OK"


@pytest.fixture
def acl(monkeypatch):
    fake = _FakeAclRedis()
    monkeypatch.setenv("BOT_REDIS_ACL", "true")
    redis_acl.set_default(redis_acl.BotRedisAcl(fake, base_url=SERVICE_REDIS))
    yield fake
    redis_acl.set_default(None)


def test_acl_rules_are_exactly_the_bot_surface():
    rules = redis_acl.acl_rules(42, "pw")
    assert rules == ["reset", "on", ">pw", "%W~transcription_segments", "&tc:meeting:42:mutable",
                     "&bot_commands:meeting:42", "+xadd", "+publish", "+subscribe", "+unsubscribe",
                     "+ping", "+quit"]
    assert not any(r.startswith(("~", "%R", "&*", "+@")) for r in rules)


def test_bot_url_replaces_credentials_and_keeps_host_db():
    assert redis_acl.bot_url(SERVICE_REDIS, "bot-x", "p w") == "redis://bot-x:p%20w@redis:6379/0"
    assert redis_acl.bot_url("redis://:pw@redis:6379/0", "bot-x", "pp") == "redis://bot-x:pp@redis:6379/0"
    assert redis_acl.bot_url("redis://redis", "u", "p") == "redis://u:p@redis"


async def test_spawn_provisions_a_per_meeting_redis_user(split, acl):
    inv, spec = await _spawn()
    assert inv["redisUrl"] != SERVICE_REDIS
    assert "svc-pw" not in json.dumps(spec)
    user = f"bot-{inv['connectionId']}"
    assert inv["redisUrl"].startswith(f"redis://{user}:") and inv["redisUrl"].endswith("@redis:6379/0")
    (cmd,) = acl.calls
    assert cmd[:3] == ("ACL", "SETUSER", user)
    assert f"&tc:meeting:{inv['meeting_id']}:mutable" in cmd
    assert f"&bot_commands:meeting:{inv['meeting_id']}" in cmd


async def test_spawn_fails_closed_when_acl_provisioning_fails(split, monkeypatch):
    monkeypatch.setenv("BOT_REDIS_ACL", "true")
    redis_acl.set_default(redis_acl.BotRedisAcl(_FakeAclRedis(fail=True), base_url=SERVICE_REDIS))
    runtime = FakeRuntimeClient()
    try:
        with pytest.raises(SpawnFailed):
            await _spawn(runtime)
        assert runtime.specs == []  # never spawned with a broader credential
    finally:
        redis_acl.set_default(None)


async def test_acl_user_revoked_when_spawn_fails(split, acl):
    from meeting_api.bot_spawn import QuotaExceeded

    class _Full(FakeRuntimeClient):
        async def create_workload(self, spec):
            raise QuotaExceeded("full")

    with pytest.raises(QuotaExceeded):
        await _spawn(_Full())
    assert [c[:2] for c in acl.calls] == [("ACL", "SETUSER"), ("ACL", "DELUSER")]
    assert acl.calls[0][2] == acl.calls[1][2]


def test_acl_user_revoked_on_terminal_lifecycle(acl, goldens):
    client = TestClient(create_app())  # DEV_MODE (conftest): the callback edge is open here
    for name in ("joining", "active"):
        assert client.post("/bots/internal/callback/lifecycle", json=dict(goldens[name])).status_code == 200
    assert acl.calls == []
    r = client.post("/bots/internal/callback/lifecycle", json=dict(goldens["completed-stopped"]))
    assert r.status_code == 200, r.text
    assert acl.calls == [("ACL", "DELUSER", "bot-sess-uid")]


async def test_acl_disabled_keeps_the_given_url(split):
    inv, _ = await _spawn()
    assert inv["redisUrl"] == SERVICE_REDIS


# ── each secret opens only its own surface (meeting-api side) ──────────────────────────────────

def _client():
    return TestClient(create_app(meeting_repo=InMemoryMeetingRepo(), runtime=FakeRuntimeClient()))


def test_gateway_identity_edge_accepts_only_the_gateway_identity_secret(split, monkeypatch):
    monkeypatch.setenv("DEV_MODE", "false")
    c = _client()
    ok = c.get("/meetings", headers={"x-user-id": "7", "x-gateway-verified": SECRETS["GATEWAY_IDENTITY_SECRET"]})
    assert ok.status_code == 200, ok.text
    token = None
    for name in ("INTERNAL_API_SECRET", "ADMIN_API_INTERNAL_SECRET", "RUNTIME_CALLBACK_SECRET",
                 "MEETING_TOKEN_SECRET", "ADMIN_TOKEN"):
        r = c.get("/meetings", headers={"x-user-id": "7", "x-gateway-verified": SECRETS[name]})
        assert r.status_code == 401, name
    from meeting_api.bot_spawn.invocation import mint_meeting_token

    token = mint_meeting_token(1, 7, "google_meet", "x", secret=MTS, connection_id="c")
    r = c.get("/meetings", headers={"x-user-id": "7", "x-gateway-verified": token})
    assert r.status_code == 401


def test_gateway_identity_falls_back_to_internal_api_secret_when_unset(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("ADMIN_TOKEN", "x")
    monkeypatch.setenv("INTERNAL_API_SECRET", "legacy-only")
    r = _client().get("/meetings", headers={"x-user-id": "7", "x-gateway-verified": "legacy-only"})
    assert r.status_code == 200, r.text


def test_boot_refuses_without_any_meeting_token_key(monkeypatch):
    from meeting_api.__main__ import _require_meeting_token_secret
    from meeting_api.config_preflight import ConfigError

    with pytest.raises(ConfigError):
        _require_meeting_token_secret({})
    _require_meeting_token_secret({"MEETING_TOKEN_SECRET": "x"})
    _require_meeting_token_secret({"ADMIN_TOKEN": "x"})
