"""workspace_attach — the custom-repo clone URL policy (SSRF / file:// / token-exfiltration guard).

A custom workspace repo URL is user-supplied and cloned server-side, so production only clones
``https://`` from an allowlisted host set (``VEXA_WORKSPACE_GIT_HOSTS``, default github.com), rejects
embedded credentials and option-looking values, restricts git to the https transport, passes the URL
after ``--``, and only ever sends the GitHub token to github.com.
"""
from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient

from control_plane import workspace_attach as wa
from control_plane.api import create_app
from control_plane.dispatch import Dispatcher
from control_plane.workspace_reader import WorkspaceReader
from shared.config import load_settings


@pytest.fixture(autouse=True)
def _production_policy(monkeypatch):
    monkeypatch.setattr(wa, "_ALLOW_LOCAL_CLONES", False)  # undo conftest's local-repo seam
    monkeypatch.delenv("VEXA_WORKSPACE_GIT_HOSTS", raising=False)


@pytest.fixture
def git_calls(monkeypatch):
    """Record (and fake) the clone sequence's git calls; every other subprocess runs for real."""
    calls: list = []
    real_run = subprocess.run

    def fake_run(argv, **kw):
        argv = list(argv)
        if not ("clone" in argv or "set-url" in argv or "checkout" in argv):
            return real_run(argv, **kw)
        calls.append({"argv": argv, "env": kw.get("env") or {}})
        if "clone" in argv:
            real_run(["git", "init", "-q", argv[-1]], check=True, capture_output=True)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(wa.subprocess, "run", fake_run)
    return calls


@pytest.mark.parametrize("url", [
    "file:///etc",
    "/etc",
    "http://github.com/o/r.git",
    "ssh://git@github.com/o/r.git",
    "git@github.com:o/r.git",
    "ext::sh -c touch% /tmp/pwned",
    "git://github.com/o/r.git",
    "https://user:tok@github.com/o/r.git",
    "https://tok@github.com/o/r.git",
    "https://github.com@evil.example/o/r.git",
    "https://evil.example/o/r.git",
    "https://github.com.evil.example/o/r.git",
    "https://169.254.169.254/latest/meta-data",
    "https://localhost/o/r.git",
    "https://github.com:8443/o/r.git",
    "https://github.com/",
    "https://github.com",
    "-uhttps://github.com/o/r.git",
    "--upload-pack=touch /tmp/pwned",
    "https://github.com/o/r.git\n--upload-pack=x",
    "https://github.com\\@evil.example/o/r.git",
    "",
])
def test_disallowed_urls_are_rejected(url):
    with pytest.raises(wa.RepoUrlError):
        wa.validate_repo_url(url)


@pytest.mark.parametrize("url", [
    "https://github.com/o/r.git",
    "https://github.com/o/r",
    "https://GitHub.com/o/r.git",
    "https://github.com:443/o/r.git",
])
def test_allowed_github_urls(url):
    assert wa.validate_repo_url(url) == "github.com"


def test_host_allowlist_is_configurable(monkeypatch):
    monkeypatch.setenv("VEXA_WORKSPACE_GIT_HOSTS", "gitlab.example.com, git.corp.example")
    assert wa.validate_repo_url("https://gitlab.example.com/o/r.git") == "gitlab.example.com"
    assert wa.validate_repo_url("https://git.corp.example/o/r.git") == "git.corp.example"
    with pytest.raises(wa.RepoUrlError):  # the default is replaced, not extended
        wa.validate_repo_url("https://github.com/o/r.git")


def test_rejected_url_never_runs_git(git_calls, tmp_path):
    with pytest.raises(wa.RepoUrlError):
        wa._git_clone("file:///etc", "main", tmp_path / "d", token="SEKRET")
    with pytest.raises(wa.RepoUrlError):
        wa._git_clone("https://evil.example/o/r.git", "main", tmp_path / "d", token="SEKRET")
    assert git_calls == []


def test_option_looking_ref_is_rejected(git_calls, tmp_path):
    with pytest.raises(wa.RepoUrlError):
        wa._git_clone("https://github.com/o/r.git", "--orphan=x", tmp_path / "d")
    assert git_calls == []


def test_github_clone_is_https_only_and_url_after_double_dash(git_calls, tmp_path):
    wa._git_clone("https://github.com/o/r.git", "main", tmp_path / "d", token="SEKRET")
    clone = next(c for c in git_calls if "clone" in c["argv"])
    argv = clone["argv"]
    assert argv[:5] == ["git", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always"]
    i = argv.index("--")
    assert argv[i + 1] == "https://SEKRET@github.com/o/r.git"  # token only for github.com
    assert clone["env"]["GIT_ALLOW_PROTOCOL"] == "https"
    # the persisted origin is reset to the token-free URL
    assert any(c["argv"][-3:] == ["set-url", "origin", "https://github.com/o/r.git"] for c in git_calls)


def test_token_never_sent_to_a_non_github_host(git_calls, tmp_path, monkeypatch):
    monkeypatch.setenv("VEXA_WORKSPACE_GIT_HOSTS", "gitlab.example.com")
    wa._git_clone("https://gitlab.example.com/o/r.git", "main", tmp_path / "d", token="SEKRET")
    assert git_calls and all("SEKRET" not in " ".join(c["argv"]) for c in git_calls)


def test_local_repo_rejected_without_test_seam(tmp_path):
    src = tmp_path / "src"
    subprocess.run(["git", "init", "-q", str(src)], check=True, capture_output=True)
    with pytest.raises(wa.RepoUrlError):
        wa._git_clone(str(src), "", tmp_path / "d")
    assert not (tmp_path / "d").exists()


class _FakeRuntime:
    def spawn(self, workload_id, profile, env):
        return workload_id

    def await_done(self, workload_id, timeout_sec=0.0):
        return "completed"


class _FakeIdentity:
    def mint(self, subject, launcher, workspaces, tools):
        return "tok"


@pytest.mark.parametrize("route", ["/api/workspace/swap", "/api/workspace/activate"])
def test_api_rejects_disallowed_repo_with_400(route, git_calls, tmp_path):
    from shared.seeding import seed_workspace

    ws = tmp_path / "u_jane"
    ws.mkdir()
    (ws / "CLAUDE.md").write_text("SEED")
    seed_workspace(ws, None)
    git_calls.clear()
    c = TestClient(create_app(Dispatcher(load_settings(), _FakeRuntime(), _FakeIdentity()),
                              reader=WorkspaceReader(str(tmp_path))))
    r = c.post(route, json={"repo": "file:///etc", "ref": "main"})
    assert r.status_code == 400, r.text
    assert "not allowed" in r.json()["detail"]
    assert not any("clone" in call["argv"] for call in git_calls)
