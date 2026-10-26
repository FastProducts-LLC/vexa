"""C5: agent-api's runtime clients carry the runtime control-plane bearer (VEXA_RUNTIME_API_TOKEN) on
every runtime.v1 / schedule.v1 call — and the job-fire headers stay separate from it."""
from __future__ import annotations

import io
import json

import pytest

from control_plane import admin_panel
from shared import adapters
from shared.adapters import RuntimeHttpClient, SchedulerHttpClient


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def captured(monkeypatch):
    seen: list = []

    def fake_urlopen(req, timeout=None):
        seen.append(req)
        if req.full_url.endswith("/workloads") and req.get_method() == "GET":
            return _Resp(b"[]")
        return _Resp(json.dumps({"workloadId": "agent-x", "state": "running", "id": "j1"}).encode())

    monkeypatch.setattr(adapters.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(admin_panel.urllib.request, "urlopen", fake_urlopen)
    return seen


def test_runtime_client_sends_bearer(captured):
    rt = RuntimeHttpClient("http://runtime:8090", auth_token="tok")
    rt.spawn("agent-x", "agent", {})
    rt.await_done("agent-x")
    assert [r.get_header("Authorization") for r in captured] == ["Bearer tok", "Bearer tok"]


def test_scheduler_client_sends_bearer_and_keeps_fire_headers_separate(captured):
    sc = SchedulerHttpClient("http://runtime:8090", auth_token="tok",
                             request_headers={"X-Internal-Secret": "isec"})
    sc.schedule({"request": {"url": "http://agent-api:8100/invocations"}, "cron": "* * * * *"})
    sc.list_jobs()
    sc.cancel_job("j1")
    assert all(r.get_header("Authorization") == "Bearer tok" for r in captured)
    body = json.loads(captured[0].data)
    # the job's own fire request carries the internal secret, NOT the runtime bearer
    assert body["request"]["headers"] == {"X-Internal-Secret": "isec"}


def test_admin_panel_listing_sends_bearer(captured):
    assert admin_panel.fetch_workloads("http://runtime:8090", auth_token="tok") == []
    assert captured[0].get_header("Authorization") == "Bearer tok"


def test_no_token_sends_no_header(captured):
    RuntimeHttpClient("http://runtime:8090").spawn("agent-x", "agent", {})
    assert captured[0].get_header("Authorization") is None
