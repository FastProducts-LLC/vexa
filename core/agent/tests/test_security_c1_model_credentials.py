"""Security finding C1 (2026-10-25): deployment model credentials must never be sent to a
subject-supplied ``base_url``.

Before the fix, ``PUT /user/models {"mode":"custom","base_url":"https://attacker.example"}`` with
no ``api_key`` made ``overlay_model_config`` point the worker at the attacker while
``build_unit_env``'s passthrough still stamped the deployment's ``ANTHROPIC_AUTH_TOKEN`` /
``ANTHROPIC_API_KEY`` / ``VEXA_LLM_API_KEY`` / ``CLAUDE_CODE_OAUTH_TOKEN`` — and the runtime's
docker backend added the same keys plus the subscription mount. ``run_models_test`` (the Settings
→ Models "Test" button) did the same synchronously.

These tests pin the pairing rule at every layer agent-api owns: the endpoint policy module, the
dispatch overlay + passthrough, the Test button, and the chat preflight.
"""
from __future__ import annotations

import pytest

from control_plane import config_test as ct
from control_plane import dispatch
from control_plane import model_endpoint as me
from shared.config import load_settings

ATTACKER = "https://attacker.example"

# Minimal fakes (the same shapes test_unit_foundation / test_api use; tests/ is not a package).
VALID_INV = {
    "identity": {"subject": "u_jane", "launcher": "user:u_jane"},
    "runner": "claude-code",
    "workspaces": [{"id": "u_jane", "mode": "rw"}],
    "trigger": "message",
    "context": {"kind": "none"},
    "start": {"entrypoint": {"inline": "hi"}},
}


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


class _FakeModelConfig:
    def __init__(self, config=None, error=None):
        self._config, self._error = config or {}, error

    def resolve(self, subject):
        if self._error:
            raise self._error
        return dict(self._config)


@pytest.fixture(autouse=True)
def _deployment_credentials(monkeypatch):
    """Every test runs with a FULL set of deployment credentials in agent-api's env — the
    precondition of the leak."""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "deployment-auth-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "deployment-api-key")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "deployment-oauth")
    monkeypatch.setenv("VEXA_LLM_API_KEY", "deployment-llm-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://deployment-gateway.example.com")
    monkeypatch.setenv("VEXA_LLM_BASE_URL", "https://deployment-gateway.example.com")
    monkeypatch.delenv("VEXA_MODEL_BASE_URL_ALLOWLIST", raising=False)
    monkeypatch.delenv("VEXA_MODEL_ALLOW_PRIVATE_ENDPOINTS", raising=False)


DEPLOYMENT_SECRETS = ("deployment-auth-token", "deployment-api-key", "deployment-oauth",
                      "deployment-llm-key")


def _spawn_env(model_config, **settings_kw) -> dict:
    rt = _FakeRuntime()
    d = dispatch.Dispatcher(load_settings(**settings_kw), rt, _FakeIdentity(),
                            model_config=_FakeModelConfig(model_config))
    d.dispatch(VALID_INV)
    _, _profile, env = rt.spawned[0]
    return env


def _assert_no_deployment_secret(env: dict) -> None:
    leaked = {k: v for k, v in env.items() if v in DEPLOYMENT_SECRETS}
    assert not leaked, f"deployment credential reached a subject-supplied endpoint: {leaked}"


# ── the leak itself ───────────────────────────────────────────────────────────────────────────

def test_user_base_url_without_key_never_carries_deployment_credentials():
    """THE finding: custom + attacker base_url + no api_key → the worker must carry NO deployment
    credential, be marked custom for the runtime brokers, and still point at the user's URL (so it
    fails loudly there with a missing-credential error rather than silently using defaults)."""
    env = _spawn_env({"mode": "custom", "base_url": ATTACKER})
    assert env["ANTHROPIC_BASE_URL"] == ATTACKER
    assert env["VEXA_LLM_BASE_URL"] == ATTACKER
    assert env[dispatch.MODEL_MODE_ENV] == dispatch.MODEL_MODE_CUSTOM
    for key in dispatch.MODEL_CREDENTIAL_ENV_KEYS:
        assert key not in env, key
    _assert_no_deployment_secret(env)


def test_user_base_url_with_key_carries_only_the_users_key():
    env = _spawn_env({"mode": "custom", "base_url": ATTACKER, "api_key": "sk-user"})
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-user"
    assert env["VEXA_LLM_API_KEY"] == "sk-user"
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert env[dispatch.MODEL_MODE_ENV] == dispatch.MODEL_MODE_CUSTOM
    _assert_no_deployment_secret(env)


def test_explicit_user_source_is_strict_too():
    env = _spawn_env({"mode": "custom", "base_url": ATTACKER, "endpoint_source": "user"})
    _assert_no_deployment_secret(env)
    assert env[dispatch.MODEL_MODE_ENV] == dispatch.MODEL_MODE_CUSTOM


def test_subscription_mode_keeps_deployment_credentials_and_no_marker():
    env = _spawn_env({"mode": "subscription", "base_url": ATTACKER})
    assert env["ANTHROPIC_AUTH_TOKEN"] == "deployment-auth-token"
    assert env["ANTHROPIC_BASE_URL"] == "https://deployment-gateway.example.com"
    assert dispatch.MODEL_MODE_ENV not in env


def test_custom_without_base_url_is_inert_and_unmarked():
    env = _spawn_env({"mode": "custom", "api_key": "sk-user"})
    assert env["ANTHROPIC_AUTH_TOKEN"] == "deployment-auth-token"
    assert env["ANTHROPIC_BASE_URL"] == "https://deployment-gateway.example.com"
    assert dispatch.MODEL_MODE_ENV not in env


def test_platform_chosen_endpoint_may_pair_with_deployment_credentials():
    """An OPERATOR-set platform endpoint (admin-api says endpoint_source=platform) keeps the
    pre-C1 semantics: operator gateway + the deployment's own key — that is what they configured."""
    env = _spawn_env({"mode": "custom", "base_url": "http://litellm:4000",
                      "endpoint_source": "platform"})
    assert env["ANTHROPIC_BASE_URL"] == "http://litellm:4000"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "deployment-auth-token"
    assert dispatch.MODEL_MODE_ENV not in env


# ── endpoint policy at dispatch ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "http://admin-api:8001",            # dotless compose service name
    "http://127.0.0.1:8090",            # loopback
    "http://10.0.0.5",                  # RFC1918
    "http://169.254.169.254/latest",    # cloud metadata
    "http://[::1]:8080",                # v6 loopback
    "http://localhost:4000",
    "http://gw.internal",
    "https://user:pw@gw.example.com",   # userinfo
    "ftp://gw.example.com",
])
def test_private_or_malformed_user_endpoint_is_dropped_to_deployment_default(url):
    env = _spawn_env({"mode": "custom", "base_url": url, "api_key": "sk-user"})
    # Dropped: deployment endpoint + deployment credential, exactly as if nothing was set.
    assert env["ANTHROPIC_BASE_URL"] == "https://deployment-gateway.example.com"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "deployment-auth-token"
    assert dispatch.MODEL_MODE_ENV not in env
    assert "sk-user" not in env.values()


def test_private_endpoint_allowed_with_operator_opt_in():
    env = _spawn_env({"mode": "custom", "base_url": "http://litellm:4000", "api_key": "sk-user"},
                     model_allow_private_endpoints=True)
    assert env["ANTHROPIC_BASE_URL"] == "http://litellm:4000"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-user"
    _assert_no_deployment_secret(env)


def test_host_allowlist_gates_user_endpoints():
    allow = "gw.example.com,.openrouter.ai"
    ok = _spawn_env({"mode": "custom", "base_url": "https://api.openrouter.ai/v1", "api_key": "k"},
                    model_base_url_allowlist=allow)
    assert ok["ANTHROPIC_BASE_URL"] == "https://api.openrouter.ai/v1"
    exact = _spawn_env({"mode": "custom", "base_url": "https://gw.example.com", "api_key": "k"},
                       model_base_url_allowlist=allow)
    assert exact["ANTHROPIC_BASE_URL"] == "https://gw.example.com"
    refused = _spawn_env({"mode": "custom", "base_url": ATTACKER, "api_key": "k"},
                         model_base_url_allowlist=allow)
    assert refused["ANTHROPIC_BASE_URL"] == "https://deployment-gateway.example.com"
    assert refused["ANTHROPIC_AUTH_TOKEN"] == "deployment-auth-token"
    # suffix entries do not admit look-alikes
    lookalike = _spawn_env({"mode": "custom", "base_url": "https://evilopenrouter.ai", "api_key": "k"},
                           model_base_url_allowlist=allow)
    assert lookalike["ANTHROPIC_BASE_URL"] == "https://deployment-gateway.example.com"


# ── the policy module ─────────────────────────────────────────────────────────────────────────

def test_endpoint_host_problem_public_host_ok():
    assert me.endpoint_host_problem("https://gw.example.com/v1") is None
    assert me.endpoint_host_problem("https://8.8.8.8") is None


def test_endpoint_host_problem_allowlist_and_private():
    assert "not on" in me.endpoint_host_problem("https://x.example.org", allowlist=("gw.example.com",))
    assert me.endpoint_host_problem("https://a.b.example.org", allowlist=(".example.org",)) is None
    assert me.endpoint_host_problem("https://example.org", allowlist=(".example.org",)) is None
    assert "private" in me.endpoint_host_problem("http://redis:6379")
    assert me.endpoint_host_problem("http://redis:6379", allow_private=True) is None
    assert me.endpoint_host_problem("") == "base_url is empty"
    assert me.parse_allowlist(" A.example.com , .B.io ,, ") == ("a.example.com", ".b.io")
    assert me.allow_private_from_env("1") and me.allow_private_from_env("TRUE")
    assert not me.allow_private_from_env("") and not me.allow_private_from_env("0")


@pytest.mark.parametrize("host,private", [
    ("localhost", True), ("foo.localhost", True), ("gw.local", True), ("gw.internal", True),
    ("admin-api", True), ("192.168.1.1", True), ("172.16.0.1", True), ("fd00::1", True),
    ("fe80::1", True), ("0.0.0.0", True), ("224.0.0.1", True),
    ("gw.example.com", False), ("1.1.1.1", False), ("2606:4700:4700::1111", False),
])
def test_is_private_host(host, private):
    assert me.is_private_host(host) is private


# ── Settings → Models "Test" button ───────────────────────────────────────────────────────────

def test_models_test_never_posts_deployment_key_to_user_endpoint():
    calls = []

    def post(url, payload, headers):
        calls.append((url, dict(headers)))
        return 200, "{}"

    env = {"ANTHROPIC_BASE_URL": "https://deployment-gateway.example.com",
           "ANTHROPIC_AUTH_TOKEN": "deployment-auth-token", "ANTHROPIC_API_KEY": "deployment-api-key"}
    out = ct.run_models_test({"mode": "custom", "base_url": ATTACKER}, env=env, post=post)
    assert calls == []                      # nothing was sent anywhere
    assert out["mode"] == "custom" and not out["ok"]
    assert "no API key" in out["summary"]
    assert "deployment-auth-token" not in str(out)


def test_models_test_uses_user_key_for_user_endpoint():
    calls = []

    def post(url, payload, headers):
        calls.append((url, dict(headers)))
        return 200, "{}"

    env = {"ANTHROPIC_AUTH_TOKEN": "deployment-auth-token"}
    out = ct.run_models_test({"mode": "custom", "base_url": ATTACKER, "api_key": "sk-user"},
                             env=env, post=post)
    assert out["ok"]
    assert calls and all(url.startswith(ATTACKER) for url, _ in calls)
    sent = " ".join(" ".join(h.values()) for _, h in calls)
    assert "sk-user" in sent and "deployment-auth-token" not in sent


def test_models_test_refuses_private_user_endpoint_without_posting():
    calls = []
    out = ct.run_models_test({"mode": "custom", "base_url": "http://admin-api:8001", "api_key": "k"},
                             env={}, post=lambda u, p, h: calls.append(u) or (200, "{}"))
    assert calls == [] and not out["ok"] and "private" in out["summary"]
    out = ct.run_models_test({"mode": "custom", "base_url": ATTACKER, "api_key": "k"},
                             env={"VEXA_MODEL_BASE_URL_ALLOWLIST": "gw.example.com"},
                             post=lambda u, p, h: calls.append(u) or (200, "{}"))
    assert calls == [] and not out["ok"] and "not on" in out["summary"]


def test_models_test_platform_endpoint_may_use_deployment_key():
    calls = []

    def post(url, payload, headers):
        calls.append((url, dict(headers)))
        return 200, "{}"

    env = {"ANTHROPIC_AUTH_TOKEN": "deployment-auth-token"}
    out = ct.run_models_test({"mode": "custom", "base_url": "http://litellm:4000",
                              "endpoint_source": "platform"}, env=env, post=post)
    assert out["ok"] and calls and calls[0][0].startswith("http://litellm:4000")


def test_models_test_custom_without_endpoint_tests_deployment_pair():
    calls = []

    def post(url, payload, headers):
        calls.append(url)
        return 200, "{}"

    env = {"ANTHROPIC_BASE_URL": "https://deployment-gateway.example.com",
           "ANTHROPIC_AUTH_TOKEN": "deployment-auth-token"}
    out = ct.run_models_test({"mode": "custom"}, env=env, post=post)
    assert out["ok"] and calls[0].startswith("https://deployment-gateway.example.com")


# ── chat preflight ────────────────────────────────────────────────────────────────────────────

def test_chat_preflight_refuses_keyless_user_endpoint_when_deployment_has_no_credential(monkeypatch):
    """With NO deployment credential, a keyless custom endpoint delivers nothing (C1 blocks the
    fallback), so the turn is refused up front with the actionable message — no worker spawn."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("_test_api_helpers", Path(__file__).with_name("test_api.py"))
    test_api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(test_api)

    test_api._clear_model_creds(monkeypatch)
    c, runtime = test_api._preflight_client(_FakeModelConfig({"mode": "custom", "base_url": ATTACKER}))
    r = c.post("/api/chat", json={"prompt": "hi", "subject": "u_jane", "session": "s1"})
    assert r.status_code == 200 and '"error"' in r.text
    assert runtime.spawned == []
