"""Object-level authorization (BOLA/IDOR) on the agent control plane — cross-tenant + cross-role.

The pattern mirrors meeting-api's ``test_xtenant_isolation.py``: every route addressed by a caller-
supplied id (meeting row id, workspace slug, session name) must resolve ONLY objects the caller owns or
holds a grant on. Someone else's object → 404 (or the pre-existing 403 of the SSE gate); a valid object
with too low a role (a shared-workspace ``viewer`` attempting a write) → 403.

Offline: fake runtime/identity, monkeypatched redis, real git for the workspace-manage routes.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from control_plane import api as api_mod
from control_plane.api import ChatBody, ChatContextBody, _context_grounding, create_app
from control_plane.dispatch import Dispatcher
from control_plane.workspace_membership import MEMBERS_FILE
from control_plane.workspace_reader import WorkspaceReader
from shared.config import load_settings


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


OWNER, VIEWER, CONTRIB, STRANGER = "u_owner", "u_view", "u_contrib", "u_stranger"


def _lookup(rows: dict):
    """rows = {(subject, meeting_id): record}. The create_app ``meeting_owner_lookup`` seam — mirrors
    meeting-api GET /meetings/{id}: returns the row for the owner AND for a share/membership grantee
    (``shared: True``), None otherwise."""
    def _fn(subject, meeting_id):
        return rows.get((str(subject), str(meeting_id)))
    return _fn


_ROWS = {
    (OWNER, "42"): {"id": 42, "user_id": OWNER, "native_meeting_id": "abc-defg-hij", "shared": False},
    (VIEWER, "42"): {"id": 42, "user_id": OWNER, "native_meeting_id": "abc-defg-hij", "shared": True},
}


class _KV:
    """Minimal redis stand-in for /api/meeting/process (+ the tail probe /api/meeting/start does)."""

    def __init__(self):
        self.kv: dict = {}

    def set(self, k, v, ex=None):
        self.kv[k] = v

    def get(self, k):
        return self.kv.get(k)

    def delete(self, k):
        self.kv.pop(k, None)

    def xrevrange(self, stream, count=1):
        return []


@pytest.fixture
def kv(monkeypatch):
    import redis

    store = _KV()
    monkeypatch.setattr(redis, "from_url", lambda *_a, **_k: store)
    return store


def _client(runtime=None, *, root: Path | None = None, rows=None, **settings):
    if root is not None:
        settings["workspaces_dir"] = str(root)
    kwargs = {"reader": WorkspaceReader(str(root))} if root is not None else {}
    return TestClient(create_app(
        Dispatcher(load_settings(**settings), runtime or _FakeRuntime(), _FakeIdentity()),
        redis_url="redis://test", meeting_owner_lookup=_lookup(_ROWS if rows is None else rows), **kwargs,
    ))


# ── /api/meeting/process — the copilot opt-in flag is per meeting ROW, owner-only ───────────────────

def test_meeting_process_refuses_another_tenants_row(kv):
    c = _client()
    for on in (True, False):
        r = c.post("/api/meeting/process", json={"native_id": "abc-defg-hij", "meeting_id": "42", "on": on},
                   headers={"X-User-Id": STRANGER})
        assert r.status_code == 404, r.text
    assert "proc:meeting:42:on" not in kv.kv


def test_meeting_process_refuses_a_share_viewer(kv):
    """A transcript-share grant is READ access — it never lets the viewer arm/disarm the owner's copilot."""
    kv.kv["proc:meeting:42:on"] = "1"
    c = _client()
    r = c.post("/api/meeting/process", json={"native_id": "abc-defg-hij", "meeting_id": "42", "on": False},
               headers={"X-User-Id": VIEWER})
    assert r.status_code == 404
    assert kv.kv.get("proc:meeting:42:on") == "1"  # the owner's flag survived


def test_meeting_process_owner_still_works(kv):
    c = _client()
    r = c.post("/api/meeting/process", json={"native_id": "abc-defg-hij", "meeting_id": "42", "on": True},
               headers={"X-User-Id": OWNER})
    assert r.status_code == 202 and r.json()["processing"] is True
    assert kv.kv.get("proc:meeting:42:on") == "1"


def test_meeting_process_off_requires_identity(kv, monkeypatch):
    """OFF used to clear the flag BEFORE any identity check."""
    monkeypatch.delenv("VEXA_AGENT_DEFAULT_SUBJECT", raising=False)
    kv.kv["proc:meeting:42:on"] = "1"
    c = _client(agent_default_subject="")
    r = c.post("/api/meeting/process", json={"native_id": "abc-defg-hij", "meeting_id": "42", "on": False})
    assert r.status_code == 401
    assert kv.kv.get("proc:meeting:42:on") == "1"


# ── /api/meeting/start — a numeric id addresses a ROW carrier (tc:meeting:{row}) ────────────────────

def test_meeting_start_refuses_a_foreign_row_id(kv):
    runtime = _FakeRuntime()
    c = _client(runtime)
    r = c.post("/api/meeting/start", json={"platform": "google_meet", "native_id": "42"},
               headers={"X-User-Id": STRANGER})
    assert r.status_code == 404
    assert runtime.spawned == []  # no copilot was aimed at the victim's transcript


def test_meeting_start_owner_row_and_native_code_still_work(kv):
    runtime = _FakeRuntime()
    c = _client(runtime)
    assert c.post("/api/meeting/start", json={"platform": "google_meet", "native_id": "42"},
                  headers={"X-User-Id": OWNER}).status_code == 202
    # A true native code is never written by the collector (P0) → no foreign feed reachable → allowed.
    assert c.post("/api/meeting/start", json={"platform": "google_meet", "native_id": "xyz-abcd-efg"},
                  headers={"X-User-Id": STRANGER}).status_code == 202
    assert len(runtime.spawned) == 2


# ── /api/meeting/stream — the owner's copilot feed is OWNER-only (not share viewers) ────────────────

def test_meeting_stream_refuses_share_viewer_and_stranger(kv):
    c = _client()
    for who in (VIEWER, STRANGER):
        r = c.get("/api/meeting/stream", params={"meeting_id": "42", "session_uid": "42"},
                  headers={"X-User-Id": who})
        assert r.status_code == 403, (who, r.status_code)


# ── /api/chat grounding — never fold another tenant's meeting carriers into the prompt ──────────────

def _meeting_body(meeting_id):
    focus = {"kind": "meeting", "native_id": "abc-defg-hij", "meeting_id": meeting_id, "platform": "google_meet"}
    return ChatBody(prompt="summarize", context=ChatContextBody(focus=focus))


def test_chat_grounding_does_not_fold_a_foreign_meeting(monkeypatch):
    folded = []

    def fake_fold(active, session, prompt, redis_url):
        folded.append(active.get("meeting_id"))
        return ({"kind": "none", "session": session}, [], "<transcript>" + prompt)

    monkeypatch.setattr(api_mod, "_meeting_grounding", fake_fold)

    # Foreign row: not in the caller's rows, live lookup says no → nothing folded, turn still runs.
    _c, _t, prompt = _context_grounding(_meeting_body("42"), "s1", None, schedule_rows=lambda: [],
                                        workspace_mounts=lambda: [], meeting_readable=lambda mid: False)
    assert folded == [] and prompt == "summarize"
    # A failing lookup fails CLOSED.
    def boom(_mid):
        raise OSError("meeting-api down")
    _context_grounding(_meeting_body("42"), "s1", None, schedule_rows=lambda: [],
                       workspace_mounts=lambda: [], meeting_readable=boom)
    assert folded == []

    # Legit: the row is one of the caller's (rows) — or the live lookup grants it (fresh meeting).
    rows = [{"id": 42, "status": "active", "platform": "google_meet", "native_meeting_id": "abc-defg-hij"}]
    _context_grounding(_meeting_body("42"), "s1", None, schedule_rows=lambda: rows,
                       workspace_mounts=lambda: [])
    _context_grounding(_meeting_body("43"), "s1", None, schedule_rows=lambda: [],
                       workspace_mounts=lambda: [], meeting_readable=lambda mid: mid == "43")
    assert [str(x) for x in folded] == ["42", "43"]


# ── shared-workspace MANAGE writes are role-gated (viewer = read-only) ──────────────────────────────

def _git(cwd: Path, *a: str) -> None:
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True)


def _shared_ws(root: Path, wsid: str = "team-ws") -> Path:
    ws = root / wsid
    ws.mkdir(parents=True)
    _git(ws, "init", "-q", "-b", "main"); _git(ws, "config", "user.email", "t@t"); _git(ws, "config", "user.name", "t")
    (ws / "README.md").write_text("team\n"); _git(ws, "add", "-A"); _git(ws, "commit", "-q", "-m", "init")
    (ws / MEMBERS_FILE).parent.mkdir(parents=True, exist_ok=True)
    (ws / MEMBERS_FILE).write_text(json.dumps([
        {"subject": OWNER, "role": "owner"},
        {"subject": CONTRIB, "role": "contributor"},
        {"subject": VIEWER, "role": "viewer"},
    ]))
    return ws


def test_viewer_cannot_write_a_shared_workspace(tmp_path):
    ws = _shared_ws(tmp_path)
    c = _client(root=tmp_path)
    hv = {"X-User-Id": VIEWER}
    assert c.post("/api/workspace/purpose", json={"slug": "team-ws", "purpose": "pwned"}, headers=hv).status_code == 403
    assert c.post("/api/workspace/push", json={"slug": "team-ws", "token": "ghp_x"}, headers=hv).status_code == 403
    assert c.post("/api/workspace/pull", json={"slug": "team-ws"}, headers=hv).status_code == 403
    r = c.post("/api/workspace/publish", json={"slug": "team-ws", "token": "ghp_x", "repo_name": "x"}, headers=hv)
    assert r.status_code == 403, r.text
    assert not (ws / "PURPOSE").exists()
    # READ stays open to the viewer (they are a member).
    assert c.get("/api/workspace/purpose", params={"slug": "team-ws"}, headers=hv).status_code == 200


def test_contributor_can_write_and_stranger_gets_404(tmp_path):
    _shared_ws(tmp_path)
    c = _client(root=tmp_path)
    r = c.post("/api/workspace/purpose", json={"slug": "team-ws", "purpose": "ship it"},
               headers={"X-User-Id": CONTRIB})
    assert r.status_code == 200 and r.json()["purpose"] == "ship it"
    hs = {"X-User-Id": STRANGER}
    assert c.get("/api/workspace/purpose", params={"slug": "team-ws"}, headers=hs).status_code == 404
    assert c.post("/api/workspace/purpose", json={"slug": "team-ws", "purpose": "x"}, headers=hs).status_code == 404
    assert c.get("/api/workspace/git-remote-status", params={"slug": "team-ws"}, headers=hs).status_code == 404


def test_member_admin_routes_are_role_gated(tmp_path):
    """Invites/members listing needs contributor; member removal / role flips need owner (403 below)."""
    _shared_ws(tmp_path)
    c = _client(root=tmp_path)
    hv = {"X-User-Id": VIEWER}
    assert c.get("/api/workspace/members", params={"workspace_id": "team-ws"}, headers=hv).status_code == 403
    assert c.get("/api/workspace/invites", params={"workspace_id": "team-ws"}, headers=hv).status_code == 403
    hc = {"X-User-Id": CONTRIB}
    assert c.delete(f"/api/workspace/members/{VIEWER}", params={"workspace_id": "team-ws"}, headers=hc).status_code == 403
    assert c.post(f"/api/workspace/members/{CONTRIB}/role", params={"workspace_id": "team-ws"},
                  json={"role": "owner"}, headers=hc).status_code == 403
    assert c.post("/api/workspace/team-ws/unshare", headers=hc).status_code == 403


# ── session history never sweeps another user's chats ──────────────────────────────────────────────

def test_session_history_never_reads_another_users_parked_chat(tmp_path):
    other = tmp_path / ".attached" / "u_victim" / "old-slot"
    (other / ".claude" / "sessions").mkdir(parents=True)
    (other / ".claude" / "sessions" / "default.session").write_text("sid-v\n")
    proj = other / ".claude" / "projects" / "slug"
    proj.mkdir(parents=True)
    (proj / "sid-v.jsonl").write_text(json.dumps(
        {"type": "user", "message": {"role": "user", "content": "victim secret"}}) + "\n")
    reader = WorkspaceReader(str(tmp_path))
    assert reader.history("u_attacker", "default") == []
    assert reader.history("u_victim", "default") == [{"role": "user", "text": "victim secret"}]
