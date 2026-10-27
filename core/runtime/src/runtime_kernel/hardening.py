"""Container hardening for spawned workloads (security finding #4, vexa deep scan 2026-10-27).

Meeting bots run a headful Chromium (``--no-sandbox``) that navigates third-party pages, and agent
workers run model-driven code. Before this module every workload container ran as root with
Docker's default capability set (NET_RAW included), no memory, pids or CPU ceiling, and a writable
root filesystem. :func:`hardening_host_config` returns the HostConfig fields the docker backend
merges into every create call, per workload class:

=========================  ========================  ===================  =====================
field                      meeting-bot               agent                other (tests, …)
=========================  ========================  ===================  =====================
CapDrop                    ALL                       ALL                  ALL
SecurityOpt                no-new-privileges:true    no-new-privileges    no-new-privileges
Memory / MemorySwap        4g / 4g (no swap)         2g / 2g              2g / 2g
PidsLimit                  1024                      512                  512
NanoCpus                   2 CPUs                    1 CPU                1 CPU
ReadonlyRootfs + Tmpfs     opt-in (off)              —                    —
User                       opt-in (unset = image)    —                    —
=========================  ========================  ===================  =====================

The defaults were tested against the stock bot images prod runs (``vexaai/vexa-bot:v0.12.0-rc.10``
and ``vexaai/vexa-bot:v012`` = v0.12.28): with CapDrop ALL, no-new-privileges and the limits, Xvfb,
fluxbox, PulseAudio (``tts_sink``/``virtual_mic``) and Chromium all start and the bot reaches the
Meet pre-join page. The stock images are built to run as root and write to ``/tmp``, ``/root``,
``/var/cache/fontconfig`` and ``/app/storage`` (screenshots); recordings are buffered in ``/tmp``.
A read-only rootfs and a non-root user each got as far as the pre-join page in the same test, but
together PulseAudio does not start (the non-root ``HOME`` is on the read-only rootfs), admission,
capture and recording upload were NOT exercised, and a tmpfs ``/tmp`` would move recordings into
memory. So both stay behind flags, off by default, for a bot image built for them.

Configuration (runtime env, all declared in ``config.v1.json``):

* ``RUNTIME_DOCKER_HARDENING`` — ``on`` (default) / ``off``: the emergency switch; ``off`` restores
  the pre-hardening create payload.
* ``RUNTIME_DOCKER_{BOT,AGENT}_MEMORY`` (``4g``/``2g``; ``k``/``m``/``g`` suffixes or bytes),
  ``RUNTIME_DOCKER_{BOT,AGENT}_PIDS_LIMIT`` (``1024``/``512``),
  ``RUNTIME_DOCKER_{BOT,AGENT}_CPUS`` (``2``/``1``; fractional allowed). ``0`` removes that
  ceiling; an unparseable value is fatal (a constraint silently dropped is the bug).
* ``RUNTIME_DOCKER_BOT_READONLY_ROOTFS`` (``false``) — read-only rootfs plus tmpfs for the bot's
  write paths (``/tmp`` 1g, ``/root`` 128m, ``/var/cache/fontconfig`` 64m, ``/app/storage`` 256m).
* ``RUNTIME_DOCKER_BOT_USER`` (unset) — run the bot as this ``uid[:gid]`` (the stock image has
  ``pwuser`` = ``1001:1001``; it needs a writable ``/app/storage`` and ``HOME``).
"""
from __future__ import annotations

import os
from typing import Any, Optional

#: The ONLY SecurityOpt values a workload may carry — exactly what this module sets. A C5-style
#: host-config guard (``docker_backend._assert_safe_host_config`` on fpn; its main port is #12) must
#: accept exactly these and refuse anything else (``seccomp=unconfined``, ``apparmor=unconfined``,
#: ``label=disable``, …).
SAFE_SECURITY_OPT = frozenset({"no-new-privileges:true", "no-new-privileges"})

_BOT = "meeting-bot"
_AGENT = "agent"

_DEFAULTS = {
    _BOT: {"memory": "4g", "pids": "1024", "cpus": "2"},
    _AGENT: {"memory": "2g", "pids": "512", "cpus": "1"},
}
_ENV_PREFIX = {_BOT: "RUNTIME_DOCKER_BOT", _AGENT: "RUNTIME_DOCKER_AGENT"}

#: Write paths of the stock bot image (see the module docstring), used when the read-only rootfs is
#: switched on. Mode 1777 on /tmp keeps it usable by a non-root user.
BOT_TMPFS = {
    "/tmp": "rw,nosuid,nodev,mode=1777,size=1g",
    "/root": "rw,nosuid,nodev,size=128m",
    "/var/cache/fontconfig": "rw,nosuid,nodev,size=64m",
    "/app/storage": "rw,nosuid,nodev,mode=1777,size=256m",
}


def _flag(key: str, default: bool) -> bool:
    raw = os.getenv(key, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{key} must be on/off/true/false, got {raw!r}")


def hardening_enabled() -> bool:
    return _flag("RUNTIME_DOCKER_HARDENING", True)


def _bytes(key: str, raw: str) -> Optional[int]:
    value = raw.strip().lower()
    mult = {"k": 1024, "m": 1024**2, "g": 1024**3}.get(value[-1:], 1)
    digits = value[:-1] if value[-1:] in ("k", "m", "g") else value
    try:
        n = int(digits)
    except ValueError:
        raise ValueError(f"{key} must be a size like 4g / 512m / bytes, got {raw!r}") from None
    if n < 0:
        raise ValueError(f"{key} must not be negative")
    return n * mult or None


def _int(key: str, raw: str) -> Optional[int]:
    try:
        n = int(raw.strip())
    except ValueError:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from None
    if n < 0:
        raise ValueError(f"{key} must not be negative")
    return n or None


def _nano_cpus(key: str, raw: str) -> Optional[int]:
    try:
        cpus = float(raw.strip())
    except ValueError:
        raise ValueError(f"{key} must be a number of CPUs, got {raw!r}") from None
    if cpus < 0:
        raise ValueError(f"{key} must not be negative")
    return int(cpus * 1e9) or None


def _limits(workload_class: Optional[str]) -> dict[str, Optional[int]]:
    cls = workload_class if workload_class in _DEFAULTS else _AGENT
    prefix = _ENV_PREFIX[cls]
    d = _DEFAULTS[cls]
    mem_key, pids_key, cpus_key = f"{prefix}_MEMORY", f"{prefix}_PIDS_LIMIT", f"{prefix}_CPUS"
    return {
        "memory": _bytes(mem_key, os.getenv(mem_key) or d["memory"]),
        "pids": _int(pids_key, os.getenv(pids_key) or d["pids"]),
        "nano_cpus": _nano_cpus(cpus_key, os.getenv(cpus_key) or d["cpus"]),
    }


def hardening_host_config(
    workload_class: Optional[str],
    *,
    memory_bytes: Optional[int] = None,
    nano_cpus: Optional[int] = None,
) -> dict[str, Any]:
    """The HostConfig fields to merge into a workload's create call (``{}`` when hardening is off).

    ``workload_class`` is the profile's class (``meeting-bot`` / ``agent``; anything else gets the
    agent-sized defaults). ``memory_bytes`` / ``nano_cpus`` override the env-configured ceilings
    when the caller resolved explicit resources for this workload."""
    if not hardening_enabled():
        return {}
    limits = _limits(workload_class)
    memory = memory_bytes if memory_bytes else limits["memory"]
    cpus = nano_cpus if nano_cpus else limits["nano_cpus"]
    hc: dict[str, Any] = {
        "CapDrop": ["ALL"],
        "SecurityOpt": ["no-new-privileges:true"],
    }
    if memory:
        hc["Memory"] = memory
        hc["MemorySwap"] = memory  # == Memory ⇒ no swap on top of the ceiling
    if limits["pids"]:
        hc["PidsLimit"] = limits["pids"]
    if cpus:
        hc["NanoCpus"] = cpus
    if workload_class == _BOT:
        if _flag("RUNTIME_DOCKER_BOT_READONLY_ROOTFS", False):
            hc["ReadonlyRootfs"] = True
            hc["Tmpfs"] = dict(BOT_TMPFS)
    return hc


def hardening_user(workload_class: Optional[str]) -> Optional[str]:
    """The container ``User`` for this class, or ``None`` to keep the image's own user."""
    if workload_class == _BOT and hardening_enabled():
        user = (os.getenv("RUNTIME_DOCKER_BOT_USER") or "").strip()
        return user or None
    return None
