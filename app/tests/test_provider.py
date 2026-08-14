import asyncio
from pathlib import Path

import httpx
import pytest

from chitti.provider import (
    CODER_MAX_OUTPUT_TOKENS,
    MODEL_CLIENT_TIMEOUT_SECONDS,
    MODEL_GATEWAY_TIMEOUT_SECONDS,
    REVIEWER_MAX_OUTPUT_TOKENS,
    VISION_ROUTE,
    FakeProvider,
    GatewayMisconfigurationError,
    GatewayTransientError,
    LiteLLMProvider,
    ModelToolCall,
    ModelTransportError,
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

    async def post(self, *_args, **_kwargs):
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
        request=httpx.Request("POST", "http://127.0.0.1:4000/v1/chat/completions"),
        json={
            "data": [
                {"id": "chitti-chat"},
                {"id": "planner"},
                {"id": "coder"},
                {"id": "reviewer"},
                {"id": "bulk"},
                {"id": VISION_ROUTE},
            ],
            "choices": [{"message": {"content": "OK"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )
    calls = []

    class Client(_Client):
        async def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return response

        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return response

    monkeypatch.setattr(
        "chitti.provider.httpx.AsyncClient", lambda **_kwargs: Client()
    )
    asyncio.run(
        LiteLLMProvider("http://127.0.0.1:4000", "configured").validate_gateway(
            probe_routes=True
        )
    )

    assert calls[0] == (
        "http://127.0.0.1:4000/v1/models",
        {"headers": {"Authorization": "Bearer configured"}},
    )
    assert [call[1]["json"]["model"] for call in calls[1:]] == [
        "bulk",
        "coder",
        "reviewer",
        "vision",
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


def test_agent_completion_timeout_is_bounded_above_gateway_timeout(monkeypatch) -> None:
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "http://127.0.0.1:4000/v1/chat/completions"),
        json={
            "choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )
    client_timeouts = []

    class Client(_Client):
        async def __aenter__(self):
            return self

        async def post(self, *_args, **_kwargs):
            return response

    def client_factory(**kwargs):
        client_timeouts.append(kwargs["timeout"])
        return Client()

    monkeypatch.setattr("chitti.provider.httpx.AsyncClient", client_factory)
    asyncio.run(
        LiteLLMProvider("http://127.0.0.1:4000", "configured").agent_completion(
            [{"role": "user", "content": "work"}], "coder"
        )
    )

    assert MODEL_GATEWAY_TIMEOUT_SECONDS == 600
    assert MODEL_CLIENT_TIMEOUT_SECONDS == 660
    assert client_timeouts == [MODEL_CLIENT_TIMEOUT_SECONDS]


def test_agent_completion_distinguishes_transport_timeout(monkeypatch) -> None:
    class Client(_Client):
        async def post(self, *_args, **_kwargs):
            raise httpx.ReadTimeout("upstream stalled")

    monkeypatch.setattr("chitti.provider.httpx.AsyncClient", lambda **_kwargs: Client())

    with pytest.raises(ModelTransportError, match="gateway request timed out"):
        asyncio.run(
            LiteLLMProvider("http://127.0.0.1:4000", "configured").agent_completion(
                [{"role": "user", "content": "work"}], "coder"
            )
        )


def test_gateway_config_has_bounded_single_attempt_timeout() -> None:
    config = Path(__file__).parents[2] / "litellm" / "config.yaml"
    text = config.read_text()
    assert "request_timeout: 600" in text
    assert "num_retries: 0" in text


def test_reviewer_request_uses_headroom_below_provider_ceiling(monkeypatch) -> None:
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "http://127.0.0.1:4000/v1/chat/completions"),
        json={
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": '{"verdict":"pass"}'},
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
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
    asyncio.run(
        LiteLLMProvider("http://127.0.0.1:4000", "configured").agent_completion(
            [{"role": "user", "content": "diagnose"}], "reviewer"
        )
    )

    assert REVIEWER_MAX_OUTPUT_TOKENS == 4096
    assert REVIEWER_MAX_OUTPUT_TOKENS < 8192
    assert calls[0][1]["json"]["max_tokens"] == REVIEWER_MAX_OUTPUT_TOKENS


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


def test_vision_completion_missing_cost_fails_closed(monkeypatch) -> None:
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "http://127.0.0.1:4000/v1/chat/completions"),
        json={
            "model": "openai/glm-4.6v-flashx",
            "choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        },
    )
    monkeypatch.setattr(
        "chitti.provider.httpx.AsyncClient",
        lambda **_kwargs: _Client(response=response),
    )
    with pytest.raises(ModelTransportError, match="usable model cost"):
        asyncio.run(
            LiteLLMProvider("http://127.0.0.1:4000", "configured").agent_completion(
                [{"role": "user", "content": "inspect"}],
                VISION_ROUTE,
            )
        )


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
