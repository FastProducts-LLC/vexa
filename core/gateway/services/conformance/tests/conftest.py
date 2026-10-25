"""Conformance-wide env: the gateway and the REAL in-process meeting-api share one internal-tier
secret, exactly as in a deployment. meeting-api honors ``x-user-*`` only with the gateway's
``X-Gateway-Verified`` marker (== ``INTERNAL_API_SECRET``), so every proxied REST assertion here also
proves the gateway stamps it on meeting-api forwards (unset → meeting-api fails closed with 503).
Session-scoped + set before any module fixture builds the gateway (it reads the secret at create time).
"""
from __future__ import annotations

import os

import pytest

CONFORMANCE_INTERNAL_SECRET = "conformance-internal-secret"


@pytest.fixture(scope="session", autouse=True)
def _shared_internal_secret():
    saved = {k: os.environ.get(k) for k in ("INTERNAL_API_SECRET", "DEV_MODE")}
    os.environ["INTERNAL_API_SECRET"] = CONFORMANCE_INTERNAL_SECRET
    os.environ["DEV_MODE"] = "false"
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
