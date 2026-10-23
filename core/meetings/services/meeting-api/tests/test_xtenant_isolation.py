"""P0 (release blocker) — the cross-tenant transcript leak + wrong-row hydration, encoded as red-then-green
regressions at the collector's READ + INGEST seams.

Root cause: ``native_meeting_id`` was used as BOTH the redis-stream key AND the transcript read key, but a
native id is NOT unique — it collides across DIFFERENT users of the same meeting link (a cross-tenant LEAK)
and across ONE user's repeated meeting rows (wrong-row hydration). The per-user meeting ROW id is the only
correct key. These tests assert isolation across BOTH the stream/carrier path (ingest → tc:meeting:{key})
and the REST read path (GET /transcripts/by-id/{row} vs the legacy native path).

Deterministic + offline: fakeredis + explicit ``ingest`` + a TestClient over the in-memory store.
"""
from __future__ import annotations

import json

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

from meeting_api.collector import create_app, ingest
from meeting_api.collector.fakes import FakeRedisBus, InMemoryTranscriptStore

NATIVE = "abc-defg-hij"  # the SHARED meeting link both symptoms hinge on


@pytest.fixture
async def bus():
    client = fakeredis.aioredis.FakeRedis()
    b = FakeRedisBus(client)
    yield b
    await client.aclose()


def _seg(seg_id, text, *, start=1.0, speaker="Alice"):
    return {"segment_id": seg_id, "start": start, "end": start + 1.0, "text": text,
            "speaker": speaker, "language": "en", "completed": True}


def _message(meeting_id, segments, *, native=NATIVE):
    return {"payload": json.dumps({
        "type": "transcription", "meeting_id": str(meeting_id),
        "native_meeting_id": native, "platform": "google_meet", "segments": segments})}


async def _feed(client, key):
    """The transcript-carrier feed entries under tc:meeting:{key} (the exact key ingest wrote)."""
    rows = await client.xrange(f"tc:meeting:{key}")
    out = []
    for _id, fields in rows:
        raw = fields.get(b"payload") or fields.get("payload")
        out.append(json.loads(raw.decode() if isinstance(raw, bytes) else raw))
    return out


# ── SYMPTOM 1: CROSS-TENANT LEAK — two DIFFERENT users, SAME native, distinct rows ────────────────────

async def test_cross_tenant_carrier_isolation(bus):
    """User A (row 10) and user B (row 20) each ran a bot in the SAME meeting link. A produces
    transcript content; B's meeting must read EMPTY of A's data on the STREAM/carrier path. On the
    OLD native-keyed carrier both wrote/read tc:meeting:{NATIVE} → B saw A's transcript (the LEAK).
    Now each keys on its ROW id, so the carriers are disjoint."""
    store = InMemoryTranscriptStore()
    store.seed_meeting(user_id=100, platform="google_meet", native_meeting_id=NATIVE, meeting_id=10)
    store.seed_meeting(user_id=200, platform="google_meet", native_meeting_id=NATIVE, meeting_id=20)

    # Only user A's bot (row 10) produces segments.
    await ingest(store, bus, _message(10, [_seg("a1", "A secret revenue number")]))

    # A's ROW-keyed carrier has A's content; B's ROW-keyed carrier is EMPTY; the shared NATIVE key is
    # NEVER written (the leak channel is gone).
    assert [e["segments"][0]["text"] for e in await _feed(bus._client, 10)] == ["A secret revenue number"]
    assert await _feed(bus._client, 20) == []
    assert await _feed(bus._client, NATIVE) == []


async def test_cross_tenant_rest_isolation():
    """REST read path: user B, fetching THEIR OWN row (20) on the same native, reads NONE of user A's
    (row 10) transcript OR processed notes — via BOTH the by-id path and the native path. A row owned by
    another user is 404 on the by-id path (never another tenant's data)."""
    store = InMemoryTranscriptStore()
    store.seed_meeting(
        user_id=100, platform="google_meet", native_meeting_id=NATIVE, meeting_id=10,
        segments=[dict(_seg("a1", "A confidential line"))],
        data={"processed": {"views": [{"id": "copilot-notes", "doc": {"notes": [
            {"id": "a1", "text": "A private note"}]}}]}},
    )
    store.seed_meeting(user_id=200, platform="google_meet", native_meeting_id=NATIVE, meeting_id=20)
    client = TestClient(create_app(store, redis=None))

    # B reads THEIR row (20) by id → empty of A's data.
    r = client.get("/transcripts/by-id/20", headers={"x-user-id": "200"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["segments"] == []
    notes = (body.get("data") or {}).get("processed", {}).get("views", [])
    assert notes == [] or all(not v.get("doc", {}).get("notes") for v in notes)

    # B reads the native path → resolves to B's OWN newest row (20), still empty of A's data.
    r = client.get(f"/transcripts/google_meet/{NATIVE}", headers={"x-user-id": "200"})
    assert r.status_code == 200, r.text
    assert r.json()["segments"] == []

    # B CANNOT read A's row by id — owner-scoped 404 (not a leak).
    r = client.get("/transcripts/by-id/10", headers={"x-user-id": "200"})
    assert r.status_code == 404

    # A reads A's row → sees A's own content (sanity: not over-filtered).
    r = client.get("/transcripts/by-id/10", headers={"x-user-id": "100"})
    assert r.status_code == 200
    assert [s["text"] for s in r.json()["segments"]] == ["A confidential line"]


# ── SYMPTOM 2: WRONG-ROW HYDRATION — ONE user, TWO rows, SAME native ──────────────────────────────────

async def test_wrong_row_carrier_isolation(bus):
    """One user re-sent the bot on the same link: row 1 (older) and row 2 (newer). Each keys on its row
    id, so a segment for row 1 never lands on row 2's carrier (the collapse the old native key caused)."""
    store = InMemoryTranscriptStore()
    store.seed_meeting(user_id=100, platform="google_meet", native_meeting_id=NATIVE, meeting_id=1)
    store.seed_meeting(user_id=100, platform="google_meet", native_meeting_id=NATIVE, meeting_id=2)

    await ingest(store, bus, _message(1, [_seg("s1", "row-1 line")]))

    assert [e["segments"][0]["text"] for e in await _feed(bus._client, 1)] == ["row-1 line"]
    assert await _feed(bus._client, 2) == []


def test_wrong_row_rest_by_id_addresses_the_exact_row():
    """One user, two rows on the SAME native: row 1 has notes+segments, row 2 is empty. The by-id read
    returns EXACTLY the addressed row (row 1 → its notes; row 2 → empty), so the terminal never collapses
    to the newest. The native path (legacy) resolves to the NEWEST row (2) — documented behaviour."""
    store = InMemoryTranscriptStore()
    store.seed_meeting(
        user_id=100, platform="google_meet", native_meeting_id=NATIVE, meeting_id=1,
        created_at="2026-06-20T08:00:00Z",
        segments=[dict(_seg("s1", "row-1 transcript"))],
        data={"processed": {"views": [{"id": "copilot-notes", "doc": {"notes": [
            {"id": "s1", "text": "row-1 processed note"}]}}]}},
    )
    store.seed_meeting(
        user_id=100, platform="google_meet", native_meeting_id=NATIVE, meeting_id=2,
        created_at="2026-06-20T09:00:00Z",  # newer
    )
    client = TestClient(create_app(store, redis=None))

    # by-id row 1 → row 1's own notes + segments (never collapsed to the newest/empty row 2).
    b1 = client.get("/transcripts/by-id/1", headers={"x-user-id": "100"}).json()
    assert [s["text"] for s in b1["segments"]] == ["row-1 transcript"]
    v1 = b1["data"]["processed"]["views"][0]["doc"]["notes"]
    assert [n["text"] for n in v1] == ["row-1 processed note"]

    # by-id row 2 → empty (its own state), NOT row 1's notes.
    b2 = client.get("/transcripts/by-id/2", headers={"x-user-id": "100"}).json()
    assert b2["segments"] == []

    # native path → the NEWEST row (2), empty — the exact symptom-2 ambiguity the by-id path avoids.
    nat = client.get(f"/transcripts/google_meet/{NATIVE}", headers={"x-user-id": "100"}).json()
    assert nat["id"] == 2 and nat["segments"] == []


# ── LEGIT CASE PRESERVED — two users each own a row in the SAME real meeting ───────────────────────────

async def test_two_tenants_same_meeting_keep_separate_correct_transcripts(bus):
    """Two users each run their OWN bot in the same real meeting → SEPARATE, CORRECT transcripts (not
    merged, not blocked). Each row's carrier + REST read carries only that user's content."""
    store = InMemoryTranscriptStore()
    store.seed_meeting(user_id=100, platform="google_meet", native_meeting_id=NATIVE, meeting_id=10)
    store.seed_meeting(user_id=200, platform="google_meet", native_meeting_id=NATIVE, meeting_id=20)

    await ingest(store, bus, _message(10, [_seg("a", "hello from A", speaker="A")]))
    await ingest(store, bus, _message(20, [_seg("b", "hello from B", speaker="B")]))

    assert [e["segments"][0]["text"] for e in await _feed(bus._client, 10)] == ["hello from A"]
    assert [e["segments"][0]["text"] for e in await _feed(bus._client, 20)] == ["hello from B"]

    client = TestClient(create_app(store, redis=None))
    a = client.get("/transcripts/by-id/10", headers={"x-user-id": "100"}).json()
    b = client.get("/transcripts/by-id/20", headers={"x-user-id": "200"}).json()
    assert [s["text"] for s in a["segments"]] == ["hello from A"]
    assert [s["text"] for s in b["segments"]] == ["hello from B"]


# ── OBJECT-LEVEL AUTHZ (BOLA/IDOR) — cross-tenant + cross-role across the meeting-api surface ─────────
#
# Every route below is addressed by a caller-supplied id (row id, (platform, native), recording id,
# workspace id, share token). The rule: someone else's object → 404 (no existence oracle); a SHARED read
# (transcript-share viewer / bound-workspace member) never receives the owner's private meeting.data.

from meeting_api import create_app as create_meeting_api  # noqa: E402 — the unified monolith app
from meeting_api.bot_spawn.fakes import InMemoryMeetingRepo  # noqa: E402
from meeting_api.lifecycle.stop_router import InMemoryCommandPublisher  # noqa: E402
from meeting_api.recordings.fakes import InMemoryRecordingRepo, InMemoryStorage  # noqa: E402

OWNER, VIEWER, MEMBER, STRANGER = 100, 200, 300, 400
_PRIVATE = {
    "webhook_url": "https://owner.example/hook",
    "webhook_secret": "whsec_owner_private_secret",
    "webhook_events": {"meeting.completed": True},
}


def _h(uid, workspaces=None):
    h = {"x-user-id": str(uid)}
    if workspaces is not None:
        h["x-user-workspaces"] = ",".join(workspaces)
    return h


def _shared_store():
    """OWNER's meeting carrying a webhook secret, shared to VIEWER (redeemed transcript-share link) and
    bound to workspace `team-ws` (MEMBER belongs to it)."""
    store = InMemoryTranscriptStore()
    store.seed_meeting(
        user_id=OWNER, platform="google_meet", native_meeting_id=NATIVE, meeting_id=10,
        segments=[dict(_seg("o1", "owner line"))],
        data={**_PRIVATE, "title": "Board sync", "workspace_id": "team-ws",
              "transcript_viewers": [VIEWER],
              "share_grants": [{"id": "g1", "secret_hash": "deadbeef", "mode": "open"}]},
    )
    return store, TestClient(create_app(store, redis=None))


def _assert_viewer_safe(data):
    for k in ("webhook_url", "webhook_secret", "webhook_events", "share_grants", "transcript_viewers"):
        assert k not in data, f"owner-private key {k!r} leaked to a non-owner"


def test_shared_reads_never_leak_owner_private_meeting_data():
    """A share viewer / workspace member may READ the transcript, but the owner's webhook secret (which
    bot_spawn persists on meeting.data), the share-grant ledger and the viewer roster are stripped on
    EVERY shared read path: GET /meetings, GET /meetings/{id}, GET /transcripts/by-id/{id}."""
    _store, client = _shared_store()

    for headers in (_h(VIEWER), _h(MEMBER, ["team-ws"])):
        rows = client.get("/meetings", headers=headers).json()["meetings"]
        assert [r["id"] for r in rows] == [10] and rows[0]["shared"] is True
        _assert_viewer_safe(rows[0]["data"])
        assert rows[0]["data"]["title"] == "Board sync"  # non-private data still flows

        doc = client.get("/transcripts/by-id/10", headers=headers)
        assert doc.status_code == 200
        _assert_viewer_safe(doc.json()["data"])
        assert [s["text"] for s in doc.json()["segments"]] == ["owner line"]

    one = client.get("/meetings/10", headers=_h(VIEWER))
    assert one.status_code == 200
    _assert_viewer_safe(one.json()["data"])

    # The OWNER still sees their own full row (legit flow not over-filtered).
    own = client.get("/meetings/10", headers=_h(OWNER)).json()
    assert own["data"]["webhook_secret"] == _PRIVATE["webhook_secret"]


def test_stranger_gets_404_on_every_id_addressed_read():
    """No grant at all → 404 on the by-id transcript + single-meeting reads, absent from the list."""
    _store, client = _shared_store()
    assert client.get("/transcripts/by-id/10", headers=_h(STRANGER)).status_code == 404
    assert client.get("/meetings/10", headers=_h(STRANGER)).status_code == 404
    assert client.get("/meetings", headers=_h(STRANGER, ["other-ws"])).json()["meetings"] == []
    assert client.get(f"/transcripts/google_meet/{NATIVE}", headers=_h(STRANGER)).status_code == 404


def test_shared_reader_cannot_write_the_owners_meeting():
    """A share/membership grant is READ-only: every owner-scoped write resolves the caller's OWN row, so
    a viewer/member gets 404 (they own no such row) — mint share, bind workspace, docs, intent, PATCH,
    DELETE."""
    _store, client = _shared_store()
    for headers in (_h(VIEWER), _h(MEMBER, ["team-ws"])):
        base = f"/meetings/google_meet/{NATIVE}"
        assert client.post(f"{base}/share", json={}, headers=headers).status_code == 404
        assert client.post(f"{base}/workspace", json={"workspace_id": "team-ws"},
                           headers={**headers, "x-user-workspaces": "team-ws"}).status_code == 404
        assert client.post(f"{base}/docs", json={"path": "a.md", "workspace": "w"},
                           headers=headers).status_code == 404
        assert client.request("DELETE", f"{base}/docs", params={"path": "a.md"},
                              headers=headers).status_code == 404
        assert client.put(f"{base}/intent", json={"intent": "idle"}, headers=headers).status_code == 404
        assert client.patch("/meetings/10", json={"title": "pwned"}, headers=headers).status_code == 404
        assert client.delete("/meetings/10", headers=headers).status_code == 404


def test_cannot_bind_a_meeting_into_a_workspace_you_are_not_a_member_of():
    """The workspace bind IS the read grant for that workspace's members, so binding into a FOREIGN
    workspace (pushing your meeting into another tenant's shared list) → 404, on all three write paths
    (POST .../workspace, POST /meetings, PATCH /meetings/{id}). Member binds + unbinds keep working."""
    store = InMemoryTranscriptStore()
    store.seed_meeting(user_id=STRANGER, platform="google_meet", native_meeting_id=NATIVE, meeting_id=40)
    client = TestClient(create_app(store, redis=None))

    r = client.post(f"/meetings/google_meet/{NATIVE}/workspace", json={"workspace_id": "victim-ws"},
                    headers=_h(STRANGER, ["my-ws"]))
    assert r.status_code == 404
    r = client.post("/meetings", json={"title": "spam", "workspace_id": "victim-ws"},
                    headers=_h(STRANGER))
    assert r.status_code == 404
    planned = client.post("/meetings", json={"title": "mine"}, headers=_h(STRANGER)).json()["id"]
    r = client.patch(f"/meetings/{planned}", json={"workspace_id": "victim-ws"},
                     headers=_h(STRANGER, ["my-ws"]))
    assert r.status_code == 404
    # Nothing landed in the victim workspace's member listing.
    assert client.get("/meetings", headers=_h(MEMBER, ["victim-ws"])).json()["meetings"] == []

    # Legit: a member binds into THEIR workspace, and unbinding (null) needs no membership.
    ok = client.post(f"/meetings/google_meet/{NATIVE}/workspace", json={"workspace_id": "my-ws"},
                     headers=_h(STRANGER, ["my-ws"]))
    assert ok.status_code == 200
    assert client.patch(f"/meetings/{planned}", json={"workspace_id": "my-ws"},
                        headers=_h(STRANGER, ["my-ws"])).status_code == 200
    unbound = client.patch(f"/meetings/{planned}", json={"workspace_id": None}, headers=_h(STRANGER))
    assert unbound.status_code == 200 and "workspace_id" not in unbound.json()["data"]


def test_share_mint_rejects_unknown_mode():
    """An unknown share mode used to fall through the grant validator as an OPEN link → now 422."""
    store = InMemoryTranscriptStore()
    store.seed_meeting(user_id=OWNER, platform="google_meet", native_meeting_id=NATIVE, meeting_id=10)
    client = TestClient(create_app(store, redis=None))
    r = client.post(f"/meetings/google_meet/{NATIVE}/share", json={"mode": "restrcited"}, headers=_h(OWNER))
    assert r.status_code == 422
    assert client.post(f"/meetings/google_meet/{NATIVE}/share", json={"mode": "restricted",
                       "allowed_emails": ["a@x.io"]}, headers=_h(OWNER)).status_code == 200


def test_ws_subscribe_is_scoped_to_grants():
    """/ws/authorize-subscribe: owner, viewer and bound-workspace member are authorized for the SAME row;
    a stranger (or a member of an unrelated workspace) gets nothing."""
    _store, client = _shared_store()
    body = {"meetings": [{"platform": "google_meet", "native_meeting_id": NATIVE}]}

    def ids(headers):
        return [a["meeting_id"] for a in client.post("/ws/authorize-subscribe", json=body,
                                                     headers=headers).json()["authorized"]]

    assert ids(_h(OWNER)) == ["10"]
    assert ids(_h(VIEWER)) == ["10"]
    assert ids(_h(MEMBER, ["team-ws"])) == ["10"]
    assert ids(_h(STRANGER)) == []
    assert ids(_h(STRANGER, ["other-ws"])) == []


def _bot_stack():
    repo, pub = InMemoryMeetingRepo(), InMemoryCommandPublisher()
    import asyncio

    m = asyncio.run(repo.create_meeting(user_id=OWNER, platform="google_meet", native_meeting_id="v1",
                                        data={"voice_agent_enabled": True}))
    asyncio.run(repo.create_session(meeting_id=m["id"], session_uid="sess-v1"))
    return repo, pub, TestClient(create_meeting_api(meeting_repo=repo, command_publisher=pub))


def test_bot_control_routes_are_owner_scoped():
    """stop / speak / speak_stop / config on ANOTHER tenant's live bot → 404 and NOTHING is published on
    its command channel; the owner's own calls still go through."""
    _repo, pub, client = _bot_stack()
    for method, path, body in (
        ("DELETE", "/bots/google_meet/v1", None),
        ("POST", "/bots/google_meet/v1/speak", {"text": "hijack"}),
        ("DELETE", "/bots/google_meet/v1/speak", None),
        ("PUT", "/bots/google_meet/v1/config", {"language": "fr"}),
    ):
        r = client.request(method, path, json=body, headers=_h(STRANGER))
        assert r.status_code == 404, (method, path, r.status_code)
    assert pub.published == []
    assert client.post("/bots/google_meet/v1/speak", json={"text": "hi"}, headers=_h(OWNER)).status_code == 202
    assert len(pub.published) == 1


def test_recording_downloads_are_owner_scoped():
    """GET /recordings/{id}, /master and /media/{mf}/raw for ANOTHER tenant's recording id → 404 (no
    bytes); the list never includes it. A transcript-share viewer gets no recording access either."""
    import asyncio

    repo, storage = InMemoryRecordingRepo(), InMemoryStorage()
    repo.seed(meeting_id=10, user_id=OWNER, session_uid="s-10")
    asyncio.run(storage.upload("rec/10/master.wav", b"RIFFsecret-audio", content_type="audio/wav"))
    asyncio.run(repo.put_recordings(10, [{
        "id": 555, "session_uid": "s-10", "status": "completed",
        "media_files": [{"id": 9, "type": "audio", "format": "wav", "is_final": True,
                         "storage_path": "rec/10/master.wav"}],
    }]))
    client = TestClient(create_meeting_api(recording_repo=repo, storage=storage))

    for uid in (STRANGER, VIEWER):
        assert client.get("/recordings", headers=_h(uid)).json()["recordings"] == []
        for path in ("/recordings/555", "/recordings/555/master?type=audio",
                     "/recordings/555/media/9/raw?type=audio"):
            r = client.get(path, headers=_h(uid))
            assert r.status_code == 404, (uid, path, r.status_code)
            assert b"secret-audio" not in r.content

    raw = client.get("/recordings/555/media/9/raw?type=audio", headers=_h(OWNER))
    assert raw.status_code == 200 and raw.content == b"RIFFsecret-audio"


def test_identity_header_is_mandatory():
    """No gateway-injected identity → 401 on the id-addressed routes (fail closed, never 'user 0')."""
    client = TestClient(create_meeting_api())  # the unified app: collector + recordings + bots mounted
    for path in ("/transcripts/by-id/10", "/meetings/10", "/meetings", "/recordings", "/recordings/1",
                 "/bots", "/bots/status"):
        assert client.get(path).status_code == 401, path
    assert client.delete("/bots/google_meet/v1").status_code == 401
