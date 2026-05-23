import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple, Union

import boto3
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app.auth import require_tenant

logger = logging.getLogger("bedrock_api")
logger.setLevel(logging.INFO)

app = FastAPI(title="OpenAI Compatible API (Bedrock)")


@app.exception_handler(RequestValidationError)
async def _log_422(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Logs the offending payload so we can see what unfamiliar clients (e.g. Warp)
    # actually send when they trip Pydantic. Body is bytes; decode best-effort.
    body = await request.body()
    try:
        body_preview = body.decode("utf-8")
    except UnicodeDecodeError:
        body_preview = repr(body[:1024])
    logger.warning(
        "422 on %s %s tenant=%s errors=%s body=%s",
        request.method,
        request.url.path,
        getattr(request.state, "tenant_id", None),
        json.dumps(exc.errors(), default=str),
        body_preview,
    )
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

MODEL_ID: Optional[str] = os.environ.get("BEDROCK_MODEL_ID")
AWS_REGION: Optional[str] = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")

_bedrock: Any = None  # populated lazily; tests may monkeypatch directly


def _get_client() -> Any:
    global _bedrock
    if _bedrock is None:
        kwargs = {"region_name": AWS_REGION} if AWS_REGION else {}
        _bedrock = boto3.client("bedrock-runtime", **kwargs)
    return _bedrock


# ---- OpenAI-shaped schemas ---------------------------------------------------

class ChatMessage(BaseModel):
    role: str
    content: str
    name: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    n: Optional[int] = 1
    stream: Optional[bool] = False
    stop: Optional[Union[str, List[str]]] = None
    max_tokens: Optional[int] = None
    user: Optional[str] = None


class CompletionRequest(BaseModel):
    model: str
    prompt: Union[str, List[str]]
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    n: Optional[int] = 1
    stream: Optional[bool] = False
    stop: Optional[Union[str, List[str]]] = None
    max_tokens: Optional[int] = None
    user: Optional[str] = None


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponseChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: Optional[str] = None


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatCompletionResponseChoice]
    usage: Usage


class CompletionResponseChoice(BaseModel):
    text: str
    index: int
    logprobs: Optional[Dict[str, Any]] = None
    finish_reason: Optional[str] = None


class CompletionResponse(BaseModel):
    id: str
    object: str = "text_completion"
    created: int
    model: str
    choices: List[CompletionResponseChoice]
    usage: Usage


# ---- Translation helpers -----------------------------------------------------

_FINISH_REASON_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "content_filtered": "content_filter",
    "guardrail_intervened": "content_filter",
}


def _require_model_id() -> str:
    if not MODEL_ID:
        raise HTTPException(
            status_code=500,
            detail="BEDROCK_MODEL_ID is not configured. Set the env var before starting the server.",
        )
    return MODEL_ID


def _to_bedrock_messages(
    messages: List[ChatMessage],
) -> Tuple[List[Dict[str, str]], List[Dict[str, Any]]]:
    """Split OpenAI messages into Bedrock (system_blocks, messages)."""
    system_blocks: List[Dict[str, str]] = []
    bedrock_messages: List[Dict[str, Any]] = []
    for m in messages:
        if m.role == "system":
            system_blocks.append({"text": m.content})
            continue
        role = m.role if m.role in ("user", "assistant") else "user"
        bedrock_messages.append({"role": role, "content": [{"text": m.content}]})
    return system_blocks, bedrock_messages


def _build_inference_config(req: ChatCompletionRequest) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {}
    if req.temperature is not None:
        cfg["temperature"] = req.temperature
    if req.top_p is not None:
        cfg["topP"] = req.top_p
    if req.max_tokens is not None:
        cfg["maxTokens"] = req.max_tokens
    if req.stop is not None:
        cfg["stopSequences"] = [req.stop] if isinstance(req.stop, str) else list(req.stop)
    return cfg


def _map_finish_reason(stop_reason: Optional[str]) -> str:
    if not stop_reason:
        return "stop"
    return _FINISH_REASON_MAP.get(stop_reason, "stop")


def _extract_text(content_blocks: List[Dict[str, Any]]) -> str:
    return "".join(block.get("text", "") for block in content_blocks if "text" in block)


# ---- Routes ------------------------------------------------------------------

@app.post("/v1/chat/completions")
async def create_chat_completion(
    request: ChatCompletionRequest,
    tenant_id: str = Depends(require_tenant),
):
    model_id = _require_model_id()
    system, messages = _to_bedrock_messages(request.messages)
    inference_config = _build_inference_config(request)

    if request.stream:
        return StreamingResponse(
            _stream_chat(model_id, system, messages, inference_config),
            media_type="text/event-stream",
        )

    try:
        resp = await asyncio.to_thread(
            _get_client().converse,
            modelId=model_id,
            system=system,
            messages=messages,
            inferenceConfig=inference_config,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Bedrock error: {e}") from e

    text = _extract_text(resp["output"]["message"].get("content", []))
    usage = resp.get("usage", {})
    finish_reason = _map_finish_reason(resp.get("stopReason"))

    return ChatCompletionResponse(
        id=f"chatcmpl-{int(time.time() * 1000)}",
        created=int(time.time()),
        model=model_id,
        choices=[
            ChatCompletionResponseChoice(
                index=0,
                message=ChatMessage(role="assistant", content=text),
                finish_reason=finish_reason,
            )
        ],
        usage=Usage(
            prompt_tokens=usage.get("inputTokens", 0),
            completion_tokens=usage.get("outputTokens", 0),
            total_tokens=usage.get("totalTokens", 0),
        ),
    )


async def _stream_chat(
    model_id: str,
    system: List[Dict[str, str]],
    messages: List[Dict[str, Any]],
    inference_config: Dict[str, Any],
):
    stream_id = f"chatcmpl-{int(time.time() * 1000)}"
    created = int(time.time())

    def envelope(delta: Dict[str, Any], finish_reason: Optional[str] = None) -> str:
        payload = {
            "id": stream_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_id,
            "choices": [
                {"index": 0, "delta": delta, "finish_reason": finish_reason}
            ],
        }
        return f"data: {json.dumps(payload)}\n\n"

    try:
        resp = await asyncio.to_thread(
            _get_client().converse_stream,
            modelId=model_id,
            system=system,
            messages=messages,
            inferenceConfig=inference_config,
        )
    except Exception as e:
        err = {"error": {"message": f"Bedrock error: {e}", "type": "bedrock_error"}}
        yield f"data: {json.dumps(err)}\n\n"
        yield "data: [DONE]\n\n"
        return

    yield envelope({"role": "assistant"})

    stream = resp["stream"]
    stream_iter = iter(stream)
    final_reason: Optional[str] = None
    sentinel = object()

    def _next_event():
        try:
            return next(stream_iter)
        except StopIteration:
            return sentinel

    try:
        while True:
            event = await asyncio.to_thread(_next_event)
            if event is sentinel:
                break
            if "contentBlockDelta" in event:
                delta_text = event["contentBlockDelta"]["delta"].get("text")
                if delta_text:
                    yield envelope({"content": delta_text})
            elif "messageStop" in event:
                final_reason = _map_finish_reason(event["messageStop"].get("stopReason"))
    except asyncio.CancelledError:
        try:
            stream.close()
        except Exception:
            pass
        raise
    except Exception as e:
        err = {"error": {"message": f"Bedrock stream error: {e}", "type": "bedrock_error"}}
        yield f"data: {json.dumps(err)}\n\n"
        yield "data: [DONE]\n\n"
        return

    yield envelope({}, finish_reason=final_reason or "stop")
    yield "data: [DONE]\n\n"


@app.post("/v1/completions")
async def create_completion(
    request: CompletionRequest,
    tenant_id: str = Depends(require_tenant),
):
    """Legacy completions endpoint — routed through the same chat path."""
    model_id = _require_model_id()
    prompt = request.prompt if isinstance(request.prompt, str) else "\n".join(request.prompt)
    chat_req = ChatCompletionRequest(
        model=request.model,
        messages=[ChatMessage(role="user", content=prompt)],
        temperature=request.temperature,
        top_p=request.top_p,
        max_tokens=request.max_tokens,
        stop=request.stop,
        stream=request.stream,
    )
    if request.stream:
        return await create_chat_completion(chat_req, tenant_id=tenant_id)

    chat_resp: ChatCompletionResponse = await create_chat_completion(chat_req, tenant_id=tenant_id)
    return CompletionResponse(
        id=f"cmpl-{int(time.time() * 1000)}",
        created=int(time.time()),
        model=model_id,
        choices=[
            CompletionResponseChoice(
                text=chat_resp.choices[0].message.content,
                index=0,
                finish_reason=chat_resp.choices[0].finish_reason,
            )
        ],
        usage=chat_resp.usage,
    )


@app.get("/v1/models")
async def list_models(tenant_id: str = Depends(require_tenant)):
    model_id = MODEL_ID or ""
    return {
        "object": "list",
        "data": [
            {
                "id": model_id,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "bedrock",
            }
        ],
    }
