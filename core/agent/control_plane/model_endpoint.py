"""model_endpoint.py — the policy for a SUBJECT-SUPPLIED model endpoint (Settings → Models
``mode: custom`` + ``base_url``).

Why this exists (security finding C1, 2026-10-25): a per-user ``base_url`` used to be overlaid onto
the dispatch env while the DEPLOYMENT's own credential (``ANTHROPIC_AUTH_TOKEN`` /
``ANTHROPIC_API_KEY`` / ``CLAUDE_CODE_OAUTH_TOKEN`` / ``VEXA_LLM_API_KEY``, or the mounted
subscription file) was still brokered into the worker whenever the user did not type an
``api_key``. Any user could therefore point the deployment's paid key at a host they control and
read it off the first request. The rules below close that and its SSRF sibling:

* A user-supplied endpoint and a user-supplied credential travel as a PAIR. The deployment's
  credentials are never sent to a user-supplied ``base_url`` — not from agent-api's env, not from
  the runtime's env, not from the subscription mount (``dispatch.overlay_model_config`` +
  ``runtime_kernel.docker_backend``).
* ``VEXA_MODEL_BASE_URL_ALLOWLIST`` (comma-separated hostnames; ``.example.com`` = that domain and
  its subdomains) restricts WHICH hosts a user may name. Empty = any public host.
* Private, loopback, link-local and reserved addresses, ``localhost``, ``*.internal`` / ``*.local`` /
  ``*.localhost``, and dotless hostnames (docker service names such as ``admin-api``) are refused
  unless the operator opts in with ``VEXA_MODEL_ALLOW_PRIVATE_ENDPOINTS=1`` (a LiteLLM on the
  docker network is a legitimate dev setup; it is not a legitimate thing for a tenant to choose).

Pure functions over the URL string + the operator's dials. The identity service (admin-api) holds
a byte-identical copy of the host rule for the write-time 422 — the packages cannot import each
other — so keep ``endpoint_host_problem`` in sync with ``admin_api.app.main._endpoint_host_problem``.
"""
from __future__ import annotations

import ipaddress
from typing import Optional
from urllib.parse import urlsplit

ALLOWLIST_ENV = "VEXA_MODEL_BASE_URL_ALLOWLIST"
ALLOW_PRIVATE_ENV = "VEXA_MODEL_ALLOW_PRIVATE_ENDPOINTS"

# Hostname suffixes that only ever name something on the operator's own network.
_PRIVATE_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")
_TRUTHY = ("1", "true", "yes", "on")


def parse_allowlist(raw: str) -> tuple[str, ...]:
    """``"gw.example.com, .openrouter.ai"`` → ``("gw.example.com", ".openrouter.ai")`` (lowercased)."""
    return tuple(h.strip().lower() for h in (raw or "").split(",") if h.strip())


def allow_private_from_env(raw: Optional[str]) -> bool:
    return (raw or "").strip().lower() in _TRUTHY


def host_matches(host: str, allowlist: tuple[str, ...]) -> bool:
    """Exact match, or suffix match for ``.domain`` entries (``.example.com`` admits
    ``example.com`` and every subdomain). Empty allowlist = anything goes."""
    if not allowlist:
        return True
    host = host.lower().rstrip(".")
    for entry in allowlist:
        if entry.startswith("."):
            if host == entry[1:] or host.endswith(entry):
                return True
        elif host == entry:
            return True
    return False


def is_private_host(host: str) -> bool:
    """True for anything that can only name the operator's own network: loopback / private /
    link-local / reserved / unspecified literal IPs (v4 and v6, incl. bracketed), ``localhost``,
    the private suffixes above, and DOTLESS names (compose/k8s service names)."""
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return True
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        ip = None
    if ip is not None:
        return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                    or ip.is_multicast or ip.is_unspecified)
    if h == "localhost" or h.endswith(_PRIVATE_SUFFIXES):
        return True
    return "." not in h


def endpoint_host_problem(base_url: str, *, allowlist: tuple[str, ...] = (),
                          allow_private: bool = False) -> Optional[str]:
    """``None`` when ``base_url`` may be used as a subject-supplied model endpoint, else the
    human-readable reason (the same text the 422 / dispatch log / Test button show)."""
    raw = (base_url or "").strip()
    if not raw:
        return "base_url is empty"
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        return f"base_url is not a valid URL ({exc})"
    if parts.scheme not in ("http", "https"):
        return "base_url must be an http(s) URL"
    if parts.username is not None or parts.password is not None:
        return "base_url must not carry userinfo (user:pass@host)"
    host = (parts.hostname or "").strip()
    if not host:
        return "base_url must name a host"
    if not allow_private and is_private_host(host):
        return (f"base_url host {host!r} is a private/internal address; model endpoints must be "
                f"public hosts (operator opt-out: {ALLOW_PRIVATE_ENV}=1)")
    if not host_matches(host, allowlist):
        return (f"base_url host {host!r} is not on {ALLOWLIST_ENV} "
                f"({', '.join(allowlist)})")
    return None
