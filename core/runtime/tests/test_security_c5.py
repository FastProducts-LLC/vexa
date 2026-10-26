"""Security finding C5 — the runtime control plane holds the host Docker socket, so:

  * every route except /health requires ``Authorization: Bearer $RUNTIME_API_TOKEN`` and FAILS CLOSED
    (503) when no token is configured;
  * a WorkloadSpec can never make the runtime bind a host path, a foreign named volume or the docker
    socket — bind sources come from the server-side ``RUNTIME_MOUNT_SOURCE_ALLOWLIST`` only;
  * the docker backend never emits privileged / host-namespace / socket-mounting host config."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from runtime_kernel import Runtime
from runtime_kernel.api import create_app
from runtime_kernel.docker_backend import DockerBackend, _assert_safe_host_config
from runtime_kernel.mounts import MountBind, MountRejected, validate_mount_env, workspace_binds
from runtime_kernel.profiles import Runnable

TOKEN = "s3cret-runtime-token-for-tests"
PROTECTED = [
    ("get", "/workloads", None),
    ("post", "/workloads", {"workloadId": "w1", "profile": "test", "env": {}}),
    ("get", "/workloads/w1", None),
    ("post", "/workloads/w1/stop", {}),
    ("delete", "/workloads/w1", None),
    ("post", "/schedule", {"request": {"url": "http://evil/"}, "execute_at": "2030-01-01T00:00:00Z"}),
    ("get", "/schedule", None),
    ("delete", "/schedule/j1", None),
    ("get", "/openapi.json", None),
    ("get", "/docs", None),
]


@pytest.fixture
def secure_env(monkeypatch):
    monkeypatch.delenv("RUNTIME_API_INSECURE_NO_AUTH", raising=False)
    monkeypatch.setenv("RUNTIME_API_TOKEN", TOKEN)


def _call(client, method, path, body, headers=None):
    fn = getattr(client, method)
    if body is not None and method in ("post",):
        return fn(path, json=body, headers=headers or {})
    return fn(path, headers=headers or {})


def _client():
    return TestClient(create_app(Runtime(profiles={"test": ["sleep", "30"]}, grace_sec=3.0),
                                 deliver=lambda ev: None))


# ── auth ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("method,path,body", PROTECTED)
def test_missing_token_is_401(secure_env, method, path, body):
    r = _call(_client(), method, path, body)
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Bearer"


@pytest.mark.parametrize("method,path,body", PROTECTED)
def test_wrong_token_is_401(secure_env, method, path, body):
    client = _client()
    for hdr in ({"Authorization": "Bearer nope"}, {"Authorization": f"Bearer {TOKEN}x"},
                {"Authorization": TOKEN}, {"Authorization": f"Basic {TOKEN}"},
                {"X-Internal-Secret": TOKEN}):
        assert _call(client, method, path, body, hdr).status_code == 401, hdr


def test_health_is_open_without_token(secure_env):
    r = _client().get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_correct_token_reaches_the_routes(secure_env):
    client = _client()
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert client.get("/workloads", headers=auth).status_code == 200
    r = client.post("/workloads", headers=auth, json={"workloadId": "w1", "profile": "test", "env": {}})
    assert r.status_code == 201
    assert client.get("/workloads/w1", headers=auth).json()["state"] == "running"
    assert client.delete("/workloads/w1", headers=auth).json()["state"] == "destroyed"
    # no scheduler wired → 503 AFTER auth (proves the request passed the gate)
    assert client.get("/schedule", headers=auth).status_code == 503
    # lower-case scheme is accepted (RFC 7235 schemes are case-insensitive)
    assert client.get("/workloads", headers={"Authorization": f"bearer {TOKEN}"}).status_code == 200


def test_unset_token_fails_closed_but_health_stays_open(monkeypatch):
    monkeypatch.delenv("RUNTIME_API_INSECURE_NO_AUTH", raising=False)
    monkeypatch.delenv("RUNTIME_API_TOKEN", raising=False)
    client = _client()
    assert client.get("/health").status_code == 200
    for method, path, body in PROTECTED:
        assert _call(client, method, path, body).status_code == 503, path
        # even a guessed bearer is refused while unconfigured (no empty-token match)
        assert _call(client, method, path, body, {"Authorization": "Bearer "}).status_code == 503


def test_explicit_api_token_argument_wins(monkeypatch):
    monkeypatch.delenv("RUNTIME_API_INSECURE_NO_AUTH", raising=False)
    monkeypatch.delenv("RUNTIME_API_TOKEN", raising=False)
    client = TestClient(create_app(Runtime(profiles={}), api_token="arg-token"))
    assert client.get("/workloads").status_code == 401
    assert client.get("/workloads", headers={"Authorization": "Bearer arg-token"}).status_code == 200


def test_whitespace_only_token_counts_as_unset(monkeypatch):
    monkeypatch.delenv("RUNTIME_API_INSECURE_NO_AUTH", raising=False)
    monkeypatch.setenv("RUNTIME_API_TOKEN", "   ")
    client = _client()
    assert client.get("/workloads", headers={"Authorization": "Bearer    "}).status_code == 503


# ── mount policy ──────────────────────────────────────────────────────────────

def _mounts_env(mounts, **extra):
    return {"VEXA_MOUNTS": json.dumps(mounts), **extra}


@pytest.mark.parametrize("mount", [
    {"slug": "root", "source": "/", "path": "/host"},
    {"slug": "etc", "source": "/etc", "path": "/workspaces/etc"},
    {"slug": "sock", "source": "/var/run/docker.sock", "path": "/var/run/docker.sock"},
    {"slug": "sock2", "source": "/run/docker.sock", "path": "/sock"},
    {"slug": "docker", "source": "/var/lib/docker", "path": "/d"},
    {"slug": "trav", "source": "/srv/vexa-global/../../etc", "path": "/workspaces/_global"},
    {"slug": "unlisted", "source": "/home/deploy", "path": "/workspaces/h"},
    {"slug": "vol", "source": "vexa-v012_postgres-data", "path": "/pg"},
    # no source + outside the store root → path is bound as a HOST path
    {"slug": "nosrc", "path": "/etc"},
    {"slug": "nosrc-sock", "path": "/var/run/docker.sock"},
])
def test_host_paths_sockets_and_foreign_volumes_rejected(monkeypatch, mount):
    monkeypatch.setenv("RUNTIME_MOUNT_SOURCE_ALLOWLIST", "agent-workspaces,/srv/vexa-global")
    env = _mounts_env([mount], VEXA_WORKSPACE_MOUNT_SOURCE="agent-workspaces",
                      VEXA_WORKSPACE_MOUNT_TARGET="/workspaces")
    with pytest.raises(MountRejected):
        validate_mount_env(env)
    with pytest.raises(MountRejected):
        workspace_binds(env)  # the backend path refuses too (defense in depth)


@pytest.mark.parametrize("env", [
    {"VEXA_WORKSPACE_MOUNT_SOURCE": "/", "VEXA_WORKSPACE_MOUNT_TARGET": "/workspaces",
     "VEXA_WORKSPACE_PATH": "/workspaces/u1"},
    {"VEXA_WORKSPACE_MOUNT_SOURCE": "vexa-v012_minio-data", "VEXA_WORKSPACE_MOUNT_TARGET": "/workspaces",
     "VEXA_WORKSPACE_PATH": "/workspaces/u1"},
    {"VEXA_WORKSPACE_MOUNT_SOURCE": "agent-workspaces", "VEXA_WORKSPACE_MOUNT_TARGET": "/workspaces",
     "VEXA_WORKSPACE_PATH": "/workspaces/../etc"},
    {"VEXA_WORKSPACE_MOUNT_SOURCE": "agent-workspaces", "VEXA_WORKSPACE_MOUNT_TARGET": "/workspaces/../",
     "VEXA_WORKSPACE_PATH": "/workspaces/u1"},
    # Binds-string option injection ("src:dst:opts")
    {"VEXA_WORKSPACE_MOUNT_SOURCE": "agent-workspaces", "VEXA_WORKSPACE_MOUNT_TARGET": "/workspaces",
     "VEXA_WORKSPACE_PATH": "/workspaces/u1:rw,z"},
    {"VEXA_WORKSPACE_MOUNT_SOURCE": "/srv/vexa-global:/x", "VEXA_WORKSPACE_MOUNT_TARGET": "/workspaces",
     "VEXA_WORKSPACE_PATH": "/workspaces/u1"},
    # relative container target
    {"VEXA_MOUNTS": json.dumps([{"slug": "r", "source": "/srv/vexa-global", "path": "rel/target"}])},
    # in-store traversal that would normalise out of the store
    {"VEXA_WORKSPACE_MOUNT_SOURCE": "agent-workspaces", "VEXA_WORKSPACE_MOUNT_TARGET": "/workspaces",
     "VEXA_MOUNTS": json.dumps([{"slug": "t", "path": "/workspaces/u1/../../etc"}])},
])
def test_traversal_injection_and_unlisted_store_rejected(monkeypatch, env):
    monkeypatch.setenv("RUNTIME_MOUNT_SOURCE_ALLOWLIST", "agent-workspaces,/srv/vexa-global")
    with pytest.raises(MountRejected):
        validate_mount_env(env)


def test_docker_sock_rejected_even_if_operator_allowlists_its_parent(monkeypatch):
    monkeypatch.setenv("RUNTIME_MOUNT_SOURCE_ALLOWLIST", "/,/var/run,/var")
    for src in ("/var/run/docker.sock", "/var/run", "/", "/etc/shadow"):
        with pytest.raises(MountRejected):
            validate_mount_env(_mounts_env([{"slug": "s", "source": src, "path": "/x"}]))


def test_empty_allowlist_rejects_every_request_declared_source(monkeypatch):
    monkeypatch.delenv("RUNTIME_MOUNT_SOURCE_ALLOWLIST", raising=False)
    with pytest.raises(MountRejected):
        validate_mount_env({"VEXA_WORKSPACE_MOUNT_SOURCE": "agent-workspaces"})
    with pytest.raises(MountRejected):
        validate_mount_env(_mounts_env([{"slug": "g", "source": "/srv/vexa-global", "path": "/w/_g"}]))
    # …but a bot spawn (no mount env at all) is untouched
    validate_mount_env({"VEXA_BOT_CONFIG": "{}", "TTS_SERVICE_URL": "http://tts-cartesia:8080"})
    assert workspace_binds({"VEXA_BOT_CONFIG": "{}"}) == []


def test_allowlisted_legitimate_agent_dispatch_still_binds(monkeypatch):
    monkeypatch.setenv("RUNTIME_MOUNT_SOURCE_ALLOWLIST", "vexa-v012_agent-workspaces, /srv/vexa-global")
    env = {
        "VEXA_WORKSPACE_MOUNT_SOURCE": "vexa-v012_agent-workspaces",
        "VEXA_WORKSPACE_MOUNT_TARGET": "/workspaces",
        "VEXA_MOUNTS": json.dumps([
            {"slug": "seed", "path": "/workspaces/u1", "write": True, "primary": True},
            {"slug": "_global", "source": "/srv/vexa-global", "path": "/workspaces/_global", "write": False},
        ]),
    }
    validate_mount_env(env)
    assert workspace_binds(env) == [
        MountBind("vexa-v012_agent-workspaces", "/workspaces/u1", False, volume_subpath="u1"),
        MountBind("/srv/vexa-global", "/workspaces/_global", True),
    ]


def test_api_rejects_host_mount_spec_with_400_and_spawns_nothing(monkeypatch):
    monkeypatch.setenv("RUNTIME_MOUNT_SOURCE_ALLOWLIST", "")
    events = []
    rt = Runtime(profiles={"test": ["sleep", "30"]}, grace_sec=3.0)
    rt.backend.validate_env = DockerBackend.validate_env  # the docker backend's API-time check
    client = TestClient(create_app(rt, deliver=events.append))
    env = {"VEXA_MOUNTS": json.dumps([{"slug": "pwn", "source": "/", "path": "/host"}])}
    r = client.post("/workloads", json={"workloadId": "evil", "profile": "test", "env": env})
    assert r.status_code == 400 and "forbidden host path" in r.json()["detail"]
    assert client.get("/workloads/evil").status_code == 404
    assert events == []


@pytest.mark.parametrize("extra", [
    {"privileged": True}, {"capAdd": ["SYS_ADMIN"]}, {"networkMode": "host"},
    {"binds": ["/:/host"]}, {"hostConfig": {"Privileged": True}}, {"image": "alpine"},
])
def test_spec_cannot_carry_host_config_fields(extra):
    client = _client()
    body = {"workloadId": "w2", "profile": "test", "env": {}, **extra}
    assert client.post("/workloads", json=body).status_code == 422


# ── docker backend host-config guard ──────────────────────────────────────────

@pytest.mark.parametrize("hc", [
    {"Privileged": True},
    {"CapAdd": ["SYS_ADMIN"]},
    {"PidMode": "host"},
    {"NetworkMode": "host"},
    {"NetworkMode": "container:vexa-v012-runtime-1"},
    {"Binds": ["/var/run/docker.sock:/var/run/docker.sock"]},
    {"Binds": ["/run/containerd/containerd.sock:/c.sock:ro"]},
    {"Mounts": [{"Type": "bind", "Source": "/var/run/docker.sock", "Target": "/s"}]},
])
def test_unsafe_host_config_is_refused(hc):
    with pytest.raises(ValueError):
        _assert_safe_host_config(hc)


def test_safe_host_config_passes():
    _assert_safe_host_config({"NetworkMode": "vexa-v012_vexa", "ShmSize": 2 << 30,
                              "Binds": ["/srv/creds.json:/root/.claude/.credentials.json:ro"]})


def test_docker_start_refuses_socket_before_touching_the_daemon(monkeypatch):
    """Even a server-side misconfiguration (HOST_CLAUDE_CREDENTIALS pointed at the socket) never
    reaches the daemon's create call."""
    monkeypatch.setenv("HOST_CLAUDE_CREDENTIALS", "/var/run/docker.sock")
    be = DockerBackend()
    calls = []
    monkeypatch.setattr(be, "_req", lambda *a, **k: calls.append(a))
    with pytest.raises(ValueError):
        be.start("mtg-1-abc", Runnable(image="vexaai/vexa-bot:v012"), {"VEXA_BOT_CONFIG": "{}"})
    assert calls == []


def test_docker_start_refuses_request_host_mount_before_touching_the_daemon(monkeypatch):
    monkeypatch.setenv("RUNTIME_MOUNT_SOURCE_ALLOWLIST", "")
    monkeypatch.delenv("HOST_CLAUDE_CREDENTIALS", raising=False)
    be = DockerBackend()
    calls = []
    monkeypatch.setattr(be, "_req", lambda *a, **k: calls.append(a))
    env = {"VEXA_MOUNTS": json.dumps([{"slug": "s", "source": "/var/run/docker.sock", "path": "/s"}])}
    with pytest.raises(MountRejected):
        be.start("mtg-1-abc", Runnable(image="vexaai/vexa-bot:v012"), env)
    assert calls == []
