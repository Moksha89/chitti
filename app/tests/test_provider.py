import asyncio

import httpx
import pytest

from chitti.provider import (
    CODER_MAX_OUTPUT_TOKENS,
    FakeProvider,
    GatewayMisconfigurationError,
    GatewayTransientError,
    LiteLLMProvider,
    ModelToolCall,
    _diagnostic_message_fields,
)


class _Client:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, *_args, **_kwargs):
        if self.error:
            raise self.error
        return self.response


def test_gateway_preflight_rejects_missing_credential() -> None:
    with pytest.raises(GatewayMisconfigurationError, match="credential is missing"):
        asyncio.run(LiteLLMProvider("http://127.0.0.1:4000", "").validate_gateway())


def test_gateway_preflight_rejects_missing_route(monkeypatch) -> None:
    response = httpx.Response(200, json={"data": [{"id": "coder"}]})
    monkeypatch.setattr(
        "chitti.provider.httpx.AsyncClient", lambda **_kwargs: _Client(response=response)
    )

    with pytest.raises(GatewayMisconfigurationError, match="reviewer"):
        asyncio.run(LiteLLMProvider("http://127.0.0.1:4000", "configured").validate_gateway())


def test_gateway_preflight_distinguishes_transient_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        "chitti.provider.httpx.AsyncClient",
        lambda **_kwargs: _Client(error=httpx.ConnectTimeout("timed out")),
    )

    with pytest.raises(GatewayTransientError, match="did not respond"):
        asyncio.run(LiteLLMProvider("http://127.0.0.1:4000", "configured").validate_gateway())


def test_gateway_preflight_uses_models_endpoint_and_both_routes(monkeypatch) -> None:
    response = httpx.Response(
        200,
        json={"data": [{"id": "coder"}, {"id": "reviewer"}]},
    )
    calls = []

    class Client(_Client):
        async def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return response

    monkeypatch.setattr(
        "chitti.provider.httpx.AsyncClient", lambda **_kwargs: Client()
    )
    asyncio.run(LiteLLMProvider("http://127.0.0.1:4000", "configured").validate_gateway())

    assert calls == [
        (
            "http://127.0.0.1:4000/v1/models",
            {"headers": {"Authorization": "Bearer configured"}},
        )
    ]


def test_agent_completion_records_native_tool_calls_and_request_schema(monkeypatch) -> None:
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "http://127.0.0.1:4000/v1/chat/completions"),
        json={
            "model": "openai/glm-5.2",
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {
                        "content": "",
                        "reasoning_content": "hidden",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "list_files",
                                    "arguments": "{\"path\":\".\"}",
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": CODER_MAX_OUTPUT_TOKENS,
                "total_tokens": 8204,
                "completion_tokens_details": {"reasoning_tokens": 123},
            },
        },
    )
    calls = []

    class Client(_Client):
        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return response

    monkeypatch.setattr(
        "chitti.provider.httpx.AsyncClient", lambda **_kwargs: Client()
    )
    completion = asyncio.run(
        LiteLLMProvider("http://127.0.0.1:4000", "configured").agent_completion(
            [{"role": "user", "content": "work"}],
            "coder",
            tools=[{"type": "function", "function": {"name": "list_files"}}],
            tool_choice="required",
        )
    )

    assert completion.content == ""
    assert completion.finish_reason == "length"
    assert completion.message_fields == ("reasoning_content", "tool_calls")
    assert completion.tool_calls == (
        ModelToolCall(id="call-1", name="list_files", arguments={"path": "."}),
    )
    assert completion.reasoning_tokens == 123
    assert calls[0][1]["json"]["max_tokens"] == CODER_MAX_OUTPUT_TOKENS
    assert calls[0][1]["json"]["tools"][0]["function"]["name"] == "list_files"
    assert calls[0][1]["json"]["tool_choice"] == "required"
    assert "thinking" not in calls[0][1]["json"]


def test_coder_output_ceiling_leaves_reasoning_and_write_headroom() -> None:
    assert CODER_MAX_OUTPUT_TOKENS == 32768


def test_diagnostic_fields_ignore_structural_and_empty_message_values() -> None:
    assert _diagnostic_message_fields(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [],
            "refusal": None,
            "reasoning_content": "useful hidden text",
        }
    ) == ("reasoning_content",)


def test_fake_provider_emits_native_completion_tool_call() -> None:
    completion = asyncio.run(
        FakeProvider().agent_completion(
            [{"role": "user", "content": "work"}],
            "coder",
            tools=[{"type": "function"}],
            tool_choice="required",
        )
    )
    assert completion.content == ""
    assert completion.finish_reason == "tool_calls"
    assert completion.tool_calls == (
        ModelToolCall(
            id="fake-finish",
            name="finish",
            arguments={"summary": "Fake provider completed the task."},
        ),
    )
