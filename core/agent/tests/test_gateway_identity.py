"""agent-api fail-closed identity (the production default, VEXA_REQUIRE_GATEWAY_IDENTITY unset).

agent-api is reachable by every container on the compose network, so a bare ``X-User-Id`` must never be
trusted: identity headers count only when the request carries ``X-Gateway-Verified`` == the internal-tier
secret (the gateway injects it on /agent/* forwards). The internal sinks (/invocations, /events) — whose
envelopes name their own subject — require the internal secret outright.
"""
from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient

from control_plane.api import create_app
from control_plane.dispatch import Dispatcher
from control_plane.workspace_reader import WorkspaceReader
from shared import adapters as adapters_mod
from shared.adapters import SchedulerHttpClient
from shared.config import load_settings

SECRET = "s3cret-internal"


class _FakeRuntime:
    def __init__(self):
        self.spawned = []

    def spawn(self, workload_id, profile, env):
        self.spawned.append((workload_id, profile, env))
        return workload_id

    def await_done(self, workload_id, timeout_sec=0.0):
        return "completed"


class _FakeIdentity:
    def mint(self, subject, launcher, workspaces, tools):
        return "tok"


@pytest.fixture
def prod_env(monkeypatch):
    # Undo conftest's dev opt-out + single-user fallback: this is the deployed posture.
    monkeypatch.delenv("VEXA_REQUIRE_GATEWAY_IDENTITY", raising=False)
    monkeypatch.setenv("VEXA_AGENT_DEFAULT_SUBJECT", "u_live")  # must be IGNORED in the default mode
    monkeypatch.setenv("VEXA_INTERNAL_API_SECRET", SECRET)


def _client(tmp_path, runtime=None):
    rt = runtime or _FakeRuntime()
    app = create_app(Dispatcher(load_settings(), rt, _FakeIdentity()),
                     reader=WorkspaceReader(str(tmp_path)))
    return TestClient(app), rt


_EVENT = {"name": "email.received", "subject": "u_victim",
          "source": {"uri": "mailbox://u_victim/INBOX/1"}, "plan": {"prompt": "exfiltrate"}}


def test_unauthenticated_identity_routes_are_401(prod_env, tmp_path):
    c, _ = _client(tmp_path)
    # no header at all — the u_live fallback must NOT apply
    assert c.get("/api/sessions").status_code == 401
    # a spoofed X-User-Id without the gateway marker
    assert c.get("/api/sessions", headers={"X-User-Id": "u_victim"}).status_code == 401
    # a forged marker (presence alone is not proof)
    r = c.get("/api/workspace/tree", headers={"X-User-Id": "u_victim", "X-Gateway-Verified": "1"})
    assert r.status_code == 401
    # the internal secret on the WRONG header does not stand in for the gateway marker
    assert c.get("/api/sessions", headers={"X-User-Id": "u_victim", "X-Internal-Secret": SECRET}).status_code == 401


def test_gateway_verified_identity_is_accepted(prod_env, tmp_path):
    c, _ = _client(tmp_path)
    r = c.get("/api/sessions", headers={"X-User-Id": "u_alice", "X-Gateway-Verified": SECRET})
    assert r.status_code == 200, r.text
    # verified marker but no user → still 401
    assert c.get("/api/sessions", headers={"X-Gateway-Verified": SECRET}).status_code == 401


def test_internal_sinks_require_secret(prod_env, tmp_path):
    c, rt = _client(tmp_path)
    assert c.post("/events", json=_EVENT).status_code == 401
    assert c.post("/events", json=_EVENT, headers={"X-Internal-Secret": "wrong"}).status_code == 401
    assert c.post("/events", json=_EVENT, headers={"X-User-Id": "u_victim"}).status_code == 401
    assert c.post("/invocations", json={"anything": 1}).status_code == 401
    assert c.post("/invocations", json={"anything": 1}, headers={"X-Internal-Secret": "wrong"}).status_code == 401
    # the gateway's per-user marker is NOT a sink credential (a proxied user request must never dispatch)
    assert c.post("/events", json=_EVENT, headers={"X-Gateway-Verified": SECRET,
                                                   "X-User-Id": "u_victim"}).status_code == 401
    assert rt.spawned == []
    r = c.post("/events", json=_EVENT, headers={"X-Internal-Secret": SECRET})
    assert r.status_code == 202, r.text
    assert rt.spawned
    # a valid secret reaches envelope validation (400 for a non-conformant body, not 401)
    assert c.post("/invocations", json={"trigger": "message"},
                  headers={"X-Internal-Secret": SECRET}).status_code == 400


def test_unconfigured_secret_fails_closed(monkeypatch, tmp_path):
    monkeypatch.delenv("VEXA_REQUIRE_GATEWAY_IDENTITY", raising=False)
    monkeypatch.delenv("VEXA_INTERNAL_API_SECRET", raising=False)
    c, _ = _client(tmp_path)
    assert c.get("/api/sessions", headers={"X-User-Id": "u", "X-Gateway-Verified": ""}).status_code == 401
    assert c.post("/events", json=_EVENT, headers={"X-Internal-Secret": ""}).status_code == 401
    assert c.post("/invocations", json={}, headers={"X-Internal-Secret": ""}).status_code == 401


def test_dev_opt_out_keeps_direct_edge(monkeypatch, tmp_path):
    monkeypatch.setenv("VEXA_REQUIRE_GATEWAY_IDENTITY", "0")
    monkeypatch.delenv("VEXA_INTERNAL_API_SECRET", raising=False)
    c, rt = _client(tmp_path)
    assert c.get("/api/sessions").status_code == 200  # VEXA_AGENT_DEFAULT_SUBJECT fallback (conftest)
    assert c.post("/events", json=_EVENT).status_code == 202


def test_scheduler_client_stamps_internal_secret_on_jobs(monkeypatch):
    seen = {}

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen["body"] = json.loads(req.data)
        return _Resp(b'{"job_id": "j1"}')

    monkeypatch.setattr(adapters_mod.urllib.request, "urlopen", fake_urlopen)
    client = SchedulerHttpClient("http://runtime:8090", request_headers={"X-Internal-Secret": SECRET})
    client.schedule({"cron": "* * * * *", "request": {"method": "POST", "url": "http://a/invocations",
                                                      "body": {}}})
    assert seen["body"]["request"]["headers"] == {"X-Internal-Secret": SECRET}
    # no headers configured → the job is sent unchanged
    SchedulerHttpClient("http://runtime:8090").schedule(
        {"cron": "* * * * *", "request": {"method": "POST", "url": "http://a/invocations", "body": {}}})
    assert "headers" not in seen["body"]["request"]


# ── C4 split: the gateway identity marker and the internal tier are DIFFERENT secrets ─────────────

IDENTITY = "gw-identity-secret"


@pytest.fixture
def split_env(prod_env, monkeypatch):
    monkeypatch.setenv("VEXA_GATEWAY_IDENTITY_SECRET", IDENTITY)


def test_split_identity_marker_accepts_only_the_gateway_identity_secret(split_env, tmp_path):
    c, _ = _client(tmp_path)
    ok = c.get("/api/sessions", headers={"X-User-Id": "u_alice", "X-Gateway-Verified": IDENTITY})
    assert ok.status_code == 200, ok.text
    # the internal-tier / admin-api key is NOT an identity marker any more
    assert c.get("/api/sessions", headers={"X-User-Id": "u_alice",
                                           "X-Gateway-Verified": SECRET}).status_code == 401


def test_split_internal_sinks_take_only_the_internal_secret(split_env, tmp_path):
    c, _ = _client(tmp_path)
    assert c.post("/events", json=_EVENT, headers={"X-Internal-Secret": IDENTITY}).status_code == 401
    assert c.post("/events", json=_EVENT, headers={"X-Gateway-Verified": IDENTITY}).status_code == 401
    assert c.post("/events", json=_EVENT, headers={"X-Internal-Secret": SECRET}).status_code != 401
