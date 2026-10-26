"""The gateway trust edge (internal_auth.py) + the boot-time placeholder-secret refusal (__main__).

meeting-api scopes every user route by gateway-injected ``x-user-*`` headers. Anything else that can
reach it over the network must not be able to assert an identity: a request carrying any ``x-user-*``
header needs ``X-Gateway-Verified == INTERNAL_API_SECRET`` (the same marker agent-api requires), and
an unset secret fails closed outside DEV_MODE.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from meeting_api import create_app
from meeting_api.bot_spawn.fakes import FakeRuntimeClient, InMemoryMeetingRepo

SECRET = "isec-0123456789"


@pytest.fixture(autouse=True)
def _admin_token(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")


def _client():
    return TestClient(create_app(meeting_repo=InMemoryMeetingRepo(), runtime=FakeRuntimeClient()))


@pytest.fixture
def prod(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("INTERNAL_API_SECRET", SECRET)


# ── the edge ─────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("method, path", [
    ("GET", "/meetings"),
    ("GET", "/bots/status"),
    ("GET", "/recordings"),
    ("POST", "/bots"),
    ("DELETE", "/bots/google_meet/abc-defg-hij"),
    ("POST", "/bots/google_meet/abc-defg-hij/speak"),
    ("POST", "/ws/authorize-subscribe"),
])
def test_forged_identity_without_marker_is_401(prod, method, path):
    r = _client().request(method, path, headers={"x-user-id": "1"}, json={})
    assert r.status_code == 401
    assert r.json()["detail"] == "gateway-verified identity required"


def test_wrong_marker_is_401(prod):
    r = _client().get("/meetings", headers={"x-user-id": "1", "x-gateway-verified": SECRET[:-1]})
    assert r.status_code == 401
    # x-internal-secret is NOT an accepted spelling for identity requests (one scheme: the marker)
    r = _client().get("/meetings", headers={"x-user-id": "1", "x-internal-secret": SECRET})
    assert r.status_code == 401


def test_any_x_user_header_triggers_the_edge(prod):
    """Not just x-user-id: a smuggled x-user-workspaces / x-user-webhook-url is also identity."""
    for h in ("x-user-workspaces", "X-User-Webhook-Url", "x-user-limits"):
        assert _client().get("/health", headers={h: "1"}).status_code == 401


def test_gateway_marker_passes_through(prod):
    r = _client().get("/meetings", headers={"x-user-id": "7", "x-gateway-verified": SECRET})
    assert r.status_code == 200, r.text


def test_requests_without_identity_are_untouched(prod):
    c = _client()
    assert c.get("/health").status_code == 200
    # an identity-less user route still fails closed on its own (401 Missing user identity)
    r = c.get("/meetings")
    assert r.status_code == 401 and r.json()["detail"] == "Missing user identity"


def test_unset_secret_fails_closed_outside_dev_mode(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.delenv("INTERNAL_API_SECRET", raising=False)
    r = _client().get("/meetings", headers={"x-user-id": "7", "x-gateway-verified": ""})
    assert r.status_code == 503
    monkeypatch.delenv("DEV_MODE", raising=False)  # absent == false
    assert _client().get("/meetings", headers={"x-user-id": "7"}).status_code == 503


def test_dev_mode_without_secret_is_open(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.delenv("INTERNAL_API_SECRET", raising=False)
    assert _client().get("/meetings", headers={"x-user-id": "7"}).status_code == 200


def test_dev_mode_with_secret_still_enforces(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("INTERNAL_API_SECRET", SECRET)
    assert _client().get("/meetings", headers={"x-user-id": "7"}).status_code == 401
    assert _client().get("/meetings", headers={"x-user-id": "7", "x-gateway-verified": SECRET}).status_code == 200


# ── boot: stock placeholder secrets are refused outside DEV_MODE ─────────────────────────────────

GOOD = {
    "ADMIN_TOKEN": "a" * 32,
    "INTERNAL_API_SECRET": "i" * 32,
    "DB_PASSWORD": "d" * 32,
    "MINIO_SECRET_KEY": "m" * 32,
    "RUNTIME_API_TOKEN": "r" * 32,
    "TRANSCRIPTION_SERVICE_URL": "",
    "TRANSCRIPTION_SERVICE_TOKEN": "",
}


def _boot(env):
    import meeting_api.__main__ as entry

    entry._require_config(env)


def test_boot_accepts_real_secrets():
    _boot(dict(GOOD))
    _boot({**GOOD, "DEV_MODE": "false"})


@pytest.mark.parametrize("key, placeholder", [
    ("ADMIN_TOKEN", "changeme"),
    ("ADMIN_TOKEN", "dev-admin-token"),
    ("INTERNAL_API_SECRET", "vexa-internal-secret"),
    ("DB_PASSWORD", "postgres"),
    ("MINIO_SECRET_KEY", "vexa-secret-key"),
    ("RUNTIME_API_TOKEN", "vexa-runtime-dev-token"),
])
def test_boot_refuses_placeholder(key, placeholder):
    from meeting_api.config_preflight import ConfigError

    with pytest.raises(ConfigError) as ei:
        _boot({**GOOD, key: placeholder})
    assert key in str(ei.value)
    assert placeholder not in str(ei.value), "never echo the secret value"
    _boot({**GOOD, key: placeholder, "DEV_MODE": "true"})  # dev escape hatch


def test_boot_refuses_unset_runtime_api_token():
    """C5: the runtime fails closed without its bearer, so a production meeting-api without
    RUNTIME_API_TOKEN would 503 every spawn — refuse to boot instead (DEV_MODE still escapes)."""
    from meeting_api.config_preflight import ConfigError

    env = {k: v for k, v in GOOD.items() if k != "RUNTIME_API_TOKEN"}
    with pytest.raises(ConfigError, match="RUNTIME_API_TOKEN"):
        _boot(env)
    with pytest.raises(ConfigError, match="RUNTIME_API_TOKEN"):
        _boot({**env, "RUNTIME_API_TOKEN": "   "})
    _boot({**env, "DEV_MODE": "true"})


def test_runtime_client_sends_the_bearer():
    from meeting_api.bot_spawn.adapters import runtime_auth_headers

    assert runtime_auth_headers({"RUNTIME_API_TOKEN": "tok"}) == {"Authorization": "Bearer tok"}
    assert runtime_auth_headers({"RUNTIME_API_TOKEN": " tok \n"}) == {"Authorization": "Bearer tok"}
    assert runtime_auth_headers({}) == {}
    assert runtime_auth_headers({"RUNTIME_API_TOKEN": "  "}) == {}


def test_boot_names_every_placeholder_at_once():
    from meeting_api.config_preflight import ConfigError

    env = {**GOOD, "ADMIN_TOKEN": "changeme", "INTERNAL_API_SECRET": "vexa-internal-secret"}
    with pytest.raises(ConfigError) as ei:
        _boot(env)
    assert "ADMIN_TOKEN" in str(ei.value) and "INTERNAL_API_SECRET" in str(ei.value)


def test_boot_unset_db_password_is_the_postgres_default():
    from meeting_api.config_preflight import ConfigError

    env = {k: v for k, v in GOOD.items() if k != "DB_PASSWORD"}
    with pytest.raises(ConfigError, match="DB_PASSWORD"):
        _boot(env)
    # an explicit DATABASE_URL replaces the DB_* parts → DB_PASSWORD is not consulted
    _boot({**env, "DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/d"})


def test_boot_minio_placeholder_ignored_when_s3_secret_overrides():
    _boot({**GOOD, "MINIO_SECRET_KEY": "vexa-secret-key", "S3_SECRET_KEY": "s" * 32})
