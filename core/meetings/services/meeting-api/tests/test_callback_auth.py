"""C4 — the internal callback trust edge (callback_auth.py).

Before: ``POST /bots/internal/callback/lifecycle`` and ``POST /runtime/callback`` accepted any
body from anything that could reach meeting-api (every container on the compose network). Now:

  * outside DEV_MODE, an unconfigured credential FAILS CLOSED (503) — the edge never silently opens;
  * the runtime callback needs ``x-internal-secret == INTERNAL_API_SECRET``;
  * the bot callback needs EITHER that secret OR a valid MeetingToken bearer (the bot's own
    ``invocation.token``, HS256 with ADMIN_TOKEN, unexpired);
  * the standalone lifecycle receiver (lifecycle/receiver.py) applies the SAME guard;
  * ``DEV_MODE=true`` keeps the open edge (what the rest of this suite runs under — conftest).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from meeting_api import create_app
from meeting_api.bot_spawn.invocation import mint_meeting_token
from meeting_api.callback_auth import check_bot_callback, check_runtime_callback
from meeting_api.lifecycle import MeetingStore
from meeting_api.lifecycle.receiver import create_app as create_receiver_app

LIFECYCLE = "/bots/internal/callback/lifecycle"
RUNTIME = "/runtime/callback"
SECRET = "c4-internal-secret"
ADMIN = "c4-admin-token"


@pytest.fixture
def closed_edge(monkeypatch):
    """A production-shaped deployment: DEV_MODE off, both credentials configured."""
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("INTERNAL_API_SECRET", SECRET)
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN)


def _unified() -> TestClient:
    return TestClient(create_app(token_secret=ADMIN))


def _event(goldens, name="joining") -> dict:
    return dict(goldens[name])


# ── pure verdicts ───────────────────────────────────────────────────────────────────────────────

def test_runtime_verdict_fails_closed_without_secret():
    env = {"DEV_MODE": "false"}
    assert check_runtime_callback({}, env=env) == (503, "INTERNAL_API_SECRET not configured; runtime callbacks are refused")


def test_runtime_verdict_constant_time_match():
    env = {"DEV_MODE": "false", "INTERNAL_API_SECRET": SECRET}
    assert check_runtime_callback({"x-internal-secret": SECRET}, env=env) is None
    assert check_runtime_callback({"x-internal-secret": SECRET + "x"}, env=env)[0] == 403
    assert check_runtime_callback({"x-internal-secret": ""}, env=env)[0] == 403
    assert check_runtime_callback({}, env=env)[0] == 403


def test_bot_verdict_fails_closed_without_any_credential_configured():
    assert check_bot_callback({}, env={"DEV_MODE": "false"})[0] == 503
    # ADMIN_TOKEN alone is a configuration (bearer path) → 403, not 503
    assert check_bot_callback({}, env={"DEV_MODE": "false", "ADMIN_TOKEN": ADMIN})[0] == 403


def test_bot_verdict_accepts_secret_or_meeting_token():
    env = {"DEV_MODE": "false", "INTERNAL_API_SECRET": SECRET, "ADMIN_TOKEN": ADMIN}
    assert check_bot_callback({"x-internal-secret": SECRET}, env=env) is None
    tok = mint_meeting_token(7, 1, "google_meet", "abc-defg-hij", secret=ADMIN)
    assert check_bot_callback({"authorization": f"Bearer {tok}"}, env=env) is None
    assert check_bot_callback({"authorization": f"bearer {tok}"}, env=env) is None  # scheme is case-insensitive
    # wrong key, tampered, expired, garbage → 403
    other = mint_meeting_token(7, 1, "google_meet", "abc-defg-hij", secret="someone-elses-admin-token")
    assert check_bot_callback({"authorization": f"Bearer {other}"}, env=env)[0] == 403
    h, p, s = tok.split(".")
    assert check_bot_callback({"authorization": f"Bearer {h}.{p}x.{s}"}, env=env)[0] == 403
    expired = mint_meeting_token(7, 1, "google_meet", "abc-defg-hij", secret=ADMIN, ttl_seconds=-5)
    assert check_bot_callback({"authorization": f"Bearer {expired}"}, env=env)[0] == 403
    assert check_bot_callback({"authorization": "Bearer not.a.jwt"}, env=env)[0] == 403
    assert check_bot_callback({"authorization": "Basic abc"}, env=env)[0] == 403
    # a wrong internal secret does NOT fall through to "no credential" — still 403
    assert check_bot_callback({"x-internal-secret": "nope"}, env=env)[0] == 403
    assert check_bot_callback({}, env=env)[0] == 403


def test_bot_verdict_uses_explicit_token_secret_over_env():
    env = {"DEV_MODE": "false", "ADMIN_TOKEN": "env-admin"}
    tok = mint_meeting_token(1, 1, "google_meet", "x", secret="injected-admin")
    assert check_bot_callback({"authorization": f"Bearer {tok}"}, token_secret="injected-admin", env=env) is None
    assert check_bot_callback({"authorization": f"Bearer {tok}"}, env=env)[0] == 403


def test_dev_mode_opens_both_edges():
    env = {"DEV_MODE": "true"}
    assert check_runtime_callback({}, env=env) is None
    assert check_bot_callback({}, env=env) is None
    env = {"DEV_MODE": "true", "INTERNAL_API_SECRET": SECRET}
    assert check_runtime_callback({"x-internal-secret": "wrong"}, env=env) is None


# ── over the unified app ────────────────────────────────────────────────────────────────────────

def test_unified_app_refuses_unauthenticated_callbacks(closed_edge, goldens):
    client = _unified()
    r = client.post(LIFECYCLE, json=_event(goldens))
    assert r.status_code == 403, r.text
    r = client.post(LIFECYCLE, json=_event(goldens), headers={"x-internal-secret": "wrong"})
    assert r.status_code == 403, r.text
    r = client.post(RUNTIME, json={"workloadId": "wl-1", "state": "destroyed"})
    assert r.status_code == 403, r.text
    r = client.post(RUNTIME, json={"workloadId": "wl-1", "state": "destroyed"},
                    headers={"x-internal-secret": "wrong"})
    assert r.status_code == 403, r.text


def test_unified_app_accepts_bot_secret_and_meeting_token(closed_edge, goldens):
    client = _unified()
    r = client.post(LIFECYCLE, json=_event(goldens), headers={"x-internal-secret": SECRET})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "accepted"
    tok = mint_meeting_token(1, 1, "google_meet", "abc-defg-hij", secret=ADMIN)
    r = client.post(LIFECYCLE, json=_event(goldens, "active"), headers={"authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text


def test_unified_app_accepts_runtime_secret(closed_edge):
    client = _unified()
    r = client.post(RUNTIME, json={"workloadId": "wl-1", "state": "destroyed"},
                    headers={"x-internal-secret": SECRET})
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "accepted"}


def test_unified_app_fails_closed_when_unconfigured(monkeypatch, goldens):
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.delenv("INTERNAL_API_SECRET", raising=False)
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    client = TestClient(create_app())  # no token_secret either
    assert client.post(LIFECYCLE, json=_event(goldens)).status_code == 503
    assert client.post(RUNTIME, json={"workloadId": "wl-1", "state": "destroyed"}).status_code == 503


def test_refusal_happens_before_body_parsing(closed_edge):
    """A malformed body from an unauthenticated caller is 403, never 422 — nothing is parsed."""
    client = _unified()
    r = client.post(LIFECYCLE, content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 403


# ── the standalone lifecycle receiver ───────────────────────────────────────────────────────────

def test_receiver_app_applies_the_same_guard(closed_edge, goldens):
    store = MeetingStore()
    client = TestClient(create_receiver_app(store=store))
    assert client.post(LIFECYCLE, json=_event(goldens)).status_code == 403
    r = client.post(LIFECYCLE, json=_event(goldens), headers={"x-internal-secret": SECRET})
    assert r.status_code == 200, r.text
    tok = mint_meeting_token(1, 1, "google_meet", "abc-defg-hij", secret=ADMIN)
    r = client.post(LIFECYCLE, json=_event(goldens, "active"), headers={"authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    assert store.get("sess-uid").status.value == "active"
