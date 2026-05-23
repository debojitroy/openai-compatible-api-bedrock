import importlib
import json

from fastapi.testclient import TestClient

from tests.conftest import MODEL_ID


def test_models_endpoint_returns_configured_model(client, auth_headers):
    resp = client.get("/v1/models", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert [m["id"] for m in body["data"]] == [MODEL_ID]


def test_chat_completions_translates_request_and_response(client, fake_bedrock, auth_headers):
    fake_bedrock.converse.return_value = {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": "Hi there!"}],
            }
        },
        "usage": {"inputTokens": 11, "outputTokens": 4, "totalTokens": 15},
        "stopReason": "end_turn",
    }

    resp = client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "ignored-by-server",
            "messages": [
                {"role": "system", "content": "You are terse."},
                {"role": "user", "content": "hi"},
            ],
            "temperature": 0.2,
            "top_p": 0.9,
            "max_tokens": 100,
            "stop": ["END"],
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == MODEL_ID
    assert body["choices"][0]["message"] == {
        "role": "assistant",
        "content": "Hi there!",
        "name": None,
    }
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"] == {
        "prompt_tokens": 11,
        "completion_tokens": 4,
        "total_tokens": 15,
    }

    kwargs = fake_bedrock.converse.call_args.kwargs
    assert kwargs["modelId"] == MODEL_ID
    assert kwargs["system"] == [{"text": "You are terse."}]
    assert kwargs["messages"] == [
        {"role": "user", "content": [{"text": "hi"}]},
    ]
    inf = kwargs["inferenceConfig"]
    assert inf["temperature"] == 0.2
    assert inf["topP"] == 0.9
    assert inf["maxTokens"] == 100
    assert inf["stopSequences"] == ["END"]


def test_finish_reason_max_tokens_maps_to_length(client, fake_bedrock, auth_headers):
    fake_bedrock.converse.return_value = {
        "output": {"message": {"role": "assistant", "content": [{"text": "..."}]}},
        "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
        "stopReason": "max_tokens",
    }
    resp = client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["finish_reason"] == "length"


def test_content_as_list_of_text_parts_is_flattened(client, fake_bedrock, auth_headers):
    """Warp/newer OpenAI SDKs send content as [{"type":"text","text":"..."}]."""
    fake_bedrock.converse.return_value = {
        "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
        "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
        "stopReason": "end_turn",
    }
    resp = client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "x",
            "messages": [
                {"role": "system", "content": [{"type": "text", "text": "be terse"}]},
                {"role": "user", "content": [
                    {"type": "text", "text": "hello "},
                    {"type": "text", "text": "world"},
                ]},
            ],
        },
    )
    assert resp.status_code == 200
    kwargs = fake_bedrock.converse.call_args.kwargs
    assert kwargs["system"] == [{"text": "be terse"}]
    assert kwargs["messages"] == [
        {"role": "user", "content": [{"text": "hello world"}]}
    ]


def test_assistant_messages_are_passed_as_assistant_role(client, fake_bedrock, auth_headers):
    fake_bedrock.converse.return_value = {
        "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
        "usage": {"inputTokens": 5, "outputTokens": 1, "totalTokens": 6},
        "stopReason": "end_turn",
    }
    resp = client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "x",
            "messages": [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "prev reply"},
                {"role": "user", "content": "again"},
            ],
        },
    )
    assert resp.status_code == 200
    kwargs = fake_bedrock.converse.call_args.kwargs
    assert kwargs["system"] == []
    assert kwargs["messages"] == [
        {"role": "user", "content": [{"text": "first"}]},
        {"role": "assistant", "content": [{"text": "prev reply"}]},
        {"role": "user", "content": [{"text": "again"}]},
    ]


def _parse_sse(body: str):
    chunks = []
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: "):]
        if payload == "[DONE]":
            chunks.append("[DONE]")
        else:
            chunks.append(json.loads(payload))
    return chunks


def test_streaming_chat_yields_openai_sse(client, fake_bedrock, auth_headers):
    fake_bedrock.converse_stream.return_value = {
        "stream": iter([
            {"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"delta": {"text": "Hello"}, "contentBlockIndex": 0}},
            {"contentBlockDelta": {"delta": {"text": " world"}, "contentBlockIndex": 0}},
            {"contentBlockStop": {"contentBlockIndex": 0}},
            {"messageStop": {"stopReason": "end_turn"}},
            {"metadata": {"usage": {"inputTokens": 2, "outputTokens": 2, "totalTokens": 4}}},
        ]),
    }

    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "x",
            "messages": [{"role": "user", "content": "say hi"}],
            "stream": True,
        },
    ) as r:
        assert r.status_code == 200
        body = "".join(r.iter_text())

    chunks = _parse_sse(body)
    assert chunks[-1] == "[DONE]"

    text_deltas = [
        ch["choices"][0]["delta"].get("content")
        for ch in chunks
        if isinstance(ch, dict) and ch["choices"][0]["delta"].get("content")
    ]
    assert text_deltas == ["Hello", " world"]

    finals = [
        ch for ch in chunks
        if isinstance(ch, dict) and ch["choices"][0].get("finish_reason")
    ]
    assert finals and finals[-1]["choices"][0]["finish_reason"] == "stop"

    kwargs = fake_bedrock.converse_stream.call_args.kwargs
    assert kwargs["modelId"] == MODEL_ID
    assert kwargs["messages"] == [{"role": "user", "content": [{"text": "say hi"}]}]


def test_missing_model_id_returns_500(monkeypatch, fake_secrets, auth_headers):
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    import app.main as main_mod
    importlib.reload(main_mod)
    c = TestClient(main_mod.app)
    resp = c.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 500
    assert "BEDROCK_MODEL_ID" in resp.text
