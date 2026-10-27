"""Meeting-URL allowlist (security finding #1) — the shared ``meeting_url_policy`` gate and every
entry point that feeds a URL to a bot's browser: ``POST /bots``, ``POST/PATCH /meetings``, calendar
(ICS) import, the auto-join sweep and the ``request_bot`` backstop.

Drives the shipped code over the in-memory fakes, OFFLINE.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meeting_api.bot_spawn import build_router
from meeting_api.bot_spawn.auto_join import auto_join_tick
from meeting_api.bot_spawn.fakes import FakeRuntimeClient, InMemoryMeetingRepo
from meeting_api.bot_spawn.service import request_bot
from meeting_api.calendar_sync import parse_ics
from meeting_api.collector import create_app
from meeting_api.collector.fakes import InMemoryTranscriptStore
from meeting_api.collector.meeting_link import find_meeting_link, parse_meeting_url
from meeting_api.meeting_url_policy import (
    MeetingUrlRejected,
    check_meeting_url,
    platform_for_host,
    validate_native_meeting_id,
)

USER = 7
H = {"x-user-id": str(USER)}

# ---- the validator: accepted links, per platform --------------------------------------------

VALID = [
    ("https://meet.google.com/abc-defg-hij", "google_meet"),
    ("https://meet.google.com/abc-defg-hij?authuser=0&hl=en", "google_meet"),
    ("https://MEET.Google.com/abc-defg-hij", "google_meet"),
    ("https://meet.google.com:443/abc-defg-hij", "google_meet"),
    ("https://teams.microsoft.com/l/meetup-join/19%3ameeting_YmFk%40thread.v2/0?context=%7b%22Tid%22%3a%22x%22%7d", "teams"),
    ("https://teams.microsoft.com/meet/9351274713?p=abc", "teams"),
    ("https://teams.live.com/meet/9351274713?p=abc", "teams"),
    ("https://gov.teams.microsoft.us/l/meetup-join/19%3ameeting_x%40thread.v2/0", "teams"),
    ("https://teams.cloud.microsoft/meet/9351274713?p=abc", "teams"),
    ("https://emea.teams.microsoft.com/l/meetup-join/19%3ameeting_x%40thread.v2/0", "teams"),
    ("https://zoom.us/j/1234567890", "zoom"),
    ("https://us05web.zoom.us/j/84335626851?pwd=abc.1", "zoom"),
    ("https://events.zoom.us/ejl/abc", "zoom"),
    ("https://app.zoom.us/wc/84335626851/join?pwd=x", "zoom"),
    ("https://zoomgov.com/j/1234567890", "zoom"),
    ("https://frbmeetings.zoomgov.com/j/12345678901?pwd=xyz", "zoom"),
]


@pytest.mark.parametrize("url,platform", VALID)
def test_valid_meeting_urls_pass(url, platform):
    normalised, got = check_meeting_url(url)
    assert got == platform
    assert normalised.startswith("https://")
    # the named platform agrees with the host
    assert check_meeting_url(url, platform=platform)[1] == platform


def test_normalisation_lowercases_host_and_drops_default_port():
    assert check_meeting_url("https://MEET.Google.com:443/abc-defg-hij")[0] == (
        "https://meet.google.com/abc-defg-hij"
    )


# ---- the validator: evil cases ---------------------------------------------------------------

EVIL = [
    "https://meet.google.com.evil.tld/abc-defg-hij",        # suffix appended (old substring bug)
    "https://evil.tld/meet.google.com",                     # allowlisted name in the path
    "https://evil.tld/?u=https://meet.google.com/abc",      # ... or the query
    "https://meet.google.com@evil.tld/abc-defg-hij",        # userinfo
    "https://user:pw@meet.google.com/abc-defg-hij",         # userinfo on an allowed host
    "https://@meet.google.com/abc-defg-hij",                # empty userinfo
    "http://meet.google.com/abc-defg-hij",                  # not https
    "javascript:alert(1)//meet.google.com",                 # other schemes
    "file:///etc/passwd",
    "//meet.google.com/abc-defg-hij",                       # scheme-relative
    "https://meet.google.com:8443/abc-defg-hij",            # non-default port
    "https://meet.google.com:/abc-defg-hij",                # empty port
    "https://meet.google.com:0443/abc-defg-hij",            # port spelled differently
    "https://[::1]/abc",                                    # IPv6 literal
    "https://[::ffff:127.0.0.1]/abc",
    "https://127.0.0.1/abc",                                # IPv4 literal
    "https://169.254.169.254/latest/meta-data",
    "https://2130706433/abc",                               # integer IPv4 spelling
    "https://localhost/abc",
    "https://meet.google.com./abc-defg-hij",                # trailing dot
    "https://meet.gооgle.com/abc-defg-hij",                 # Cyrillic о look-alike (IDN)
    "https://xn--meet-google-com-xyz.evil.tld/abc",         # punycode host, not allowlisted
    "https://xn--mt-xka.google.com/abc",                    # punycode label under google.com
    "https://meet。google。com/abc-defg-hij",                # full-width dots (IDNA would map them)
    "https://meet.google.com\\@evil.tld/abc",               # backslash
    "https://evil.tld\\meet.google.com/abc",
    "https://meet.google.com/%5c%5cevil.tld",               # encoded backslash
    "https://meet%2egoogle%2ecom/abc-defg-hij",             # encoded dots in the host
    "https://meet.google.com%2eevil.tld/abc",
    "https://meet.google.com/%2e%2e/%2e%2e/evil",           # encoded dot segments
    "https://meet.google.com/a/../../evil",                 # literal dot segments
    "https://meet.google.com/a%2fb",                        # encoded slash
    "https://meet.google.com/abc def",                      # whitespace
    "https://meet.google.com/abc\tdef",                     # tab (urlsplit would strip it)
    "https://meet.google.com/abc\ndef",
    "https://meet.google.com/%00",                          # encoded NUL
    "https://evilzoom.us/j/1234567890",                     # not a dot boundary
    "https://zoom.us.evil.tld/j/1234567890",
    "https://zoom-lfx.platform.linuxfoundation.org/meeting/96088138284",  # white-label portal
    "https://teams.microsoft.com.evil.tld/l/meetup-join/x",
    "https://notteams.live.com/meet/1",
    "https://evil.cloud.microsoft/meet/1",
    "https://google.com/abc-defg-hij",                      # parent of meet.google.com
    "https://evil.meet.google.com/abc-defg-hij",            # Meet is exact-host only
    "https:/meet.google.com/abc-defg-hij",
    "https:///meet.google.com/abc",
    "",
    "   ",
    "x" * 3000,
]


@pytest.mark.parametrize("url", EVIL)
def test_evil_meeting_urls_rejected(url):
    with pytest.raises(MeetingUrlRejected):
        check_meeting_url(url)


@pytest.mark.parametrize("value", [None, 123, ["https://meet.google.com/abc-defg-hij"], {"u": 1}])
def test_non_string_rejected(value):
    with pytest.raises(MeetingUrlRejected):
        check_meeting_url(value)


@pytest.mark.parametrize("url,platform", [
    ("https://meet.google.com/abc-defg-hij", "zoom"),
    ("https://us05web.zoom.us/j/84335626851", "google_meet"),
    ("https://teams.microsoft.com/meet/1", "zoom"),
    ("https://meet.google.com/abc-defg-hij", "browser_session"),
    ("https://meet.google.com/abc-defg-hij", "unknown"),
])
def test_platform_must_match_host(url, platform):
    with pytest.raises(MeetingUrlRejected):
        check_meeting_url(url, platform=platform)


def test_platform_for_host_is_dot_boundary():
    assert platform_for_host("zoom.us") == "zoom"
    assert platform_for_host("a.b.zoom.us") == "zoom"
    assert platform_for_host("evilzoom.us") is None
    assert platform_for_host("meet.google.com") == "google_meet"
    assert platform_for_host("x.meet.google.com") is None


# ---- native ids the API builds a URL from -----------------------------------------------------

@pytest.mark.parametrize("platform,nid", [
    ("google_meet", "abc-defg-hij"),
    ("google_meet", "legacy_id-1"),
    ("teams", "9351274713"),
    ("teams", "19:meeting_YmFkZGVk@thread.v2"),
    ("zoom", "anything"),            # zoom has no URL template: the caller must send meeting_url
])
def test_native_ids_accepted(platform, nid):
    assert validate_native_meeting_id(platform, nid) == nid


@pytest.mark.parametrize("platform,nid", [
    ("google_meet", "abc/../../evil"),
    ("google_meet", "@evil.tld"),
    ("google_meet", ".evil.tld/x"),
    ("google_meet", "abc?x=1"),
    ("google_meet", "abc#frag"),
    ("google_meet", "abc%2fx"),
    ("google_meet", "a\\b"),
    ("google_meet", "abc def"),
    ("google_meet", ""),
    ("google_meet", "a" * 300),
    ("teams", "19:meeting_x@thread.v2/../../evil"),
    ("teams", "..evil"),
    ("teams", "1?p=x"),
    ("teams", "/evil.tld"),
])
def test_native_ids_rejected(platform, nid):
    with pytest.raises(MeetingUrlRejected):
        validate_native_meeting_id(platform, nid)


# ---- POST /bots -------------------------------------------------------------------------------

def _bots_client(repo=None, runtime=None):
    app = FastAPI()
    app.include_router(build_router(repo or InMemoryMeetingRepo(), runtime or FakeRuntimeClient()))
    return TestClient(app)


@pytest.fixture
def _spawnable(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "test-secret")
    monkeypatch.setenv("TRANSCRIPTION_SERVICE_URL", "http://stt")
    monkeypatch.setenv("TRANSCRIPTION_SERVICE_TOKEN", "t")


@pytest.mark.parametrize("body", [
    {"platform": "google_meet", "native_meeting_id": "abc-defg-hij",
     "meeting_url": "https://meet.google.com.evil.tld/abc-defg-hij"},
    {"platform": "zoom", "native_meeting_id": "1234567890", "meeting_url": "https://evil.tld/j/1234567890"},
    {"platform": "zoom", "native_meeting_id": "1234567890", "meeting_url": "https://zoom.us@evil.tld/j/1"},
    {"platform": "zoom", "native_meeting_id": "1234567890", "meeting_url": "http://zoom.us/j/1234567890"},
    {"platform": "zoom", "native_meeting_id": "1234567890", "meeting_url": "https://zoom.us:8443/j/1"},
    {"platform": "zoom", "native_meeting_id": "1234567890", "meeting_url": "https://[::1]/j/1"},
    {"platform": "google_meet", "native_meeting_id": "abc-defg-hij",
     "meeting_url": "https://us05web.zoom.us/j/84335626851"},                  # platform mismatch
    {"platform": "google_meet", "native_meeting_id": "abc/../x"},             # bad constructed id
    {"platform": "teams", "native_meeting_id": "1?p=x"},
    {"platform": "zoom", "native_meeting_id": "1", "meeting_url": 42},
])
def test_post_bots_rejects_non_allowlisted_urls_with_422(_spawnable, body):
    runtime = FakeRuntimeClient()
    repo = InMemoryMeetingRepo()
    r = _bots_client(repo, runtime).post("/bots", headers=H, json=body)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]
    assert runtime.specs == []          # nothing spawned
    assert repo._meetings == {}         # nothing written


@pytest.mark.parametrize("body,expected_url", [
    ({"platform": "google_meet", "native_meeting_id": "abc-defg-hij"},
     "https://meet.google.com/abc-defg-hij"),
    ({"platform": "zoom", "native_meeting_id": "84335626851",
      "meeting_url": "https://US05WEB.zoom.us/j/84335626851?pwd=x"},
     "https://us05web.zoom.us/j/84335626851?pwd=x"),
    ({"platform": "teams", "native_meeting_id": "9351274713",
      "meeting_url": "https://teams.microsoft.com/meet/9351274713?p=abc"},
     "https://teams.microsoft.com/meet/9351274713?p=abc"),
])
def test_post_bots_accepts_allowlisted_urls(_spawnable, body, expected_url):
    import json

    runtime = FakeRuntimeClient()
    r = _bots_client(runtime=runtime).post("/bots", headers=H, json=body)
    assert r.status_code == 201, r.text
    (spec,) = runtime.specs
    assert json.loads(spec["env"]["VEXA_BOT_CONFIG"])["meetingUrl"] == expected_url


# ---- request_bot backstop (every caller, not just the router) --------------------------------

def test_request_bot_backstop_rejects_before_any_write():
    repo, runtime = InMemoryMeetingRepo(), FakeRuntimeClient()
    with pytest.raises(MeetingUrlRejected):
        asyncio.run(request_bot(
            repo, runtime, user_id=USER, platform="zoom", native_meeting_id="1234567890",
            meeting_url="https://zoom.us.evil.tld/j/1234567890", token_secret="s",
            redis_url="redis://r",
        ))
    assert repo._meetings == {} and runtime.specs == []


# ---- auto-join of a stored link ----------------------------------------------------------------

def test_auto_join_refuses_a_stored_evil_link():
    repo, runtime = InMemoryMeetingRepo(), FakeRuntimeClient()
    now = datetime(2026, 7, 10, 15, 0, 0, tzinfo=timezone.utc)
    repo._meetings[1] = {
        "id": 1, "user_id": USER, "platform": "zoom", "native_meeting_id": "1234567890",
        "platform_specific_id": "1234567890", "status": "scheduled", "bot_container_id": None,
        "start_time": None, "end_time": None,
        "data": {"title": "t", "auto_join": True, "scheduled_at": now.isoformat(),
                 "constructed_meeting_url": "https://zoom.us.evil.tld/j/1234567890"},
        "created_at": "2026-07-08T09:00:00Z", "updated_at": "2026-07-08T09:00:00Z",
    }
    counters = asyncio.run(auto_join_tick(
        repo, runtime, transcribe_gate=lambda: None, now=now, token_secret="s",
        redis_url="redis://r",
    ))
    assert counters["errors"] == 1 and counters["spawned"] == 0
    assert runtime.specs == []
    assert "meeting link rejected" in repo._meetings[1]["data"]["auto_join_error"]


# ---- POST/PATCH /meetings and calendar import (the pasted-link parser) -----------------------

@pytest.mark.parametrize("url", [
    "https://meet.google.com.evil.tld/abc-defg-hij",
    "https://evil.tld/meet.google.com/abc-defg-hij",
    "https://meet.google.com@evil.tld/abc-defg-hij",
    "http://meet.google.com/abc-defg-hij",
    "https://zoom.evil.tld/j/1234567890",
    "https://teams.microsoft.com.evil.tld/meet/9351274713",
])
def test_parse_meeting_url_rejects_lookalikes(url):
    assert parse_meeting_url(url) is None


def test_parse_meeting_url_still_parses_real_links():
    assert parse_meeting_url("https://meet.google.com/abc-defg-hij") == ("google_meet", "abc-defg-hij")
    assert parse_meeting_url("https://us02web.zoom.us/j/1234567890?pwd=x") == ("zoom", "1234567890")
    assert parse_meeting_url("https://teams.microsoft.com/meet/9351274713?p=abc") == ("teams", "9351274713")
    assert parse_meeting_url("abc-defg-hij") == ("google_meet", "abc-defg-hij")
    assert parse_meeting_url("1234567890") == ("zoom", "1234567890")


def test_find_meeting_link_skips_lookalikes_and_takes_the_real_link():
    text = ("Join https://meet.google.com.evil.tld/abc-defg-hij or "
            "https://meet.google.com/xyz-abcd-efg")
    assert find_meeting_link(text) == ("google_meet", "xyz-abcd-efg", "https://meet.google.com/xyz-abcd-efg")


def test_calendar_import_never_takes_an_evil_link():
    ics = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
           "BEGIN:VEVENT\r\nUID:u1\r\nDTSTAMP:20260701T000000Z\r\nDTSTART:20260708T150000Z\r\n"
           "SUMMARY:s\r\nLOCATION:https://meet.google.com.evil.tld/abc-defg-hij\r\nEND:VEVENT\r\n"
           "END:VCALENDAR\r\n")
    (ev,) = parse_ics(ics, now=datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc))["events"]
    assert ev["meeting_url"] is None and ev["platform"] is None


def _collector_client():
    return TestClient(create_app(InMemoryTranscriptStore(), redis=None))


@pytest.mark.parametrize("url", [
    "https://meet.google.com.evil.tld/abc-defg-hij",
    "https://meet.google.com@evil.tld/abc-defg-hij",
    "https://zoom.us.evil.tld/j/1234567890",
])
def test_post_and_patch_meetings_reject_evil_links(url):
    client = _collector_client()
    assert client.post("/meetings", json={"meeting_url": url}, headers=H).status_code == 422
    r = client.post("/meetings", json={"title": "x"}, headers=H)
    assert r.status_code == 201, r.text
    mid = r.json()["id"]
    assert client.patch(f"/meetings/{mid}", json={"meeting_url": url}, headers=H).status_code == 422
