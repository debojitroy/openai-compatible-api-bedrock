import importlib
import json

import pytest
from fastapi.testclient import TestClient

from tests.conftest import MODEL_ID, TENANT_KEYS


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def test_chat_without_auth_returns_401(client):
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 401


def test_chat_with_invalid_key_returns_401(client):
    resp = client.post(
        "/v1/chat/completions",
        headers=_auth("not-a-real-key"),
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 401


def test_chat_with_valid_key_succeeds(client, fake_bedrock):
    fake_bedrock.converse.return_value = {
        "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
        "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
        "stopReason": "end_turn",
    }
    resp = client.post(
        "/v1/chat/completions",
        headers=_auth(TENANT_KEYS["acme"]),
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200, resp.text
    fake_bedrock.converse.assert_called_once()


def test_models_endpoint_requires_auth(client):
    resp = client.get("/v1/models")
    assert resp.status_code == 401
    resp = client.get("/v1/models", headers=_auth(TENANT_KEYS["globex"]))
    assert resp.status_code == 200


def test_secrets_manager_called_only_once_within_ttl(client, fake_secrets):
    for _ in range(3):
        r = client.get("/v1/models", headers=_auth(TENANT_KEYS["acme"]))
        assert r.status_code == 200
    assert fake_secrets.get_secret_value.call_count == 1


def test_missing_secret_id_returns_500(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", MODEL_ID)
    monkeypatch.delenv("TENANT_KEYS_SECRET_ID", raising=False)
    import app.auth as auth_mod
    importlib.reload(auth_mod)
    import app.main as main_mod
    importlib.reload(main_mod)
    c = TestClient(main_mod.app)
    resp = c.get("/v1/models", headers={"Authorization": "Bearer anything"})
    assert resp.status_code == 500
    assert "TENANT_KEYS_SECRET_ID" in resp.text
