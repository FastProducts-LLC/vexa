"""recordings — chunk upload + finalize → master in meeting.data JSONB (recording.v1).

Drives the SHIPPED ``upload_chunk`` / ``finalize_master`` / ``build_router`` over the in-memory
fakes, OFFLINE (no MinIO, no DB): chunks fold into the recording's JSONB payload, the master is
built by the golden-locked codec and the media-file stamped finalized, and the upload-token auth +
session-resolution seams behave.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from meeting_api.bot_spawn import mint_meeting_token
from meeting_api.recordings import build_router, finalize_master, upload_chunk
from meeting_api.recordings.fakes import InMemoryRecordingRepo, InMemoryStorage
from meeting_api.recordings.jsonb import chunk_storage_key

SECRET = "test-admin-token"
USER = 7
MEETING_ID = 1
SESSION_UID = "conn-abc"

# A minimal valid wav file (44-byte RIFF header + 4 bytes of PCM) so the wav master codec runs.
def _wav(n_data: int = 4) -> bytes:
    import struct

    data = b"\x00" * n_data
    fmt = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 1, 16000, 32000, 2, 16)
    chunk = struct.pack("<4sI", b"data", len(data)) + data
    riff_len = 4 + len(fmt) + len(chunk)
    return struct.pack("<4sI4s", b"RIFF", riff_len, b"WAVE") + fmt + chunk


def _seeded():
    repo = InMemoryRecordingRepo()
    repo.seed(meeting_id=MEETING_ID, user_id=USER, session_uid=SESSION_UID)
    return repo, InMemoryStorage()


# ── flow: upload folds chunks into JSONB; finalize builds the master ─────────────────────────────

async def test_upload_chunk_writes_recording_jsonb():
    repo, storage = _seeded()
    receipt = await upload_chunk(
        repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
        data=_wav(), media_type="audio", media_format="wav", chunk_seq=0, is_final=False,
    )
    assert receipt["status"] == "in_progress"
    recs = await repo.get_recordings(MEETING_ID)
    assert len(recs) == 1
    mf = recs[0]["media_files"][0]
    assert mf["type"] == "audio"
    assert mf["chunk_count"] == 1
    # The chunk landed in storage under the parent key scheme.
    assert mf["storage_path"] in storage.blobs


async def test_final_chunk_completes_recording():
    repo, storage = _seeded()
    await upload_chunk(repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
                       data=_wav(), media_format="wav", chunk_seq=0, is_final=False)
    receipt = await upload_chunk(repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
                                 data=_wav(), media_format="wav", chunk_seq=1, is_final=True)
    assert receipt["status"] == "completed"


async def test_finalize_master_builds_and_stamps():
    repo, storage = _seeded()
    rid = None
    for seq in range(3):
        receipt = await upload_chunk(
            repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
            data=_wav(), media_format="wav", chunk_seq=seq, is_final=False,
        )
        rid = receipt["recording_id"]
    master_key = await finalize_master(repo, storage, meeting_id=MEETING_ID, recording_id=rid)
    assert master_key.endswith("/audio/master.wav")
    assert master_key in storage.blobs  # the codec-built master was uploaded
    recs = await repo.get_recordings(MEETING_ID)
    mf = recs[0]["media_files"][0]
    assert mf["is_final"] is True
    assert mf["finalized_by"] == "recording_finalizer.master"
    assert mf["storage_path"] == master_key


async def test_upload_before_session_is_pending():
    repo, storage = _seeded()
    receipt = await upload_chunk(
        repo, storage, token_meeting_id=MEETING_ID, session_uid="unknown-session",
        data=_wav(), media_format="wav", chunk_seq=0, is_final=False,
    )
    assert receipt == {"status": "pending"}


# ── route: the upload endpoint authenticates the MeetingToken ────────────────────────────────────

def _client():
    from fastapi import FastAPI

    repo, storage = _seeded()
    app = FastAPI()
    app.include_router(build_router(repo, storage, token_secret=SECRET))
    return TestClient(app)


def test_upload_route_requires_token():
    client = _client()
    r = client.post(
        "/internal/recordings/upload",
        data={"session_uid": SESSION_UID, "media_format": "wav", "chunk_seq": 0, "is_final": "true"},
        files={"file": ("c.wav", _wav(), "audio/wav")},
    )
    assert r.status_code == 401  # missing Authorization


def test_upload_route_accepts_valid_token():
    client = _client()
    token = mint_meeting_token(MEETING_ID, USER, "google_meet", "abc", secret=SECRET)
    r = client.post(
        "/internal/recordings/upload",
        headers={"Authorization": f"Bearer {token}"},
        data={"session_uid": SESSION_UID, "media_format": "wav", "chunk_seq": 0, "is_final": "true"},
        files={"file": ("c.wav", _wav(), "audio/wav")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"


# ── MeetingToken verifier: exp REQUIRED, iss/aud/scope/alg pinned to what the minter stamps ──────


def _forge(claims: dict, *, secret: str = SECRET, header: dict | None = None) -> str:
    """Sign arbitrary claims with the REAL secret — the verifier must still reject anything the
    minter would not have produced (no exp, foreign iss/aud/scope, non-HS256 header)."""
    import base64
    import hmac
    import json

    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d, separators=(",", ":")).encode()).rstrip(b"=").decode()

    h = b64(header or {"alg": "HS256", "typ": "JWT"})
    p = b64(claims)
    sig = hmac.new(secret.encode(), f"{h}.{p}".encode(), digestmod="sha256").digest()
    return f"{h}.{p}.{base64.urlsafe_b64encode(sig).rstrip(b'=').decode()}"


def _good_claims(**over):
    import time

    c = {"meeting_id": MEETING_ID, "user_id": USER, "platform": "google_meet", "native_meeting_id": "abc",
         "scope": "transcribe:write", "iss": "meeting-api", "aud": "transcription-collector",
         "iat": int(time.time()), "exp": int(time.time()) + 600, "jti": "j"}
    c.update(over)
    return c


def test_verifier_accepts_minted_token():
    from meeting_api.recordings.service import _verify_meeting_token

    tok = mint_meeting_token(MEETING_ID, USER, "google_meet", "abc", secret=SECRET)
    claims = _verify_meeting_token(tok, secret=SECRET)
    assert claims["meeting_id"] == MEETING_ID
    # a hand-built token with exactly the minter's claims verifies too (the forge helper is honest)
    assert _verify_meeting_token(_forge(_good_claims()), secret=SECRET)["iss"] == "meeting-api"


@pytest.mark.parametrize(
    "claims, why",
    [
        ({k: v for k, v in _good_claims().items() if k != "exp"}, "missing exp"),
        (_good_claims(exp=1), "expired"),
        (_good_claims(exp="soon"), "exp invalid"),
        (_good_claims(iss="someone-else"), "issuer"),
        ({k: v for k, v in _good_claims().items() if k != "iss"}, "issuer"),
        (_good_claims(aud="admin-api"), "audience"),
        ({k: v for k, v in _good_claims().items() if k != "aud"}, "audience"),
        (_good_claims(scope="admin"), "scope"),
        ({k: v for k, v in _good_claims().items() if k != "scope"}, "scope"),
        ({k: v for k, v in _good_claims().items() if k != "meeting_id"}, "meeting_id"),
    ],
)
def test_verifier_rejects_off_contract_claims(claims, why):
    from meeting_api.recordings.service import _verify_meeting_token

    with pytest.raises(ValueError) as ei:
        _verify_meeting_token(_forge(claims), secret=SECRET)
    assert why in str(ei.value)


def test_verifier_accepts_aud_list_containing_audience():
    from meeting_api.recordings.service import _verify_meeting_token

    tok = _forge(_good_claims(aud=["other", "transcription-collector"]))
    assert _verify_meeting_token(tok, secret=SECRET)["meeting_id"] == MEETING_ID


def test_verifier_rejects_non_hs256_header_and_wrong_secret():
    from meeting_api.recordings.service import _verify_meeting_token

    with pytest.raises(ValueError, match="HS256"):
        _verify_meeting_token(_forge(_good_claims(), header={"alg": "none"}), secret=SECRET)
    with pytest.raises(ValueError, match="signature"):
        _verify_meeting_token(_forge(_good_claims(), secret="other-secret"), secret=SECRET)
    with pytest.raises(ValueError, match="malformed"):
        _verify_meeting_token("a.b", secret=SECRET)
    with pytest.raises(ValueError):
        _verify_meeting_token("!!!.@@@.###", secret=SECRET)


def test_upload_route_rejects_token_without_exp():
    client = _client()
    tok = _forge({k: v for k, v in _good_claims().items() if k != "exp"})
    r = client.post(
        "/internal/recordings/upload",
        headers={"Authorization": f"Bearer {tok}"},
        data={"session_uid": SESSION_UID, "media_format": "wav", "chunk_seq": 0, "is_final": "true"},
        files={"file": ("c.wav", _wav(), "audio/wav")},
    )
    assert r.status_code == 401
    assert "exp" in r.json()["detail"]


def test_upload_route_shared_internal_secret_bearer_is_refused(monkeypatch):
    """C4 follow-up: bots no longer hold INTERNAL_API_SECRET, and with it any holder could write into
    ANY session — the upload accepts only the bot's MeetingToken now."""
    monkeypatch.setenv("INTERNAL_API_SECRET", "isec-123")
    client = _client()
    r = client.post(
        "/internal/recordings/upload",
        headers={"Authorization": "Bearer isec-123"},
        data={"session_uid": SESSION_UID, "media_format": "wav", "chunk_seq": 0, "is_final": "true"},
        files={"file": ("c.wav", _wav(), "audio/wav")},
    )
    assert r.status_code == 401, r.text


def test_upload_route_token_bound_to_another_session_is_403():
    from meeting_api.bot_spawn.invocation import mint_meeting_token

    client = _client()
    other = mint_meeting_token(MEETING_ID, 1, "google_meet", "x", secret=SECRET, connection_id="another-session")
    r = client.post(
        "/internal/recordings/upload",
        headers={"Authorization": f"Bearer {other}"},
        data={"session_uid": SESSION_UID, "media_format": "wav", "chunk_seq": 0, "is_final": "true"},
        files={"file": ("c.wav", _wav(), "audio/wav")},
    )
    assert r.status_code == 403, r.text
    bound = mint_meeting_token(MEETING_ID, 1, "google_meet", "x", secret=SECRET, connection_id=SESSION_UID)
    ok = client.post(
        "/internal/recordings/upload",
        headers={"Authorization": f"Bearer {bound}"},
        data={"session_uid": SESSION_UID, "media_format": "wav", "chunk_seq": 0, "is_final": "true"},
        files={"file": ("c.wav", _wav(), "audio/wav")},
    )
    assert ok.status_code == 200, ok.text


# ── G4: object-storage I/O must not block the event loop ─────────────────────────────────────────


class _BlockingS3Client:
    """A stub boto3 client whose put_object BLOCKS (sync) — stands in for a slow S3 round-trip."""

    def __init__(self, block_s: float):
        self._block_s = block_s
        self.calls = 0

    def put_object(self, **kw):
        import time

        time.sleep(self._block_s)  # a real, blocking, synchronous call (what boto3 does)
        self.calls += 1
        return {}


async def test_s3_storage_does_not_block_the_event_loop():
    """G4: a blocking boto3 call must run OFF the loop (asyncio.to_thread), so the control plane keeps
    serving lifecycle/webhook/ws traffic during a slow/large S3 op. We run a ~0.3s blocking upload
    concurrently with a 5ms heartbeat — a non-blocking loop ticks many times; a blocked loop ~never."""
    import asyncio

    from meeting_api.recordings.adapters import S3Storage

    class _StubS3(S3Storage):
        def __init__(self, client):
            super().__init__(bucket="b")
            self._stub = client

        def _c(self):
            return self._stub

        # NB: _run is INHERITED (asyncio.to_thread) — that's exactly what's under test.

    storage = _StubS3(_BlockingS3Client(block_s=0.3))
    ticks = {"n": 0}
    stop = {"v": False}

    async def heartbeat():
        while not stop["v"]:
            ticks["n"] += 1
            await asyncio.sleep(0.005)

    hb = asyncio.create_task(heartbeat())
    try:
        await storage.upload("k", b"x" * 1024, content_type="audio/wav")
    finally:
        stop["v"] = True
        await hb

    assert storage._stub.calls == 1
    assert ticks["n"] >= 20, (
        f"event loop appears BLOCKED during the S3 upload (only {ticks['n']} heartbeats in ~0.3s) — "
        "the boto3 call is not being offloaded to a thread"
    )


# ── G3: concurrent chunk folds must not lose updates (atomic read→modify→write) ──────────────────


class _YieldingStorage(InMemoryStorage):
    """An InMemoryStorage whose upload YIELDS the event loop, so two concurrent uploads genuinely
    interleave (forcing the read→modify→write race the atomic mutate must serialize)."""

    async def upload(self, key, data, *, content_type):
        import asyncio

        await asyncio.sleep(0)
        await super().upload(key, data, content_type=content_type)


async def test_concurrent_chunk_uploads_do_not_lose_updates():
    """G3: two chunk uploads racing on the SAME recording must BOTH be folded. The old
    get_recordings → apply → put_recordings ran in SEPARATE transactions, so the second put clobbered
    the first (lost update → chunk_count stuck at 2). The atomic mutate_recordings re-reads the LIVE
    list under one lock and folds cumulatively → chunk_count 3."""
    import asyncio

    repo = InMemoryRecordingRepo()
    repo.seed(meeting_id=MEETING_ID, user_id=USER, session_uid=SESSION_UID)
    storage = _YieldingStorage()

    # chunk 0 (sequential) establishes the recording.
    await upload_chunk(repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
                       data=_wav(), media_format="wav", chunk_seq=0, is_final=False)
    # chunks 1 + 2 race.
    await asyncio.gather(
        upload_chunk(repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
                     data=_wav(), media_format="wav", chunk_seq=1, is_final=False),
        upload_chunk(repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
                     data=_wav(), media_format="wav", chunk_seq=2, is_final=False),
    )

    recs = await repo.get_recordings(MEETING_ID)
    bot_recs = [r for r in recs if r.get("source") == "bot"]
    assert len(bot_recs) == 1, f"exactly one recording for the session, got {len(bot_recs)}"
    mf = next(m for m in bot_recs[0]["media_files"] if m["type"] == "audio")
    assert mf["chunk_count"] == 3, f"all 3 chunks must be folded (no lost update), got {mf['chunk_count']}"
