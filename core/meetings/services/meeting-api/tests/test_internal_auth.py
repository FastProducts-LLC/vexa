"""The gateway trust edge (internal_auth.py), ported to main with the C4 follow-up.

meeting-api scopes every user route by gateway-injected ``x-user-*`` headers. Anything else that can
reach it (every container on `vexa`, and every spawned bot on `bots`) must not be able to assert an
identity: a request carrying any ``x-user-*`` header needs ``X-Gateway-Verified ==
GATEWAY_IDENTITY_SECRET`` (legacy fallback INTERNAL_API_SECRET), and an unset secret fails closed
outside DEV_MODE.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from meeting_api import create_app
from meeting_api.bot_spawn.fakes import FakeRuntimeClient, InMemoryMeetingRepo

SECRET = "gw-identity-0123456789"


def _client():
    return TestClient(create_app(meeting_repo=InMemoryMeetingRepo(), runtime=FakeRuntimeClient()))


@pytest.fixture
def prod(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("MEETING_TOKEN_SECRET", "mts")
    monkeypatch.setenv("GATEWAY_IDENTITY_SECRET", SECRET)


@pytest.mark.parametrize("method, path", [
    ("GET", "/meetings"),
    ("GET", "/bots/status"),
    ("GET", "/recordings"),
    ("POST", "/bots"),
])
def test_forged_identity_without_marker_is_401(prod, method, path):
    r = _client().request(method, path, headers={"x-user-id": "1"}, json={})
    assert r.status_code == 401
    assert r.json()["detail"] == "gateway-verified identity required"


def test_wrong_marker_is_401(prod):
    c = _client()
    assert c.get("/meetings", headers={"x-user-id": "1", "x-gateway-verified": SECRET[:-1]}).status_code == 401
    # x-internal-secret is NOT an accepted spelling for identity requests
    assert c.get("/meetings", headers={"x-user-id": "1", "x-internal-secret": SECRET}).status_code == 401


def test_any_x_user_header_triggers_the_edge(prod):
    for h in ("x-user-workspaces", "X-User-Webhook-Url", "x-user-limits"):
        assert _client().get("/health", headers={h: "1"}).status_code == 401


def test_gateway_marker_passes_through(prod):
    r = _client().get("/meetings", headers={"x-user-id": "7", "x-gateway-verified": SECRET})
    assert r.status_code == 200, r.text


def test_requests_without_identity_are_untouched(prod):
    assert _client().get("/health").status_code == 200


def test_unset_secret_fails_closed_outside_dev_mode(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.delenv("GATEWAY_IDENTITY_SECRET", raising=False)
    monkeypatch.delenv("INTERNAL_API_SECRET", raising=False)
    r = _client().get("/meetings", headers={"x-user-id": "7", "x-gateway-verified": "anything"})
    assert r.status_code == 503


def test_dev_mode_without_secret_keeps_the_edge_open(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.delenv("GATEWAY_IDENTITY_SECRET", raising=False)
    monkeypatch.delenv("INTERNAL_API_SECRET", raising=False)
    assert _client().get("/meetings", headers={"x-user-id": "7"}).status_code == 200
