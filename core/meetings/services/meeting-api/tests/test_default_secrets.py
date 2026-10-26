"""The boot refuses published placeholder secrets (ADMIN_TOKEN / INTERNAL_API_SECRET / RUNTIME_API_TOKEN).

The stock compose file, .env.example, helm chart and lite image have shipped public values
(``changeme``, ``dev-admin-token``, ``vexa-internal-secret``, ...). A deploy booting with one of them
has a forgeable MeetingToken key / internal edge, so outside DEV_MODE meeting-api refuses to start.
"""
from __future__ import annotations

import pytest

import meeting_api.__main__ as entry
from meeting_api.config_preflight import ConfigError

REAL_ADMIN = "3f9c2a7e5b1d4c8a9e0f6b2d7a1c5e8f"
REAL_INTERNAL = "b7e1d9c3a5f2e8b4c6d0a2f9e7b5c3d1"
REAL_RUNTIME = "c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6"
GOOD = {"ADMIN_TOKEN": REAL_ADMIN, "INTERNAL_API_SECRET": REAL_INTERNAL, "RUNTIME_API_TOKEN": REAL_RUNTIME}


@pytest.mark.parametrize("key,value", [
    ("ADMIN_TOKEN", "changeme"),
    ("ADMIN_TOKEN", "CHANGE_ME"),
    ("ADMIN_TOKEN", "dev-admin-token"),
    ("INTERNAL_API_SECRET", "vexa-internal-secret"),
    ("INTERNAL_API_SECRET", "lite-internal-secret"),
    ("INTERNAL_API_SECRET", " vexa-internal-secret "),
    ("RUNTIME_API_TOKEN", "vexa-runtime-dev-token"),
])
def test_placeholder_secret_refuses_boot(key, value):
    env = {**GOOD, key: value}
    with pytest.raises(ConfigError) as ei:
        entry._reject_default_secrets(env)
    msg = str(ei.value)
    assert key in msg
    assert value.strip() not in msg  # never echo the value


def test_every_offender_is_named_at_once():
    with pytest.raises(ConfigError) as ei:
        entry._reject_default_secrets({"ADMIN_TOKEN": "changeme", "INTERNAL_API_SECRET": "vexa-internal-secret"})
    assert "ADMIN_TOKEN" in str(ei.value) and "INTERNAL_API_SECRET" in str(ei.value)


def test_real_secrets_boot():
    entry._reject_default_secrets(GOOD)


def test_unset_internal_secret_is_not_this_checks_concern():
    entry._reject_default_secrets({"ADMIN_TOKEN": REAL_ADMIN, "RUNTIME_API_TOKEN": REAL_RUNTIME})


def test_unset_runtime_api_token_refuses_boot():
    """C5: the runtime fails closed without its bearer, so a production meeting-api without
    RUNTIME_API_TOKEN would 503 every spawn — refuse to boot instead (DEV_MODE still escapes)."""
    env = {k: v for k, v in GOOD.items() if k != "RUNTIME_API_TOKEN"}
    with pytest.raises(ConfigError, match="RUNTIME_API_TOKEN"):
        entry._reject_default_secrets(env)
    with pytest.raises(ConfigError, match="RUNTIME_API_TOKEN"):
        entry._reject_default_secrets({**env, "RUNTIME_API_TOKEN": "   "})
    entry._reject_default_secrets({**env, "DEV_MODE": "true"})


def test_runtime_client_sends_the_bearer():
    from meeting_api.bot_spawn.adapters import runtime_auth_headers

    assert runtime_auth_headers({"RUNTIME_API_TOKEN": "tok"}) == {"Authorization": "Bearer tok"}
    assert runtime_auth_headers({"RUNTIME_API_TOKEN": " tok \n"}) == {"Authorization": "Bearer tok"}
    assert runtime_auth_headers({}) == {}
    assert runtime_auth_headers({"RUNTIME_API_TOKEN": "  "}) == {}


def test_dev_mode_tolerates_placeholders():
    entry._reject_default_secrets({"ADMIN_TOKEN": "changeme", "INTERNAL_API_SECRET": "vexa-internal-secret",
                                   "RUNTIME_API_TOKEN": "vexa-runtime-dev-token", "DEV_MODE": "true"})
    with pytest.raises(ConfigError):  # anything but an explicit true still refuses
        entry._reject_default_secrets({"ADMIN_TOKEN": "changeme", "DEV_MODE": "1"})


def test_require_config_runs_the_check(monkeypatch):
    monkeypatch.delenv("DEV_MODE", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "changeme")
    with pytest.raises(RuntimeError) as ei:
        entry._require_config()
    assert "ADMIN_TOKEN" in str(ei.value)
