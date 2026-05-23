import importlib
import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient


MODEL_ID = "anthropic.claude-3-5-sonnet-20240620-v1:0"
SECRET_ID = "bedrock-api/tenant-keys"
TENANT_KEYS = {"acme": "sk-acme-123", "globex": "sk-globex-456"}
DEFAULT_KEY = TENANT_KEYS["acme"]


@pytest.fixture
def app_module(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", MODEL_ID)
    monkeypatch.setenv("TENANT_KEYS_SECRET_ID", SECRET_ID)
    import app.auth as auth_mod
    importlib.reload(auth_mod)
    import app.main as main_mod
    importlib.reload(main_mod)
    return main_mod


@pytest.fixture
def fake_secrets(app_module, monkeypatch):
    fake = MagicMock()
    fake.get_secret_value.return_value = {
        "SecretString": json.dumps(TENANT_KEYS),
    }
    import app.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_secrets_client", fake)
    return fake


@pytest.fixture
def fake_bedrock(app_module, monkeypatch):
    fake = MagicMock()
    monkeypatch.setattr(app_module, "_bedrock", fake)
    return fake


@pytest.fixture
def client(app_module, fake_secrets, fake_bedrock):
    return TestClient(app_module.app)


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {DEFAULT_KEY}"}
