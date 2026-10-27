"""C4 — every RuntimeEvent callback the kernel posts carries ``X-Internal-Secret`` =
``INTERNAL_API_SECRET`` so the receiver (meeting-api ``/runtime/callback``, fail-closed outside
DEV_MODE) can tell the kernel's workload evidence from a forgery by anything else on the network.

  * the header is stamped from the kernel's env at delivery time (not captured at import);
  * unset/blank secret ⇒ NO header (never an empty one);
  * the header rides every attempt, including sweep retries (the durable-queue path).
"""
from fastapi.testclient import TestClient

from runtime_kernel import CallbackQueue, Runtime
from runtime_kernel.api import create_app
from runtime_kernel.callbacks import INTERNAL_SECRET_HEADER, internal_callback_headers


def test_headers_from_env():
    assert internal_callback_headers({"INTERNAL_API_SECRET": "s3cret"}) == {INTERNAL_SECRET_HEADER: "s3cret"}
    assert internal_callback_headers({"INTERNAL_API_SECRET": "  s3cret \n"}) == {INTERNAL_SECRET_HEADER: "s3cret"}
    assert internal_callback_headers({"INTERNAL_API_SECRET": ""}) == {}
    assert internal_callback_headers({"INTERNAL_API_SECRET": "   "}) == {}


def test_runtime_callback_secret_wins_over_the_legacy_shared_secret():
    """C4 split: the kernel stamps ITS callback secret, never the shared/gateway value, once set."""
    env = {"RUNTIME_CALLBACK_SECRET": "rcs", "INTERNAL_API_SECRET": "legacy"}
    assert internal_callback_headers(env) == {INTERNAL_SECRET_HEADER: "rcs"}
    assert internal_callback_headers({"RUNTIME_CALLBACK_SECRET": "rcs"}) == {INTERNAL_SECRET_HEADER: "rcs"}
    assert internal_callback_headers({"RUNTIME_CALLBACK_SECRET": " ", "INTERNAL_API_SECRET": "legacy"}) == {
        INTERNAL_SECRET_HEADER: "legacy"}
    assert internal_callback_headers({}) == {}


def test_api_callbacks_carry_the_secret_on_every_attempt(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", "kernel-secret")
    seen: list[tuple[str, dict, dict]] = []
    codes = iter([500, 200, 200, 200, 200, 200])

    def poster(url, payload, headers):
        seen.append((url, payload, dict(headers)))
        return next(codes, 200)

    queue = CallbackQueue(poster=poster)
    rt = Runtime(profiles={"test": ["sleep", "30"]}, grace_sec=2.0)
    client = TestClient(create_app(rt, callback_queue=queue))
    r = client.post(
        "/workloads",
        json={"workloadId": "w1", "profile": "test", "env": {}, "callbackUrl": "http://receiver/runtime/callback"},
    )
    assert r.status_code == 201
    for _ in range(10):
        if queue.sweep() == 0:
            break
    client.post("/workloads/w1/stop")
    assert len(seen) >= 2  # the 500'd first attempt + its retry at minimum
    for url, payload, headers in seen:
        assert url == "http://receiver/runtime/callback"
        assert payload["workloadId"] == "w1"
        assert headers.get(INTERNAL_SECRET_HEADER) == "kernel-secret", headers


def test_api_callbacks_omit_the_header_when_unset(monkeypatch):
    monkeypatch.delenv("INTERNAL_API_SECRET", raising=False)
    monkeypatch.delenv("RUNTIME_CALLBACK_SECRET", raising=False)
    seen: list[dict] = []

    def poster(url, payload, headers):
        seen.append(dict(headers))
        return 200

    queue = CallbackQueue(poster=poster)
    rt = Runtime(profiles={"test": ["sleep", "30"]}, grace_sec=2.0)
    client = TestClient(create_app(rt, callback_queue=queue))
    client.post("/workloads", json={"workloadId": "w2", "profile": "test", "env": {}, "callbackUrl": "http://r/cb"})
    client.post("/workloads/w2/stop")
    assert seen
    assert all(INTERNAL_SECRET_HEADER not in h for h in seen)
