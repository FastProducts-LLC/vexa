"""Shared test env for the runtime kernel suite.

Security finding C5 made the runtime FAIL CLOSED: the HTTP API needs ``RUNTIME_API_TOKEN`` and every
request-declared mount source must be on the server-side ``RUNTIME_MOUNT_SOURCE_ALLOWLIST``. The
pre-existing lifecycle/mount tests exercise behaviour, not policy, so by default they run with the
explicit insecure opt-out and an allowlist covering the operator-style sources they use.
``test_security_c5.py`` clears/overrides both to test the policy itself."""
import pytest


@pytest.fixture(autouse=True)
def _c5_default_policy(monkeypatch):
    monkeypatch.delenv("RUNTIME_API_TOKEN", raising=False)
    monkeypatch.setenv("RUNTIME_API_INSECURE_NO_AUTH", "1")
    monkeypatch.setenv(
        "RUNTIME_MOUNT_SOURCE_ALLOWLIST",
        "agent-workspaces,vexa-agent-workspaces,/srv/vexa/workspaces,/srv/vexa-global,/srv/g,/shared-store,/x",
    )
