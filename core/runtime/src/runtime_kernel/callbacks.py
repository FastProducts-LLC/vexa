"""Durable RuntimeEvent callback delivery. 0.11's `lifecycle._deliver_callback` left a pending record
in Redis on burst-exhaustion so `idle_loop` could retry every tick — that is the single mechanism
making exit-callback delivery eventually-complete across consumer outages. We reimplement that here as
a small queue + sweep so the kernel's API doesn't fire-once-and-forget.

  • enqueue(url, event)  — record a pending delivery.
  • sweep()              — try every pending delivery once; drop the ones the receiver acked (2xx/3xx),
                          KEEP the ones that failed so the next sweep retries them.

The transport is injectable: production posts with httpx; the eval supplies a fake receiver. The
backing store is a PendingStore Protocol (in-memory by default; a Redis adapter mirrors 0.11's
`runtime:callback:*` keys)."""
from __future__ import annotations

import json
import logging
from typing import Callable, Optional, Protocol

logger = logging.getLogger("runtime_kernel.callbacks")


# A poster returns the HTTP status code (or raises on transport failure).
Poster = Callable[[str, dict, dict], int]


class PendingStore(Protocol):
    def put(self, key: str, value: dict) -> None: ...
    def get_all(self) -> dict[str, dict]: ...
    def delete(self, key: str) -> None: ...


class InMemoryPendingStore:
    def __init__(self) -> None:
        self._d: dict[str, dict] = {}

    def put(self, key: str, value: dict) -> None:
        self._d[key] = value

    def get_all(self) -> dict[str, dict]:
        return dict(self._d)

    def delete(self, key: str) -> None:
        self._d.pop(key, None)


class RedisPendingStore:
    """Mirrors 0.11's `runtime:callback:*` pending-callback keys."""

    PREFIX = "runtime:callback:"

    def __init__(self, redis, ttl: int = 3600) -> None:
        self._r = redis
        self._ttl = ttl

    @staticmethod
    def _s(v) -> str:
        return v.decode() if isinstance(v, (bytes, bytearray)) else v

    def put(self, key: str, value: dict) -> None:
        self._r.set(f"{self.PREFIX}{key}", json.dumps(value), ex=self._ttl)

    def get_all(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for raw_key in self._r.scan_iter(match=f"{self.PREFIX}*"):
            k = self._s(raw_key)
            raw = self._r.get(k)
            if raw is None:
                continue
            out[k[len(self.PREFIX):]] = json.loads(self._s(raw))
        return out

    def delete(self, key: str) -> None:
        self._r.delete(f"{self.PREFIX}{key}")


def _http_poster(url: str, payload: dict, headers: dict) -> int:
    import httpx

    return httpx.post(url, json=payload, headers=headers, timeout=10.0).status_code


INTERNAL_SECRET_HEADER = "X-Internal-Secret"


def internal_callback_headers(env: Optional[dict] = None) -> dict:
    """The headers every RuntimeEvent callback carries. ``RUNTIME_CALLBACK_SECRET`` (a value only the
    runtime and meeting-api hold — C4 split; NOT the gateway identity marker or admin-api's internal
    key) rides as ``X-Internal-Secret`` so the receiver can tell the kernel's workload evidence from
    a forgery by anything else on the network (meeting-api fails closed on it outside DEV_MODE).
    Falls back to the legacy shared ``INTERNAL_API_SECRET`` when unset (an un-migrated deployment).
    Neither set ⇒ no header (a dev/lite stack with an open edge); never an empty header."""
    import os

    source = os.environ if env is None else env
    secret = (source.get("RUNTIME_CALLBACK_SECRET") or "").strip() or (
        source.get("INTERNAL_API_SECRET") or "").strip()
    return {INTERNAL_SECRET_HEADER: secret} if secret else {}


class CallbackQueue:
    def __init__(
        self,
        poster: Optional[Poster] = None,
        store: Optional[PendingStore] = None,
        max_attempts: int = 0,
    ) -> None:
        self.poster = poster or _http_poster
        self.store = store or InMemoryPendingStore()
        # 0 ⇒ retry forever (until acked or TTL expiry, matching 0.11's durable stance).
        self.max_attempts = max_attempts
        self._seq = 0

    def enqueue(self, url: str, event: dict, headers: Optional[dict] = None) -> str:
        self._seq += 1
        key = f"cb-{self._seq}"
        self.store.put(key, {"url": url, "headers": headers or {}, "event": event, "attempts": 0})
        # Best-effort immediate attempt; whatever doesn't ack stays queued for the sweep.
        self._attempt(key)
        return key

    def _attempt(self, key: str) -> bool:
        rec = self.store.get_all().get(key)
        if rec is None:
            return True
        rec["attempts"] = rec.get("attempts", 0) + 1
        try:
            code = self.poster(rec["url"], rec["event"], rec.get("headers") or {})
            if code < 400:
                self.store.delete(key)
                logger.info("callback %s delivered (attempt %d) -> %s", key, rec["attempts"], code)
                return True
            logger.warning("callback %s got %d (attempt %d)", key, code, rec["attempts"])
        except Exception as e:  # noqa: BLE001 — transport failures are retryable
            logger.warning("callback %s delivery failed (attempt %d): %s", key, rec["attempts"], e)

        # Not acked. Give up only if a finite cap is set and reached; else keep for retry.
        if self.max_attempts and rec["attempts"] >= self.max_attempts:
            logger.error("callback %s exhausted %d attempts; dropping", key, self.max_attempts)
            self.store.delete(key)
            return False
        self.store.put(key, rec)
        return False

    def sweep(self) -> int:
        """Retry every still-pending delivery once. Returns how many remain pending afterward."""
        for key in list(self.store.get_all()):
            self._attempt(key)
        return self.pending_count()

    def pending_count(self) -> int:
        return len(self.store.get_all())
