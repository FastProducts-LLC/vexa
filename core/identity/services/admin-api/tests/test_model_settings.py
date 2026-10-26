"""Settings → Models (identity side) — /user/models + /user/transcription self-serve, the
platform_settings internal CRUD, and the resolution edges dispatch/bot_spawn consume.

Secrets (api_key, transcription token) are masked on every user-facing read and cross in the
clear ONLY over the X-Internal-Secret edges (`/internal/users/{id}/model-config`, bot-context).
Effective config resolves FIELD-BY-FIELD user > platform; env fallback stays downstream.

Same testcontainers-PG harness as O-STACK-3 (skips without docker).
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from admin_api.app import db as app_db
from admin_api.app.main import create_app
from admin_api.schema.models import Base
from admin_api.schema.sync import ensure_schema_sync

from conftest import requires_docker
from test_stack_admin_api import ADMIN_TOKEN, INTERNAL_SECRET, _admin, _dispose_async_engine

pytestmark = requires_docker


@pytest.fixture()
def client(pg_url, pg_async_url, monkeypatch):
    sync_engine = create_engine(pg_url)
    Base.metadata.drop_all(sync_engine)
    ensure_schema_sync(sync_engine, Base)
    sync_engine.dispose()
    monkeypatch.setenv("ADMIN_API_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("INTERNAL_API_SECRET", INTERNAL_SECRET)
    monkeypatch.setenv("DEV_MODE", "false")
    app_db.configure(pg_async_url)
    with TestClient(create_app()) as c:
        yield c
    _dispose_async_engine()


def _internal():
    return {"X-Internal-Secret": INTERNAL_SECRET}


def _user_token(client, email="models@vexa.ai"):
    uid = client.post("/admin/users", headers=_admin(), json={"email": email}).json()["id"]
    tok = client.post(f"/admin/users/{uid}/tokens?scopes=bot", headers=_admin()).json()["token"]
    return uid, tok


def test_user_models_set_masked_readback_and_clear(client):
    _uid, tok = _user_token(client)
    h = {"X-API-Key": tok}

    r = client.put("/user/models", headers=h, json={
        "mode": "custom", "model": "qwen3-coder", "base_url": "https://llm.example.com/v1",
        "api_key": "sk-secret-1234abcd",
    })
    assert r.status_code == 200, r.text
    cfg = r.json()
    assert cfg["mode"] == "custom"
    assert cfg["model"] == "qwen3-coder"
    assert cfg["api_key_set"] is True
    # masked: the secret NEVER echoes in the clear
    assert "sk-secret" not in (cfg["api_key"] or "")
    assert cfg["api_key"].endswith("abcd")

    # partial update leaves other fields; empty string clears one
    r = client.put("/user/models", headers=h, json={"model": ""})
    cfg = r.json()
    assert cfg["model"] is None
    assert cfg["mode"] == "custom"          # untouched
    assert cfg["api_key_set"] is True       # untouched


def test_user_models_validation(client):
    _uid, tok = _user_token(client, email="val@vexa.ai")
    h = {"X-API-Key": tok}
    assert client.put("/user/models", headers=h, json={"mode": "yolo"}).status_code == 422
    assert client.put("/user/models", headers=h, json={"base_url": "not-a-url"}).status_code == 422
    assert client.put("/user/transcription", headers=h, json={"url": "ftp://x"}).status_code == 422


def test_platform_settings_crud_and_gate(client):
    # internal edge only — no/wrong secret fails closed
    assert client.get("/internal/settings/models").status_code == 403
    assert client.put("/internal/settings/models",
                      headers={"X-Internal-Secret": "wrong"}, json={}).status_code == 403
    # unknown key 404s
    assert client.get("/internal/settings/nope", headers=_internal()).status_code == 404

    r = client.put("/internal/settings/models", headers=_internal(),
                   json={"model": "haiku", "mode": "subscription"})
    assert r.status_code == 200, r.text
    assert r.json()["value"] == {"model": "haiku", "mode": "subscription"}
    # partial update + clear
    r = client.put("/internal/settings/models", headers=_internal(), json={"mode": ""})
    assert r.json()["value"] == {"model": "haiku"}
    assert client.get("/internal/settings/models", headers=_internal()).json()["value"] == {"model": "haiku"}
    # same field rules as the user tier
    assert client.put("/internal/settings/models", headers=_internal(),
                      json={"mode": "yolo"}).status_code == 422


def test_model_config_resolves_user_over_platform(client):
    uid, tok = _user_token(client, email="resolve@vexa.ai")
    client.put("/internal/settings/models", headers=_internal(),
               json={"model": "global-model", "meeting_model": "global-meeting",
                     "base_url": "https://global.example.com"})
    client.put("/user/models", headers={"X-API-Key": tok},
               json={"model": "my-model", "api_key": "sk-user-key"})

    r = client.get(f"/internal/users/{uid}/model-config", headers=_internal())
    assert r.status_code == 200, r.text
    models = r.json()["models"]
    assert models["model"] == "my-model"                       # user beats platform
    assert models["meeting_model"] == "global-meeting"         # platform fills the gap
    assert models["base_url"] == "https://global.example.com"  # field-by-field, not all-or-nothing
    assert models["api_key"] == "sk-user-key"                  # the secret crosses ONLY here

    # unknown subject → 404 (dispatch treats it as env defaults)
    assert client.get("/internal/users/999999/model-config", headers=_internal()).status_code == 404


def test_bot_context_carries_effective_transcription(client):
    uid, tok = _user_token(client, email="stt@vexa.ai")
    # nothing configured → no transcription key at all (bot_spawn keeps its env)
    r = client.get(f"/internal/users/{uid}/bot-context", headers=_internal())
    assert "transcription" not in r.json()

    client.put("/internal/settings/transcription", headers=_internal(),
               json={"url": "https://stt-global.example.com", "token": "tok-global"})
    r = client.get(f"/internal/users/{uid}/bot-context", headers=_internal())
    assert r.json()["transcription"] == {"url": "https://stt-global.example.com", "token": "tok-global"}

    client.put("/user/transcription", headers={"X-API-Key": tok},
               json={"url": "https://stt-mine.example.com"})
    r = client.get(f"/internal/users/{uid}/bot-context", headers=_internal())
    # user url wins; global token still fills the gap (field-by-field)
    assert r.json()["transcription"] == {"url": "https://stt-mine.example.com", "token": "tok-global"}

    # masked user-facing read-back
    cfg = client.get("/user/transcription", headers={"X-API-Key": tok}).json()
    assert cfg["url"] == "https://stt-mine.example.com"
    assert cfg["token_set"] is False


# ── C1 (2026-10-25): a user-supplied endpoint and its credential are a PAIR ───────────────────
# Before the fix a user could store {"mode":"custom","base_url":"https://attacker.example"} with
# no api_key; the resolver then paired that base_url with the platform's api_key (and dispatch
# with the deployment's env key) and the worker posted it to the attacker.

def test_user_base_url_requires_users_own_api_key(client):
    _uid, tok = _user_token(client, email="pair@vexa.ai")
    h = {"X-API-Key": tok}
    r = client.put("/user/models", headers=h, json={"mode": "custom", "base_url": "https://attacker.example"})
    assert r.status_code == 422, r.text
    assert "api_key" in r.json()["detail"]
    # mode=custom without an endpoint is a dead setting too
    assert client.put("/user/models", headers=h, json={"mode": "custom"}).status_code == 422
    # the pair is accepted together …
    r = client.put("/user/models", headers=h, json={"mode": "custom", "base_url": "https://gw.example.com",
                                                    "api_key": "sk-user"})
    assert r.status_code == 200, r.text
    # … and the key cannot be cleared while the endpoint stays
    assert client.put("/user/models", headers=h, json={"api_key": ""}).status_code == 422
    # clearing both is fine
    assert client.put("/user/models", headers=h, json={"api_key": "", "base_url": "", "mode": ""}).status_code == 200
    # a key alone (BYO key to the operator's gateway) is fine
    assert client.put("/user/models", headers=h, json={"api_key": "sk-only"}).status_code == 200


def test_user_base_url_rejects_private_and_internal_hosts(client, monkeypatch):
    monkeypatch.delenv("VEXA_MODEL_ALLOW_PRIVATE_ENDPOINTS", raising=False)
    _uid, tok = _user_token(client, email="ssrf@vexa.ai")
    h = {"X-API-Key": tok}
    for url in ("http://admin-api:8001", "http://127.0.0.1:8090", "http://10.1.2.3", "http://localhost:4000",
                "http://169.254.169.254/latest", "https://user:pw@gw.example.com", "http://[::1]:1"):
        r = client.put("/user/models", headers=h, json={"mode": "custom", "base_url": url, "api_key": "k"})
        assert r.status_code == 422, (url, r.text)
    monkeypatch.setenv("VEXA_MODEL_ALLOW_PRIVATE_ENDPOINTS", "1")
    r = client.put("/user/models", headers=h, json={"mode": "custom", "base_url": "http://litellm:4000", "api_key": "k"})
    assert r.status_code == 200, r.text


def test_user_base_url_honours_host_allowlist(client, monkeypatch):
    monkeypatch.setenv("VEXA_MODEL_BASE_URL_ALLOWLIST", "gw.example.com, .openrouter.ai")
    _uid, tok = _user_token(client, email="allow@vexa.ai")
    h = {"X-API-Key": tok}
    assert client.put("/user/models", headers=h, json={"mode": "custom", "base_url": "https://attacker.example",
                                                       "api_key": "k"}).status_code == 422
    assert client.put("/user/models", headers=h, json={"mode": "custom", "base_url": "https://api.openrouter.ai/v1",
                                                       "api_key": "k"}).status_code == 200
    assert client.put("/user/models", headers=h, json={"base_url": "https://gw.example.com"}).status_code == 200
    assert client.put("/user/models", headers=h, json={"base_url": "https://evilopenrouter.ai"}).status_code == 422


def test_platform_tier_may_set_an_operator_endpoint_without_a_key(client):
    """The operator's gateway is the operator's business: a private host and no stored key are both
    allowed at the platform tier (dispatch pairs it with the deployment's own credentials)."""
    r = client.put("/internal/settings/models", headers=_internal(),
                   json={"mode": "custom", "base_url": "http://litellm:4000"})
    assert r.status_code == 200, r.text


def test_model_config_never_pairs_user_base_url_with_platform_key(client, pg_url):
    uid, tok = _user_token(client, email="nopair@vexa.ai")
    client.put("/internal/settings/models", headers=_internal(),
               json={"mode": "custom", "base_url": "https://global.example.com", "api_key": "sk-platform"})
    # The write-time pairing check refuses a keyless user base_url outright …
    r = client.put("/user/models", headers={"X-API-Key": tok}, json={"base_url": "https://attacker.example"})
    assert r.status_code == 422
    # … and even for a stored legacy row (pre-fix data) the resolver never lends the platform key.
    import json as _json
    from sqlalchemy import text as sa_text
    from admin_api.schema.models import User

    eng = create_engine(pg_url)
    with eng.begin() as conn:
        conn.execute(sa_text(f"UPDATE {User.__tablename__} SET data = CAST(:d AS jsonb) WHERE id = :id"),
                     {"d": _json.dumps({"model_prefs": {"base_url": "https://attacker.example"}}), "id": uid})
    eng.dispose()

    models = client.get(f"/internal/users/{uid}/model-config", headers=_internal()).json()["models"]
    assert models["base_url"] == "https://attacker.example"
    assert "api_key" not in models                      # platform key NOT lent to the user's host
    assert models["endpoint_source"] == "user"          # dispatch applies the strict rule
    assert models["mode"] == "custom"                   # mode still falls through field-by-field


def test_model_config_marks_platform_endpoint_and_user_key_pairing(client):
    uid, tok = _user_token(client, email="src@vexa.ai")
    client.put("/internal/settings/models", headers=_internal(),
               json={"mode": "custom", "base_url": "https://global.example.com", "api_key": "sk-platform"})
    models = client.get(f"/internal/users/{uid}/model-config", headers=_internal()).json()["models"]
    assert models["endpoint_source"] == "platform" and models["api_key"] == "sk-platform"
    # a user's own key to the operator's endpoint is fine (their key, the operator's gateway)
    client.put("/user/models", headers={"X-API-Key": tok}, json={"api_key": "sk-user"})
    models = client.get(f"/internal/users/{uid}/model-config", headers=_internal()).json()["models"]
    assert models["endpoint_source"] == "platform" and models["api_key"] == "sk-user"
    # a user's own endpoint + key: user source, user key only
    client.put("/user/models", headers={"X-API-Key": tok}, json={"base_url": "https://mine.example.com"})
    models = client.get(f"/internal/users/{uid}/model-config", headers=_internal()).json()["models"]
    assert models["endpoint_source"] == "user" and models["api_key"] == "sk-user"
    assert models["base_url"] == "https://mine.example.com"
    # no endpoint anywhere → no source field
    client.put("/internal/settings/models", headers=_internal(), json={"base_url": "", "api_key": "", "mode": ""})
    client.put("/user/models", headers={"X-API-Key": tok}, json={"base_url": "", "api_key": ""})
    models = client.get(f"/internal/users/{uid}/model-config", headers=_internal()).json()["models"]
    assert "endpoint_source" not in models
