"""Security finding C1 (2026-10-25) — the runtime's side of the pairing rule.

The runtime BROKERS the deployment's model credentials into every worker it spawns: the docker
backend bind-mounts ``HOST_CLAUDE_CREDENTIALS`` and copies ``ANTHROPIC_*`` / ``VEXA_LLM_*`` from
its own env for keys the dispatch did not stamp; the process backend stages
``~/.claude/.credentials.json`` into the subject HOME. A dispatch whose endpoint was chosen by the
SUBJECT (agent-api stamps ``VEXA_MODEL_MODE=custom`` next to the subject's own base_url + key)
must get NONE of that — otherwise the deployment's paid key / OAuth token travels to a host a
tenant typed into Settings → Models.
"""
from __future__ import annotations

import json
import os

import pytest

from runtime_kernel import docker_backend as dbk
from runtime_kernel import isolation as iso
from runtime_kernel.docker_backend import DockerBackend
from runtime_kernel.profiles import Runnable


class _Resp:
    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code = status_code
        self._body = body or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class _Recorder:
    """Stands in for DockerBackend._req: records the /containers/create payload."""

    def __init__(self):
        self.created: list[dict] = []

    def __call__(self, method, path, **kw):
        if method == "POST" and path.startswith("/containers/create"):
            self.created.append(kw["json"])
            return _Resp(201, {"Id": "cid"})
        if method == "POST" and path.endswith("/start"):
            return _Resp(204)
        return _Resp(404)


@pytest.fixture()
def deployment_env(monkeypatch, tmp_path):
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    monkeypatch.setenv("HOST_CLAUDE_CREDENTIALS", str(creds))
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "deployment-auth-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "deployment-api-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://deployment-gateway.example.com")
    monkeypatch.setenv("VEXA_LLM_API_KEY", "deployment-llm-key")
    monkeypatch.setenv("VEXA_LLM_BASE_URL", "https://deployment-gateway.example.com")
    monkeypatch.setenv("VEXA_LLM_MODEL", "deployment-model")
    monkeypatch.setenv("VEXA_RUNNER", "claude-code")
    monkeypatch.delenv("DOCKER_NETWORK", raising=False)
    monkeypatch.delenv("VEXA_AGENT_SRC_MOUNT", raising=False)
    return str(creds)


def _spawn(env: dict[str, str]) -> dict:
    be = DockerBackend()
    rec = _Recorder()
    be._req = rec  # type: ignore[method-assign]
    be.start("w1", Runnable(image="worker:img", command=None), env)
    assert len(rec.created) == 1
    return rec.created[0]


def _env_map(payload: dict) -> dict[str, str]:
    return dict(kv.split("=", 1) for kv in payload["Env"])


def test_default_dispatch_is_brokered_deployment_credentials(deployment_env):
    """Baseline (unchanged): a normal dispatch gets the subscription mount + the deployment's
    endpoint/credential env for every key it did not carry itself."""
    payload = _spawn({"VEXA_OWNER": "17"})
    binds = payload["HostConfig"].get("Binds", [])
    assert f"{deployment_env}:/root/.claude/.credentials.json:ro" in binds
    env = _env_map(payload)
    assert env["ANTHROPIC_AUTH_TOKEN"] == "deployment-auth-token"
    assert env["ANTHROPIC_API_KEY"] == "deployment-api-key"
    assert env["VEXA_LLM_API_KEY"] == "deployment-llm-key"
    assert env["ANTHROPIC_BASE_URL"] == "https://deployment-gateway.example.com"


def test_subject_supplied_endpoint_gets_no_deployment_credentials(deployment_env):
    """THE fix: VEXA_MODEL_MODE=custom → no subscription mount, no ANTHROPIC_*/VEXA_LLM_* copied
    from the runtime env; the subject's own base_url + key (dispatch-stamped) are all that travel."""
    payload = _spawn({
        "VEXA_OWNER": "17",
        "VEXA_MODEL_MODE": "custom",
        "ANTHROPIC_BASE_URL": "https://attacker.example",
        "VEXA_LLM_BASE_URL": "https://attacker.example",
        "ANTHROPIC_AUTH_TOKEN": "sk-user",
        "VEXA_LLM_API_KEY": "sk-user",
    })
    binds = payload["HostConfig"].get("Binds", [])
    assert not any(".credentials.json" in b for b in binds), binds
    env = _env_map(payload)
    assert env["ANTHROPIC_BASE_URL"] == "https://attacker.example"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-user"
    assert env["VEXA_LLM_API_KEY"] == "sk-user"
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    secrets = ("deployment-auth-token", "deployment-api-key", "deployment-llm-key")
    leaked = {k: v for k, v in env.items() if v in secrets}
    assert leaked == {}, leaked
    assert env.get("VEXA_LLM_BASE_URL") == "https://attacker.example"   # dispatch-stamped, not the runtime's
    # non-credential dials still flow (model name, runner selection)
    assert env["VEXA_LLM_MODEL"] == "deployment-model"
    assert env["VEXA_RUNNER"] == "claude-code"


def test_subject_supplied_endpoint_without_key_stays_credential_less(deployment_env):
    """A keyless custom dispatch must NOT be "helped" by the runtime: it reaches the worker with
    no credential at all (fails loudly in the worker), never with the deployment's."""
    payload = _spawn({"VEXA_OWNER": "17", "VEXA_MODEL_MODE": "custom",
                      "ANTHROPIC_BASE_URL": "https://attacker.example"})
    env = _env_map(payload)
    for key in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "VEXA_LLM_API_KEY",
                "CLAUDE_CODE_OAUTH_TOKEN"):
        assert key not in env, key
    assert not any(".credentials.json" in b for b in payload["HostConfig"].get("Binds", []))


def test_marker_is_case_and_whitespace_tolerant_but_exact():
    assert dbk.subject_supplied_endpoint({"VEXA_MODEL_MODE": "custom"})
    assert dbk.subject_supplied_endpoint({"VEXA_MODEL_MODE": " Custom "})
    assert not dbk.subject_supplied_endpoint({"VEXA_MODEL_MODE": "subscription"})
    assert not dbk.subject_supplied_endpoint({"VEXA_MODEL_MODE": ""})
    assert not dbk.subject_supplied_endpoint({})
    assert not dbk.subject_supplied_endpoint(None)


# ── process backend (lite): the subscription file is not staged for a subject endpoint ─────────

def _plan_env(**extra):
    env = {"VEXA_WORKSPACE_MOUNT_TARGET": "/workspaces", "VEXA_OWNER": "17",
           "VEXA_MOUNTS": json.dumps([{"slug": "seed", "path": "/workspaces/17", "role": "private",
                                       "write": True, "primary": True}])}
    env.update(extra)
    return env


def test_isolation_plan_flags_subject_endpoint():
    assert iso.plan_process_isolation(_plan_env(), euid=0).stage_subscription_creds is True
    plan = iso.plan_process_isolation(_plan_env(VEXA_MODEL_MODE="custom"), euid=0)
    assert plan.stage_subscription_creds is False


def test_apply_skips_credential_staging_for_subject_endpoint(tmp_path, monkeypatch):
    """apply() copies ~/.claude/.credentials.json into the subject HOME normally; with the flag
    off the HOME gets no credentials file (everything else is still applied)."""
    home_src = tmp_path / "roothome"
    (home_src / ".claude").mkdir(parents=True)
    (home_src / ".claude" / ".credentials.json").write_text("{\"claudeAiOauth\": {}}")
    monkeypatch.setenv("HOME", str(home_src))
    monkeypatch.setattr(os, "chown", lambda *a, **k: None)
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
    monkeypatch.setattr(iso, "_chown_tree", lambda *a, **k: None)
    monkeypatch.setattr(iso, "_sweep_default_deny", lambda *a, **k: None)
    root = tmp_path / "store"
    root.mkdir()

    def _plan(stage: bool, subject: str):
        home = root / ".home" / subject
        return iso.ProcessIsolation(uid=100017, gid=100017, store_root=str(root), home=str(home),
                                    private=(), shared=(), stage_subscription_creds=stage)

    iso.apply_process_isolation(_plan(True, "17"))
    assert (root / ".home" / "17" / ".claude" / ".credentials.json").is_file()
    iso.apply_process_isolation(_plan(False, "18"))
    assert (root / ".home" / "18").is_dir()
    assert not (root / ".home" / "18" / ".claude" / ".credentials.json").exists()
