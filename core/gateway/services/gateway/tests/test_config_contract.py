"""#526 · config.v1 (ADR-0026) — gateway's declaration + boot preflight.

The gateway attaches INTERNAL_API_SECRET as X-Internal-Secret on the admin-api /internal/validate
hop; unset, admin-api rejects validation and EVERY API-key check 503s — yet the gateway was outside
the config-contract machinery, so it booted green. This pins the declaration + the boot preflight
that refuses a secretless deploy. Offline, stdlib only.
"""
from __future__ import annotations

import pytest

from gateway import config_preflight as cp


def test_declaration_loads_and_split_secrets_are_declared():
    decl = cp.load_declaration()
    assert decl["service"] == "gateway"
    keys = {k["key"]: k for k in decl["keys"]}
    # C4 split: the admin-api key and the identity marker are separate; the legacy single secret is a
    # fallback, so the "one of them must be set" bar lives in the boot (adapters.build_production_app), not here.
    assert {"ADMIN_API_INTERNAL_SECRET", "GATEWAY_IDENTITY_SECRET", "INTERNAL_API_SECRET"} <= set(keys)
    assert keys["INTERNAL_API_SECRET"]["class"] == "defaulted"


def test_boot_refuses_without_any_admin_internal_key(monkeypatch):
    from gateway import adapters

    for k in ("ADMIN_API_INTERNAL_SECRET", "INTERNAL_API_SECRET"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(cp.ConfigError) as ei:
        adapters.build_production_app()
    assert "ADMIN_API_INTERNAL_SECRET" in str(ei.value)


def test_preflight_passes_when_required_set():
    cp.preflight({"ADMIN_API_INTERNAL_SECRET": "a-real-secret"})
    cp.preflight({"INTERNAL_API_SECRET": "a-real-secret"})
