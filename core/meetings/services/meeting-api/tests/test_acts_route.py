"""POST/DELETE /bots/{platform}/{native}/speak + PUT .../config — the acts.v1 control-plane
routes (lifecycle/acts_router, fpn).

Drives the SAME shipped ``create_app`` mount with the in-memory fakes (the test_stop_route
pattern): a seeded active voice-agent meeting is spoken to → the route publishes the acts.v1
``speak`` act on ``bot_commands:meeting:{id}``; ``DELETE`` publishes ``speak_stop``; ``PUT
.../config`` publishes ``reconfigure``. Every published payload is validated against the sealed
``acts.v1`` schema (additionalProperties:false — the act must be EXACTLY the contract shape).

Also covers the spawn side: ``voice_agent_enabled`` on POST /bots → invocation.v1
``voiceAgentEnabled`` + the TTS creds threaded into the workload env (the bot's tts-playback.ts
reads TTS_SERVICE_URL / TTS_API_TOKEN from its container env).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import jsonschema
from fastapi.testclient import TestClient

from meeting_api import create_app
from meeting_api.bot_spawn.fakes import FakeRuntimeClient, InMemoryMeetingRepo
from meeting_api.lifecycle.stop_router import InMemoryCommandPublisher

HEADERS = {"x-user-id": "7"}
SECRET = "test-admin-token"


# ── the sealed acts.v1 oracle (loaded BY PATH, the P8 seam — like the invocation builder) ────────

def _acts_schema() -> dict:
    rel = Path("meetings") / "contracts" / "acts.v1" / "acts.schema.json"
    for parent in Path(__file__).resolve().parents:
        candidate = parent / rel
        if candidate.is_file():
            return json.loads(candidate.read_text())
    raise FileNotFoundError("sealed acts.v1 contract not found by path")


def assert_conforms_act(act: dict) -> None:
    schema = _acts_schema()
    jsonschema.Draft202012Validator(
        {"$ref": f"{schema['$id']}#/$defs/Act", "$defs": schema["$defs"], "$id": schema["$id"]}
    ).validate(act)


def _seed_active(repo, *, user_id=7, platform="google_meet", native="m1", voice_agent=True):
    data = {"voice_agent_enabled": True} if voice_agent else {}
    m = asyncio.run(
        repo.create_meeting(user_id=user_id, platform=platform, native_meeting_id=native, data=data)
    )
    asyncio.run(repo.create_session(meeting_id=m["id"], session_uid=f"sess-{m['id']}"))
    return m


def _stack():
    repo, pub = InMemoryMeetingRepo(), InMemoryCommandPublisher()
    return repo, pub, TestClient(create_app(meeting_repo=repo, command_publisher=pub))


# ── POST .../speak ────────────────────────────────────────────────────────────────────────────────

def test_speak_publishes_speak_act():
    repo, pub, client = _stack()
    m = _seed_active(repo)

    r = client.post("/bots/google_meet/m1/speak", headers=HEADERS,
                    json={"text": "Hello everyone", "voice": "nova"})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "speaking_requested"
    assert body["meeting_id"] == m["id"]

    assert pub.published, "no speak act published"
    chan, msg = pub.published[0]
    assert chan == f"bot_commands:meeting:{m['id']}"
    act = json.loads(msg)
    assert act == {"action": "speak", "text": "Hello everyone", "voice": "nova"}
    assert_conforms_act(act)


def test_speak_voice_optional():
    repo, pub, client = _stack()
    _seed_active(repo)
    r = client.post("/bots/google_meet/m1/speak", headers=HEADERS, json={"text": "hi"})
    assert r.status_code == 202, r.text
    act = json.loads(pub.published[0][1])
    assert act == {"action": "speak", "text": "hi"}  # no null/absent voice key
    assert_conforms_act(act)


def test_speak_404_when_no_active_meeting():
    repo, pub, client = _stack()
    r = client.post("/bots/google_meet/nope/speak", headers=HEADERS, json={"text": "hi"})
    assert r.status_code == 404
    assert not pub.published


def test_speak_409_when_voice_agent_disabled():
    repo, pub, client = _stack()
    _seed_active(repo, voice_agent=False)
    r = client.post("/bots/google_meet/m1/speak", headers=HEADERS, json={"text": "hi"})
    assert r.status_code == 409, r.text
    assert "voice_agent_enabled" in r.json()["detail"]
    assert not pub.published


def test_speak_422_on_missing_or_empty_text():
    repo, pub, client = _stack()
    _seed_active(repo)
    assert client.post("/bots/google_meet/m1/speak", headers=HEADERS, json={}).status_code == 422
    assert client.post(
        "/bots/google_meet/m1/speak", headers=HEADERS, json={"text": "   "}
    ).status_code == 422
    assert not pub.published


def test_speak_422_on_unsupported_platform():
    repo, pub, client = _stack()
    r = client.post("/bots/skype/m1/speak", headers=HEADERS, json={"text": "hi"})
    assert r.status_code == 422
    assert not pub.published


def test_speak_401_without_identity():
    repo, pub, client = _stack()
    _seed_active(repo)
    assert client.post("/bots/google_meet/m1/speak", json={"text": "hi"}).status_code == 401


# ── DELETE .../speak (interrupt) ─────────────────────────────────────────────────────────────────

def test_delete_speak_publishes_speak_stop():
    repo, pub, client = _stack()
    m = _seed_active(repo)
    r = client.delete("/bots/google_meet/m1/speak", headers=HEADERS)
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "speak_stop_requested"
    chan, msg = pub.published[0]
    assert chan == f"bot_commands:meeting:{m['id']}"
    act = json.loads(msg)
    assert act == {"action": "speak_stop"}
    assert_conforms_act(act)


def test_delete_speak_409_when_voice_agent_disabled():
    repo, pub, client = _stack()
    _seed_active(repo, voice_agent=False)
    assert client.delete("/bots/google_meet/m1/speak", headers=HEADERS).status_code == 409
    assert not pub.published


# ── PUT .../config (reconfigure) ─────────────────────────────────────────────────────────────────

def test_put_config_publishes_reconfigure():
    repo, pub, client = _stack()
    m = _seed_active(repo, voice_agent=False)  # config is NOT voice-gated
    r = client.put("/bots/google_meet/m1/config", headers=HEADERS,
                   json={"language": "es", "task": "translate"})
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "reconfigure_requested"
    chan, msg = pub.published[0]
    assert chan == f"bot_commands:meeting:{m['id']}"
    act = json.loads(msg)
    assert act == {"action": "reconfigure", "language": "es", "task": "translate"}
    assert_conforms_act(act)


def test_put_config_404_when_no_active_meeting():
    repo, pub, client = _stack()
    r = client.put("/bots/google_meet/nope/config", headers=HEADERS, json={"language": "es"})
    assert r.status_code == 404
    assert not pub.published


# ── spawn side: voice_agent_enabled → invocation.v1 voiceAgentEnabled + TTS workload env ────────

def test_spawn_voice_agent_threads_invocation_and_tts_env(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", SECRET)
    monkeypatch.setenv("TTS_SERVICE_URL", "http://tts-cartesia:8080")
    monkeypatch.setenv("TTS_SERVICE_TOKEN", "tts-secret")
    repo, runtime, pub = InMemoryMeetingRepo(), FakeRuntimeClient(), InMemoryCommandPublisher()
    client = TestClient(create_app(meeting_repo=repo, runtime=runtime, command_publisher=pub))

    r = client.post("/bots", headers=HEADERS,
                    json={"platform": "google_meet", "native_meeting_id": "abc-defg-hij",
                          "voice_agent_enabled": True})
    assert r.status_code == 201, r.text
    assert r.json()["data"]["voice_agent_enabled"] is True  # persisted → the speak 409 gate reads it

    spec = runtime.specs[0]
    invocation = json.loads(spec["env"]["VEXA_BOT_CONFIG"])
    assert invocation["voiceAgentEnabled"] is True
    assert spec["env"]["TTS_SERVICE_URL"] == "http://tts-cartesia:8080"
    assert spec["env"]["TTS_API_TOKEN"] == "tts-secret"

    # …and the seeded meeting is immediately speakable end-to-end through the SAME app.
    r2 = client.post("/bots/google_meet/abc-defg-hij/speak", headers=HEADERS, json={"text": "hi"})
    assert r2.status_code == 202, r2.text
    assert json.loads(pub.published[0][1])["action"] == "speak"


def test_spawn_default_has_no_voice_agent(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", SECRET)
    monkeypatch.setenv("TTS_SERVICE_URL", "http://tts-cartesia:8080")
    monkeypatch.setenv("TTS_SERVICE_TOKEN", "tts-secret")
    repo, runtime = InMemoryMeetingRepo(), FakeRuntimeClient()
    client = TestClient(create_app(meeting_repo=repo, runtime=runtime))

    r = client.post("/bots", headers=HEADERS,
                    json={"platform": "google_meet", "native_meeting_id": "abc-defg-hij"})
    assert r.status_code == 201, r.text

    spec = runtime.specs[0]
    invocation = json.loads(spec["env"]["VEXA_BOT_CONFIG"])
    assert "voiceAgentEnabled" not in invocation  # stripped when falsy (absent == off)
    assert "TTS_SERVICE_URL" not in spec["env"]   # no TTS creds in a non-voice bot's env
    assert "TTS_API_TOKEN" not in spec["env"]

    # …and speaking to it is a 409 (the bot would ignore the act).
    r2 = client.post("/bots/google_meet/abc-defg-hij/speak", headers=HEADERS, json={"text": "hi"})
    assert r2.status_code == 409
