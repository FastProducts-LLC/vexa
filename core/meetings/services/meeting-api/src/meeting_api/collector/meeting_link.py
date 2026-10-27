"""Meeting-link → ``(platform, native_meeting_id)`` parsing — the server-side twin of the
terminal's ``clients/terminal/src/surfaces/meetingId.ts`` (same id formats, same platforms).

Used by ``POST /meetings`` / ``PATCH /meetings/{id}`` (a planned meeting created from a pasted
link) and by ``calendar_sync`` (extracting the joinable link out of an ICS event's LOCATION /
DESCRIPTION). Pure string logic — no I/O, no framework imports; the one config read is
``VEXA_JITSI_HOSTS`` (P14, declared in config.v1.json), consulted per call so tests and
reloads see the live env.

Id formats (mirrors the dashboard join-form):
  * google_meet → ``abc-defg-hij``
  * zoom        → 9–11 digits
  * teams       → the ``19:meeting_…@thread.v2`` thread id, or the ``/meet/<id>`` short-link segment
  * jitsi       → the room name (the URL's path segment). Hosts: meet.jit.si, 8x8.vc and the
                  ``VEXA_JITSI_HOSTS``-declared deployments (``meeting_url_policy``). The room
                  name is deployment-scoped, so the raw URL rides alongside as ``meeting_url`` —
                  never reconstructed from the id.

Every URL must pass ``meeting_url_policy.check_meeting_url`` (the shared allowlist, security
finding #1) before an id is extracted; the old substring host checks accepted
``meet.google.com.evil.tld``, any host containing "zoom", and any ``meet.*`` host as jitsi.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import unquote, urlsplit

from ..meeting_url_policy import MeetingUrlRejected, check_meeting_url

_GMEET_ID = re.compile(r"^[a-z]{3}-[a-z]{4}-[a-z]{3}$")
_ZOOM_ID = re.compile(r"\d{9,11}")
_TEAMS_THREAD = re.compile(r"19:meeting_[^@%\s/]+@thread\.v2", re.IGNORECASE)
_TEAMS_SHORT = re.compile(r"/meet/([^/?#]+)", re.IGNORECASE)
# A Jitsi room is the URL path's single segment; permissive by design (jitsi accepts nearly any
# room string) but excludes separators/whitespace so a mangled URL never yields a bogus room.
_JITSI_ROOM = re.compile(r"^[^/?#\s]+$")


def parse_meeting_url(raw: str, *, generic_hosts: bool = True) -> Optional[tuple[str, str]]:
    """Parse a pasted meeting URL (or bare id) → ``(platform, native_meeting_id)``, or ``None``
    when nothing valid can be extracted. Accepts the same inputs the terminal's
    ``parseMeetingInput`` accepts, so a link that validates client-side also validates here.

    ``generic_hosts`` is accepted for compatibility and no longer widens anything: jitsi hosts are
    the allowlisted ones (meet.jit.si, 8x8.vc, ``VEXA_JITSI_HOSTS``) for pasted links and the ICS
    scan alike (finding #1 — the old "jitsi"/``meet.*`` naming heuristic accepted any such host)."""
    value = (raw or "").strip()
    if not value:
        return None

    # Bare Google Meet code, e.g. "abc-defg-hij"
    if _GMEET_ID.match(value.lower()):
        return ("google_meet", value.lower())

    if "://" in value or value.lower().startswith(("http:", "https:")):
        try:
            url, platform = check_meeting_url(value)
        except MeetingUrlRejected:
            return None
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if platform == "google_meet":
            code = next((p for p in reversed(parsed.path.split("/")) if p), "").lower()
            return ("google_meet", code) if _GMEET_ID.match(code) else None
        if platform == "zoom":
            m = _ZOOM_ID.search(parsed.path) or _ZOOM_ID.search(parsed.query)
            return ("zoom", m.group(0)) if m else None
        if platform == "teams":
            # Classic deep link carries the thread id (…/l/meetup-join/19:meeting_…@thread.v2).
            thread = _TEAMS_THREAD.search(unquote(url))
            if thread:
                return ("teams", thread.group(0))
            # New short meeting link: teams.microsoft.com/meet/<id>?p=<passcode>.
            short = _TEAMS_SHORT.search(parsed.path)
            if short:
                return ("teams", short.group(1))
            return None
        if platform == "jitsi":
            # The room is the path's single segment, kept EXACTLY as it appears in the URL (case +
            # percent-encoding preserved) — the native id is embedded back into the DELETE path
            # param, so it must stay URL-safe; decoding would corrupt rooms with encoded
            # characters. Callers keep the raw URL alongside (``meeting_url``) so a self-hosted
            # room joins on ITS deployment.
            room = parsed.path.strip("/")
            if not room or not _JITSI_ROOM.match(room):
                return None
            # A jitsi room name is deployment-scoped, so the native id embeds the host for every
            # non-canonical deployment (room@host — jitsi's own XMPP identity shape). meet.jit.si
            # keeps the bare room (canonical, unambiguous).
            return ("jitsi", room if host == "meet.jit.si" else f"{room}@{host}")
        return None

    # Bare numeric id → assume Zoom
    if re.fullmatch(r"\d{9,11}", value):
        return ("zoom", value)

    return None


def find_meeting_link(text: str) -> Optional[tuple[str, str, str]]:
    """Scan free text (an ICS LOCATION/DESCRIPTION) for the FIRST recognizable meeting URL →
    ``(platform, native_meeting_id, url)``, or ``None``. Only http(s) URLs are considered."""
    if not text:
        return None
    for m in re.finditer(r"https?://[^\s<>\"']+", text):
        url = m.group(0).rstrip(").,;")
        # Free-text scan: only allowlisted meeting hosts (meeting_url_policy) are recognised.
        parsed = parse_meeting_url(url, generic_hosts=False)
        if parsed:
            return (parsed[0], parsed[1], url)
    return None
