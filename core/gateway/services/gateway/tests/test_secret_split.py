"""C4 follow-up (deep audit #2): the gateway holds two DIFFERENT secrets and never mixes them up.

  * ``ADMIN_API_INTERNAL_SECRET`` → ``X-Internal-Secret`` on admin-api ``/internal/validate`` only;
  * ``GATEWAY_IDENTITY_SECRET``   → ``x-gateway-verified`` on agent-api forwards only (main).

The legacy single ``INTERNAL_API_SECRET`` is only a fallback for an un-migrated deployment.
"""
import pytest
from fastapi.testclient import TestClient

from gateway import create_app
from gateway.adapters import AdminApiAuthorizer
from conftest import VALID_KEY, FakeAuthorizer, FakeDownstream, FakeRedis

ADMIN = "admin-internal-secret"
IDENTITY = "gateway-identity-secret"
LEGACY = "legacy-internal-secret"


class _Resp:
    def __init__(self, status_code, body):
        self.status_code, self._body = status_code, body

    def json(self):
        return self._body


class _Client:
    def __init__(self):
        self.calls = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": dict(headers or {})})
        if url.endswith("/internal/validate"):
            return _Resp(200, {"user_id": 7, "scopes": ["bot"], "max_concurrent": 3})
        return _Resp(200, {"authorized": [], "errors": []})


@pytest.fixture
def split(monkeypatch):
    monkeypatch.setenv("ADMIN_API_INTERNAL_SECRET", ADMIN)
    monkeypatch.setenv("GATEWAY_IDENTITY_SECRET", IDENTITY)
    monkeypatch.setenv("INTERNAL_API_SECRET", LEGACY)


async def test_admin_validate_carries_only_the_admin_internal_secret(split):
    c = _Client()
    await AdminApiAuthorizer(c, "http://admin-api", "http://meeting-api").resolve("k")
    (hop,) = c.calls
    assert hop["url"] == "http://admin-api/internal/validate"
    assert hop["headers"]["X-Internal-Secret"] == ADMIN
    assert IDENTITY not in hop["headers"].values() and LEGACY not in hop["headers"].values()
    assert "x-gateway-verified" not in hop["headers"]


async def test_meeting_api_hop_carries_no_internal_secret(split):
    """main: the /ws authorize-subscribe hop to meeting-api carries neither secret."""
    c = _Client()
    await AdminApiAuthorizer(c, "http://admin-api", "http://meeting-api").authorize_subscribe("k", [])
    hop = c.calls[-1]
    assert hop["url"] == "http://meeting-api/ws/authorize-subscribe"
    for secret in (ADMIN, IDENTITY, LEGACY):
        assert secret not in hop["headers"].values()


async def test_legacy_single_secret_still_works_when_split_unset(monkeypatch):
    monkeypatch.delenv("ADMIN_API_INTERNAL_SECRET", raising=False)
    monkeypatch.delenv("GATEWAY_IDENTITY_SECRET", raising=False)
    monkeypatch.setenv("INTERNAL_API_SECRET", LEGACY)
    c = _Client()
    await AdminApiAuthorizer(c, "http://admin-api", "http://meeting-api").resolve("k")
    assert c.calls[0]["headers"]["X-Internal-Secret"] == LEGACY


def test_forwards_stamp_the_identity_secret_not_the_admin_key(split):
    downstream = FakeDownstream(status_code=200, body={"ok": True})
    app = create_app(FakeAuthorizer(), downstream, FakeRedis(), agent_api_url="http://agent-api",
                     meeting_api_url="http://meeting-api")
    client = TestClient(app)
    client.get("/agent/sessions", headers={"x-api-key": VALID_KEY})
    assert downstream.last["headers"]["x-gateway-verified"] == IDENTITY
    assert ADMIN not in downstream.last["headers"].values()
