"""Meeting-URL allowlist — the ONE gate every meeting URL passes before a bot's browser goes to it.

Security finding #1 (vexa deep scan 2026-10-27): a meeting URL is handed to a headful Chromium
that runs on the internal network (``page.goto(meetingUrl)``), so an unchecked URL lets any API-key
holder point a bot at attacker JavaScript or at internal services. Substring host matching
(``"meet.google.com" in host``) let ``meet.google.com.evil.tld`` through. Every entry point
(``POST /bots``, ``POST/PATCH /meetings``, calendar sync, auto-join and the ``request_bot``
backstop) now calls :func:`check_meeting_url` or :func:`validate_native_meeting_id`. The module has
no I/O and imports nothing from the framework, so both the routers and the pure parsers can use it.

The rules, all static (no DNS on the spawn path):

* scheme ``https`` only;
* no userinfo (``user@host``), no port other than the default 443, no IP literal (v4, v6 or
  bracketed), no trailing-dot host, no non-ASCII (IDN) host — the host must already be the plain
  ASCII name, so look-alike Unicode and full-width dots cannot reach the matcher;
* no backslash, whitespace or control character anywhere, no percent-encoding in the authority, and
  no encoded dot/slash/backslash or ``.``/``..`` segment in the path;
* the host must EQUAL an allowlisted host, or be a subdomain of an allowlisted suffix on a dot
  boundary (``x.zoom.us`` matches ``zoom.us``; ``evilzoom.us`` and ``zoom.us.evil.tld`` do not);
* when the caller names a platform, the host must belong to that platform.

Allowlisted hosts (checked against the bot's own join code, ``core/meetings/modules/join``):

* google_meet — exactly ``meet.google.com``;
* teams — ``teams.microsoft.com``, ``teams.live.com``, ``teams.cloud.microsoft`` and
  ``teams.microsoft.us`` (GCC High / DoD), each with its subdomains (the join layer's
  ``TEAMS_HOST_SUFFIXES``);
* zoom — ``zoom.us`` and ``zoomgov.com`` with their subdomains (``us05web.zoom.us``,
  ``events.zoom.us``, ``frbmeetings.zoomgov.com``). White-label Zoom portals are not accepted.
"""
from __future__ import annotations

import ipaddress
import re
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

__all__ = [
    "MeetingUrlRejected",
    "allowed_platforms",
    "check_meeting_url",
    "platform_for_host",
    "validate_native_meeting_id",
]


class MeetingUrlRejected(ValueError):
    """The URL (or native meeting id) is not a joinable link on an allowlisted meeting host.
    Routers map it to HTTP 422; the message is safe to return to the caller."""


# platform → (exact hosts, suffixes matched as ``host == s or host.endswith("." + s)``)
_PLATFORM_HOSTS: dict[str, tuple[frozenset[str], tuple[str, ...]]] = {
    "google_meet": (frozenset({"meet.google.com"}), ()),
    "teams": (
        frozenset(),
        ("teams.microsoft.com", "teams.live.com", "teams.cloud.microsoft", "teams.microsoft.us"),
    ),
    "zoom": (frozenset(), ("zoom.us", "zoomgov.com")),
}

_MAX_URL_LEN = 2048
_MAX_NATIVE_ID_LEN = 255
# Raw characters that never belong in a meeting link: backslash (WHATWG parsers turn it into a
# slash, Python's urlsplit does not, so the two would disagree about the host), whitespace and C0/DEL
# controls (urlsplit silently strips tab/CR/LF).
_FORBIDDEN_RAW = re.compile(r"[\\\x00-\x20\x7f]")
# Percent-encoded backslash, controls or NUL anywhere in the URL.
_FORBIDDEN_ENCODED = re.compile(r"%(?:5c|[01][0-9a-f]|7f)", re.IGNORECASE)
# Percent-encoded dot or slash in the path (``/%2e%2e/``, ``%2f``): dot-segment and separator tricks.
_ENCODED_PATH_TRICK = re.compile(r"%(?:2e|2f)", re.IGNORECASE)
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

# Native-id rule for the platforms whose URL the API BUILDS from the id (construct_meeting_url).
# The id is interpolated into a URL path, so it must be a single URL-safe path token: no slash,
# ``?``, ``#``, ``%``, ``&``, ``=``, backslash, whitespace or ``..`` can reach the URL. The id's
# semantic shape (Meet ``abc-defg-hij``, Teams numeric / ``19:meeting_…@thread.v2``) is deliberately
# NOT enforced: ids that look wrong have joined real meetings, and the token rule already pins the
# host and the path.
_NATIVE_ID_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:@._~-]*$")


def allowed_platforms() -> tuple[str, ...]:
    """The platforms that have a meeting-URL allowlist."""
    return tuple(_PLATFORM_HOSTS)


def platform_for_host(host: str) -> Optional[str]:
    """The platform an (already normalised, lower-case ASCII) host belongs to, or ``None``."""
    for platform, (exact, suffixes) in _PLATFORM_HOSTS.items():
        if host in exact or any(host == s or host.endswith("." + s) for s in suffixes):
            return platform
    return None


def _normalise_host(netloc: str) -> str:
    """Validate the authority part and return the lower-case ASCII host (port 443 dropped)."""
    if not netloc:
        raise MeetingUrlRejected("meeting_url has no host")
    if "@" in netloc:
        raise MeetingUrlRejected("meeting_url must not contain credentials (user@host)")
    if "%" in netloc:
        raise MeetingUrlRejected("meeting_url host must not be percent-encoded")
    if netloc.startswith("["):
        raise MeetingUrlRejected("meeting_url cannot be an IP literal")
    host, sep, port = netloc.partition(":")
    if sep and port != "443":
        raise MeetingUrlRejected("meeting_url must use the default https port")
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise MeetingUrlRejected("meeting_url has an invalid hostname") from None
    if ascii_host != host.lower():
        # A non-ASCII host only reaches here as an IDN; the allowlist is ASCII, so any Unicode host
        # is a look-alike (or a full-width-dot spelling) of something else.
        raise MeetingUrlRejected("meeting_url host must be plain ASCII (no internationalised look-alikes)")
    if ascii_host.endswith("."):
        raise MeetingUrlRejected("meeting_url host must not end with a dot")
    try:
        ipaddress.ip_address(ascii_host)
    except ValueError:
        pass
    else:
        raise MeetingUrlRejected("meeting_url cannot be an IP literal")
    labels = ascii_host.split(".")
    if len(labels) < 2 or not all(_HOST_LABEL.match(label) for label in labels):
        raise MeetingUrlRejected("meeting_url has an invalid hostname")
    return ascii_host


def check_meeting_url(raw: object, *, platform: Optional[str] = None) -> tuple[str, str]:
    """Validate a meeting URL against the per-platform allowlist.

    Returns ``(normalised_url, platform)``; the normalised URL has a lower-case host and no explicit
    port. ``platform`` (when given) must match the host's platform. Raises
    :class:`MeetingUrlRejected` with a caller-safe message otherwise."""
    if not isinstance(raw, str) or not raw.strip():
        raise MeetingUrlRejected("meeting_url must be a non-empty string")
    url = raw.strip()
    if len(url) > _MAX_URL_LEN:
        raise MeetingUrlRejected(f"meeting_url is longer than {_MAX_URL_LEN} characters")
    if _FORBIDDEN_RAW.search(url):
        raise MeetingUrlRejected("meeting_url must not contain backslashes, whitespace or control characters")
    if _FORBIDDEN_ENCODED.search(url):
        raise MeetingUrlRejected("meeting_url must not contain encoded backslashes or control characters")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise MeetingUrlRejected("meeting_url does not parse as a URL") from None
    if parts.scheme.lower() != "https":
        raise MeetingUrlRejected("meeting_url must use https://")
    host = _normalise_host(parts.netloc)
    path = parts.path
    if _ENCODED_PATH_TRICK.search(path) or any(seg in (".", "..") for seg in path.split("/")):
        raise MeetingUrlRejected("meeting_url path must not contain dot segments or encoded separators")
    host_platform = platform_for_host(host)
    if host_platform is None:
        raise MeetingUrlRejected(
            f"meeting_url host '{host}' is not an allowed meeting host (allowed: "
            + "; ".join(_describe(p) for p in _PLATFORM_HOSTS)
            + ")"
        )
    if platform is not None and platform != host_platform:
        if platform not in _PLATFORM_HOSTS:
            raise MeetingUrlRejected(f"platform '{platform}' has no meeting-URL allowlist")
        raise MeetingUrlRejected(
            f"platform '{platform}' does not match meeting_url host '{host}' (a {host_platform} host)"
        )
    normalised = urlunsplit(("https", host, path, parts.query, parts.fragment))
    return normalised, host_platform


def validate_native_meeting_id(platform: str, native_meeting_id: object) -> str:
    """Validate a native meeting id that the API will interpolate into a join URL.

    Only the platforms with a URL template are checked here (google_meet, teams); for the others the
    caller must supply a full ``meeting_url``, which :func:`check_meeting_url` validates."""
    if not isinstance(native_meeting_id, str) or not native_meeting_id:
        raise MeetingUrlRejected("native_meeting_id must be a non-empty string")
    if len(native_meeting_id) > _MAX_NATIVE_ID_LEN:
        raise MeetingUrlRejected(f"native_meeting_id is longer than {_MAX_NATIVE_ID_LEN} characters")
    if platform in ("google_meet", "teams"):
        if not _NATIVE_ID_TOKEN.match(native_meeting_id) or ".." in native_meeting_id:
            raise MeetingUrlRejected(
                f"native_meeting_id for {platform} must be the bare meeting id (letters, digits and "
                "':@._~-'; no URL characters such as '/', '?', '#', '%' or '..') — pass any "
                "passcode separately or supply the full meeting_url"
            )
    return native_meeting_id


def _describe(platform: str) -> str:
    exact, suffixes = _PLATFORM_HOSTS[platform]
    hosts = sorted(exact) + [f"{s} (+subdomains)" for s in suffixes]
    return f"{platform}: {', '.join(hosts)}"
