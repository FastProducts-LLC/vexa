"""C4 — the internal callback trust edge (callback_auth.py) after the follow-up (deep audit #2/#5).

  * the runtime callback needs ``x-internal-secret == RUNTIME_CALLBACK_SECRET`` (its own value);
  * the bot callback needs the bot's MeetingToken (HS256, MEETING_TOKEN_SECRET) AND the token must be
    bound to the event's session (``connection_id`` claim == body ``connection_id``) — token A cannot
    drive session B;
  * NO shared deployment secret is accepted on the bot callback — not INTERNAL_API_SECRET, not the
    gateway identity secret, not admin-api's internal key, not the runtime callback secret;
  * ``BOT_LEGACY_INTERNAL_SECRET=true`` (stock-bot compat) additionally reads the MeetingToken from
    ``x-internal-secret`` — the slot the stock bot fills from ``invocation.internalSecret`` — with the
    same signature + binding checks;
  * outside DEV_MODE, an unconfigured credential FAILS CLOSED (503);
  * the standalone lifecycle receiver applies the SAME guard;
  * ``DEV_MODE=true`` keeps the open edge (what the rest of this suite runs under — conftest).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from meeting_api import create_app
from meeting_api.bot_spawn.invocation import mint_meeting_token
from meeting_api.callback_auth import check_bot_callback, check_runtime_callback
from meeting_api.lifecycle import MeetingStore
from meeting_api.lifecycle.receiver import create_app as create_receiver_app

LIFECYCLE = "/bots/internal/callback/lifecycle"
RUNTIME = "/runtime/callback"
RUNTIME_SECRET = "c4-runtime-callback-secret"
GATEWAY_SECRET = "c4-gateway-identity-secret"
ADMIN_INTERNAL = "c4-admin-internal-secret"
LEGACY_INTERNAL = "c4-legacy-internal-secret"
MTS = "c4-meeting-token-secret"
SESSION = "sess-uid"  # the lifecycle.v1 goldens' connection_id


def _tok(session=SESSION, *, secret=MTS, meeting_id=1, **kw) -> str:
    return mint_meeting_token(meeting_id, 1, "google_meet", "abc-defg-hij", secret=secret,
                              connection_id=session, **kw)


@pytest.fixture
def closed_edge(monkeypatch):
    """A production-shaped deployment: DEV_MODE off, every split secret configured separately."""
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("RUNTIME_CALLBACK_SECRET", RUNTIME_SECRET)
    monkeypatch.setenv("GATEWAY_IDENTITY_SECRET", GATEWAY_SECRET)
    monkeypatch.setenv("ADMIN_API_INTERNAL_SECRET", ADMIN_INTERNAL)
    monkeypatch.setenv("INTERNAL_API_SECRET", LEGACY_INTERNAL)
    monkeypatch.setenv("MEETING_TOKEN_SECRET", MTS)
    monkeypatch.delenv("BOT_LEGACY_INTERNAL_SECRET", raising=False)


@pytest.fixture
def legacy_bot(closed_edge, monkeypatch):
    monkeypatch.setenv("BOT_LEGACY_INTERNAL_SECRET", "true")


def _unified() -> TestClient:
    return TestClient(create_app(token_secret=MTS))


def _event(goldens, name="joining") -> dict:
    return dict(goldens[name])


ENV = {"DEV_MODE": "false", "MEETING_TOKEN_SECRET": MTS, "RUNTIME_CALLBACK_SECRET": RUNTIME_SECRET}
BODY = {"connection_id": SESSION, "status": "joining"}


# ── pure verdicts: runtime ──────────────────────────────────────────────────────────────────────

def test_runtime_verdict_fails_closed_without_secret():
    assert check_runtime_callback({}, env={"DEV_MODE": "false"})[0] == 503


def test_runtime_verdict_constant_time_match():
    assert check_runtime_callback({"x-internal-secret": RUNTIME_SECRET}, env=ENV) is None
    assert check_runtime_callback({"x-internal-secret": RUNTIME_SECRET + "x"}, env=ENV)[0] == 403
    assert check_runtime_callback({"x-internal-secret": ""}, env=ENV)[0] == 403
    assert check_runtime_callback({}, env=ENV)[0] == 403


def test_runtime_verdict_rejects_every_other_secret():
    env = dict(ENV, GATEWAY_IDENTITY_SECRET=GATEWAY_SECRET, ADMIN_API_INTERNAL_SECRET=ADMIN_INTERNAL,
               INTERNAL_API_SECRET=LEGACY_INTERNAL)
    for other in (GATEWAY_SECRET, ADMIN_INTERNAL, LEGACY_INTERNAL, MTS, _tok()):
        assert check_runtime_callback({"x-internal-secret": other}, env=env)[0] == 403, other


def test_runtime_secret_falls_back_to_internal_api_secret_when_unset():
    """An un-migrated deploy (only INTERNAL_API_SECRET) keeps working — the deprecation fallback."""
    env = {"DEV_MODE": "false", "INTERNAL_API_SECRET": LEGACY_INTERNAL}
    assert check_runtime_callback({"x-internal-secret": LEGACY_INTERNAL}, env=env) is None


# ── pure verdicts: bot ──────────────────────────────────────────────────────────────────────────

def test_bot_verdict_fails_closed_without_token_key():
    assert check_bot_callback({}, BODY, env={"DEV_MODE": "false"})[0] == 503


def test_bot_verdict_accepts_bound_meeting_token():
    tok = _tok()
    assert check_bot_callback({"authorization": f"Bearer {tok}"}, BODY, env=ENV) is None
    assert check_bot_callback({"authorization": f"bearer {tok}"}, BODY, env=ENV) is None  # scheme case-insensitive


def test_bot_token_for_session_a_is_rejected_for_session_b():
    tok_a = _tok("session-A")
    body_b = {"connection_id": "session-B", "status": "completed", "completion_reason": "stopped"}
    v = check_bot_callback({"authorization": f"Bearer {tok_a}"}, body_b, env=ENV)
    assert v == (403, "MeetingToken is bound to a different session")
    assert check_bot_callback({"authorization": f"Bearer {tok_a}"},
                              {"connection_id": "session-A", "status": "joining"}, env=ENV) is None


def test_bot_token_without_binding_or_body_session_is_rejected():
    unbound = mint_meeting_token(1, 1, "google_meet", "abc-defg-hij", secret=MTS)  # no connection_id claim
    assert check_bot_callback({"authorization": f"Bearer {unbound}"}, BODY, env=ENV)[0] == 403
    tok = _tok()
    assert check_bot_callback({"authorization": f"Bearer {tok}"}, {"status": "joining"}, env=ENV)[0] == 403
    assert check_bot_callback({"authorization": f"Bearer {tok}"}, None, env=ENV)[0] == 403
    assert check_bot_callback({"authorization": f"Bearer {tok}"}, ["sess-uid"], env=ENV)[0] == 403


def test_bot_verdict_rejects_bad_tokens():
    tok = _tok()
    other_key = _tok(secret="someone-elses-key")
    h, p, s = tok.split(".")
    expired = _tok(ttl_seconds=-5)
    for bad in (other_key, f"{h}.{p}x.{s}", expired, "not.a.jwt"):
        assert check_bot_callback({"authorization": f"Bearer {bad}"}, BODY, env=ENV)[0] == 403
    assert check_bot_callback({"authorization": "Basic abc"}, BODY, env=ENV)[0] == 403
    assert check_bot_callback({}, BODY, env=ENV)[0] == 403


def test_bot_verdict_rejects_every_shared_secret():
    """The impersonation path: no deployment secret authenticates a bot callback, in either header."""
    env = dict(ENV, GATEWAY_IDENTITY_SECRET=GATEWAY_SECRET, ADMIN_API_INTERNAL_SECRET=ADMIN_INTERNAL,
               INTERNAL_API_SECRET=LEGACY_INTERNAL)
    for legacy in ("false", "true"):
        env["BOT_LEGACY_INTERNAL_SECRET"] = legacy
        for secret in (LEGACY_INTERNAL, GATEWAY_SECRET, ADMIN_INTERNAL, RUNTIME_SECRET, MTS):
            assert check_bot_callback({"x-internal-secret": secret}, BODY, env=env)[0] == 403, (legacy, secret)
            assert check_bot_callback({"authorization": f"Bearer {secret}"}, BODY, env=env)[0] == 403, (legacy, secret)


def test_meeting_token_signed_with_admin_token_is_rejected_once_split():
    """With MEETING_TOKEN_SECRET set, an ADMIN_TOKEN-signed token (the old key) no longer verifies."""
    env = dict(ENV, ADMIN_TOKEN="the-admin-token")
    old = _tok(secret="the-admin-token")
    assert check_bot_callback({"authorization": f"Bearer {old}"}, BODY, env=env)[0] == 403


def test_meeting_token_key_falls_back_to_admin_token_when_unset():
    env = {"DEV_MODE": "false", "ADMIN_TOKEN": "the-admin-token"}
    tok = _tok(secret="the-admin-token")
    assert check_bot_callback({"authorization": f"Bearer {tok}"}, BODY, env=env) is None


def test_legacy_header_only_in_compat_mode():
    tok = _tok()
    assert check_bot_callback({"x-internal-secret": tok}, BODY, env=ENV)[0] == 403
    compat = dict(ENV, BOT_LEGACY_INTERNAL_SECRET="true")
    assert check_bot_callback({"x-internal-secret": tok}, BODY, env=compat) is None
    # …and still bound: the stock bot's token for A cannot drive B
    other = {"connection_id": "someone-else", "status": "completed", "completion_reason": "stopped"}
    assert check_bot_callback({"x-internal-secret": tok}, other, env=compat)[0] == 403


def test_bot_verdict_uses_explicit_token_secret_over_env():
    tok = _tok(secret="injected")
    assert check_bot_callback({"authorization": f"Bearer {tok}"}, BODY, token_secret="injected", env=ENV) is None
    assert check_bot_callback({"authorization": f"Bearer {tok}"}, BODY, env=ENV)[0] == 403


def test_dev_mode_opens_both_edges():
    env = {"DEV_MODE": "true"}
    assert check_runtime_callback({}, env=env) is None
    assert check_bot_callback({}, BODY, env=env) is None


# ── over the unified app ────────────────────────────────────────────────────────────────────────

def test_unified_app_refuses_unauthenticated_and_shared_secret_callbacks(closed_edge, goldens):
    client = _unified()
    assert client.post(LIFECYCLE, json=_event(goldens)).status_code == 403
    for secret in (LEGACY_INTERNAL, GATEWAY_SECRET, ADMIN_INTERNAL, RUNTIME_SECRET):
        r = client.post(LIFECYCLE, json=_event(goldens), headers={"x-internal-secret": secret})
        assert r.status_code == 403, (secret, r.text)
    r = client.post(RUNTIME, json={"workloadId": "wl-1", "state": "destroyed"})
    assert r.status_code == 403, r.text
    for secret in (LEGACY_INTERNAL, GATEWAY_SECRET, ADMIN_INTERNAL, _tok()):
        r = client.post(RUNTIME, json={"workloadId": "wl-1", "state": "destroyed"},
                        headers={"x-internal-secret": secret})
        assert r.status_code == 403, (secret, r.text)


def test_unified_app_accepts_bound_meeting_token(closed_edge, goldens):
    client = _unified()
    tok = _tok()
    r = client.post(LIFECYCLE, json=_event(goldens), headers={"authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "accepted"
    r = client.post(LIFECYCLE, json=_event(goldens, "active"), headers={"authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text


def test_unified_app_token_for_another_session_is_403_and_does_not_advance(closed_edge, goldens):
    client = _unified()
    tok_victim = _tok()
    assert client.post(LIFECYCLE, json=_event(goldens),
                       headers={"authorization": f"Bearer {tok_victim}"}).status_code == 200
    attacker = _tok("attacker-session", meeting_id=99)
    forged = dict(_event(goldens, "active"))  # names the victim's connection_id
    r = client.post(LIFECYCLE, json=forged, headers={"authorization": f"Bearer {attacker}"})
    assert r.status_code == 403, r.text
    # the victim's FSM did not move: its own next legal event still applies as a real advance
    r = client.post(LIFECYCLE, json=_event(goldens, "active"), headers={"authorization": f"Bearer {tok_victim}"})
    assert r.status_code == 200 and r.json()["meeting_status"] == "active", r.text


def test_unified_app_compat_mode_accepts_stock_bot_header(legacy_bot, goldens):
    client = _unified()
    tok = _tok()
    r = client.post(LIFECYCLE, json=_event(goldens), headers={"x-internal-secret": tok})
    assert r.status_code == 200, r.text
    r = client.post(LIFECYCLE, json=_event(goldens), headers={"x-internal-secret": LEGACY_INTERNAL})
    assert r.status_code == 403


def test_unified_app_accepts_runtime_secret(closed_edge):
    client = _unified()
    r = client.post(RUNTIME, json={"workloadId": "wl-1", "state": "destroyed"},
                    headers={"x-internal-secret": RUNTIME_SECRET})
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "accepted"}


def test_unified_app_fails_closed_when_unconfigured(monkeypatch, goldens):
    monkeypatch.setenv("DEV_MODE", "false")
    for k in ("INTERNAL_API_SECRET", "ADMIN_TOKEN", "MEETING_TOKEN_SECRET", "RUNTIME_CALLBACK_SECRET"):
        monkeypatch.delenv(k, raising=False)
    client = TestClient(create_app())  # no token_secret either
    assert client.post(LIFECYCLE, json=_event(goldens)).status_code == 503
    assert client.post(RUNTIME, json={"workloadId": "wl-1", "state": "destroyed"}).status_code == 503


def test_refusal_happens_before_body_parsing(closed_edge):
    """A malformed body from an unauthenticated caller is 403, never 422 — nothing is parsed."""
    client = _unified()
    r = client.post(LIFECYCLE, content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 403
    # an authenticated caller with a body that names no session is refused by the binding, not parsed further
    r = client.post(LIFECYCLE, content=b"not json",
                    headers={"content-type": "application/json", "authorization": f"Bearer {_tok()}"})
    assert r.status_code == 403


# ── the standalone lifecycle receiver ───────────────────────────────────────────────────────────

def test_receiver_app_applies_the_same_guard(closed_edge, goldens):
    store = MeetingStore()
    client = TestClient(create_receiver_app(store=store))
    assert client.post(LIFECYCLE, json=_event(goldens)).status_code == 403
    assert client.post(LIFECYCLE, json=_event(goldens),
                       headers={"x-internal-secret": LEGACY_INTERNAL}).status_code == 403
    assert client.post(LIFECYCLE, json=_event(goldens),
                       headers={"authorization": f"Bearer {_tok('other')}"}).status_code == 403
    tok = _tok()
    r = client.post(LIFECYCLE, json=_event(goldens), headers={"authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    r = client.post(LIFECYCLE, json=_event(goldens, "active"), headers={"authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    assert store.get(SESSION).status.value == "active"
