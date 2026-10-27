"""Security finding #4 — spawned workload containers are hardened per workload class.

The docker backend's create payload carries CapDrop ALL, no-new-privileges, memory/pids/CPU
ceilings (configurable, with safe defaults) and, opt-in, a read-only rootfs / non-root user. The C5
host-config guard accepts exactly the SecurityOpt values the runtime sets and still refuses
user-supplied ones and Privileged/CapAdd.
"""
from __future__ import annotations

import pytest

from runtime_kernel.docker_backend import DockerBackend, _assert_safe_host_config
from runtime_kernel.hardening import BOT_TMPFS, hardening_host_config
from runtime_kernel.profiles import Runnable, default_registry

_ENV_KEYS = (
    "RUNTIME_DOCKER_HARDENING",
    "RUNTIME_DOCKER_BOT_MEMORY", "RUNTIME_DOCKER_BOT_PIDS_LIMIT", "RUNTIME_DOCKER_BOT_CPUS",
    "RUNTIME_DOCKER_AGENT_MEMORY", "RUNTIME_DOCKER_AGENT_PIDS_LIMIT", "RUNTIME_DOCKER_AGENT_CPUS",
    "RUNTIME_DOCKER_BOT_READONLY_ROOTFS", "RUNTIME_DOCKER_BOT_USER",
    "HOST_CLAUDE_CREDENTIALS", "VEXA_AGENT_SRC_MOUNT", "DOCKER_NETWORK",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}
        self.text = ""

    def json(self):
        return self._body


def _capture_create(monkeypatch, runnable, env=None):
    """Run DockerBackend.start against a fake daemon; return the /containers/create payload."""
    be = DockerBackend()
    calls = []

    def fake_req(method, path, **kw):
        calls.append((method, path, kw))
        if path.startswith("/containers/create"):
            return _Resp(201, {"Id": "cid"})
        return _Resp(204)

    monkeypatch.setattr(be, "_req", fake_req)
    be.start("mtg-1-abc", runnable, env or {"VEXA_BOT_CONFIG": "{}"})
    (create,) = [c for c in calls if c[1].startswith("/containers/create")]
    return create[2]["json"]


GIB = 1024**3


def test_meeting_bot_payload_is_hardened_by_default(monkeypatch):
    payload = _capture_create(
        monkeypatch, Runnable(image="vexaai/vexa-bot:v012", workload_class="meeting-bot"))
    hc = payload["HostConfig"]
    assert hc["CapDrop"] == ["ALL"]
    assert "CapAdd" not in hc
    assert hc["SecurityOpt"] == ["no-new-privileges:true"]
    assert hc["Memory"] == 4 * GIB and hc["MemorySwap"] == 4 * GIB
    assert hc["PidsLimit"] == 1024
    assert hc["NanoCpus"] == 2_000_000_000
    assert hc["ShmSize"] == 2 * GIB             # the existing Chromium /dev/shm survives
    # opt-ins stay off for the stock image
    assert "ReadonlyRootfs" not in hc and "Tmpfs" not in hc
    assert "User" not in payload
    assert not hc.get("Privileged")


def test_agent_payload_gets_agent_limits(monkeypatch):
    hc = _capture_create(
        monkeypatch, Runnable(image="vexaai/v012-agent-worker:dev", workload_class="agent"))["HostConfig"]
    assert hc["CapDrop"] == ["ALL"] and hc["SecurityOpt"] == ["no-new-privileges:true"]
    assert hc["Memory"] == 2 * GIB and hc["PidsLimit"] == 512 and hc["NanoCpus"] == 1_000_000_000


def test_unclassified_workload_is_hardened_too(monkeypatch):
    hc = _capture_create(monkeypatch, Runnable(image="alpine", command=["sleep", "1"]))["HostConfig"]
    assert hc["CapDrop"] == ["ALL"] and hc["SecurityOpt"] == ["no-new-privileges:true"]
    assert hc["Memory"] == 2 * GIB


def test_default_registry_classes_the_two_profiles(monkeypatch):
    monkeypatch.setenv("BROWSER_IMAGE", "vexaai/vexa-bot:v012")
    monkeypatch.setenv("AGENT_IMAGE", "vexaai/v012-agent-api:dev")
    reg = default_registry()
    assert reg.resolve("meeting-bot").workload_class == "meeting-bot"
    assert reg.resolve("agent").workload_class == "agent"


def test_limits_are_configurable(monkeypatch):
    monkeypatch.setenv("RUNTIME_DOCKER_BOT_MEMORY", "3072m")
    monkeypatch.setenv("RUNTIME_DOCKER_BOT_PIDS_LIMIT", "2048")
    monkeypatch.setenv("RUNTIME_DOCKER_BOT_CPUS", "1.5")
    hc = hardening_host_config("meeting-bot")
    assert hc["Memory"] == 3 * GIB and hc["MemorySwap"] == 3 * GIB
    assert hc["PidsLimit"] == 2048 and hc["NanoCpus"] == 1_500_000_000


def test_zero_removes_a_ceiling_but_keeps_the_rest(monkeypatch):
    monkeypatch.setenv("RUNTIME_DOCKER_BOT_MEMORY", "0")
    hc = hardening_host_config("meeting-bot")
    assert "Memory" not in hc and "MemorySwap" not in hc
    assert hc["CapDrop"] == ["ALL"] and hc["PidsLimit"] == 1024


@pytest.mark.parametrize("key,value", [
    ("RUNTIME_DOCKER_BOT_MEMORY", "lots"),
    ("RUNTIME_DOCKER_BOT_PIDS_LIMIT", "1k"),
    ("RUNTIME_DOCKER_BOT_CPUS", "two"),
    ("RUNTIME_DOCKER_BOT_MEMORY", "-1g"),
    ("RUNTIME_DOCKER_HARDENING", "maybe"),
])
def test_unparseable_config_fails_loud(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        hardening_host_config("meeting-bot")


def test_off_switch_restores_the_old_payload(monkeypatch):
    monkeypatch.setenv("RUNTIME_DOCKER_HARDENING", "off")
    monkeypatch.setenv("RUNTIME_DOCKER_BOT_USER", "1001:1001")
    payload = _capture_create(
        monkeypatch, Runnable(image="vexaai/vexa-bot:v012", workload_class="meeting-bot"))
    hc = payload["HostConfig"]
    for k in ("CapDrop", "SecurityOpt", "Memory", "PidsLimit", "NanoCpus", "ReadonlyRootfs"):
        assert k not in hc
    assert "User" not in payload


def test_readonly_rootfs_and_user_are_opt_in(monkeypatch):
    monkeypatch.setenv("RUNTIME_DOCKER_BOT_READONLY_ROOTFS", "true")
    monkeypatch.setenv("RUNTIME_DOCKER_BOT_USER", "1001:1001")
    payload = _capture_create(
        monkeypatch, Runnable(image="vexaai/vexa-bot:v012", workload_class="meeting-bot"))
    hc = payload["HostConfig"]
    assert hc["ReadonlyRootfs"] is True
    assert hc["Tmpfs"] == BOT_TMPFS
    assert set(hc["Tmpfs"]) == {"/tmp", "/root", "/var/cache/fontconfig", "/app/storage"}
    assert payload["User"] == "1001:1001"


def test_bot_opt_ins_never_apply_to_agents(monkeypatch):
    monkeypatch.setenv("RUNTIME_DOCKER_BOT_READONLY_ROOTFS", "true")
    monkeypatch.setenv("RUNTIME_DOCKER_BOT_USER", "1001:1001")
    payload = _capture_create(monkeypatch, Runnable(image="w", workload_class="agent"))
    assert "ReadonlyRootfs" not in payload["HostConfig"] and "User" not in payload


# ── the C5 guard: runtime-set SecurityOpt allowed, anything else refused ──────

def test_guard_accepts_the_hardened_host_config():
    hc = {"NetworkMode": "vexa-v012_vexa", "ShmSize": 2 * GIB, **hardening_host_config("meeting-bot")}
    _assert_safe_host_config(hc)


@pytest.mark.parametrize("opt", [
    "seccomp=unconfined", "apparmor=unconfined", "label=disable", "label:disable",
    "systempaths=unconfined", "no-new-privileges:false", "no-new-privileges=false",
])
def test_guard_refuses_user_supplied_security_opt(opt):
    with pytest.raises(ValueError, match="SecurityOpt"):
        _assert_safe_host_config({"SecurityOpt": ["no-new-privileges:true", opt]})


@pytest.mark.parametrize("hc", [
    {"Privileged": True, "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"]},
    {"CapAdd": ["SYS_ADMIN"], "CapDrop": ["ALL"]},
    {"CapAdd": ["NET_RAW"]},
])
def test_guard_still_refuses_privileged_and_cap_add(hc):
    with pytest.raises(ValueError):
        _assert_safe_host_config(hc)
