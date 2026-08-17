import asyncio
from pathlib import Path

import httpx
import pytest

from chitti.provider import (
    CODER_MAX_OUTPUT_TOKENS,
    MODEL_CALL_MAX_ATTEMPTS,
    MODEL_CALL_MAX_RETRY_AFTER_SECONDS,
    MODEL_CLIENT_TIMEOUT_SECONDS,
    MODEL_GATEWAY_TIMEOUT_SECONDS,
    REVIEWER_MAX_OUTPUT_TOKENS,
    VISION_MAX_OUTPUT_TOKENS,
    VISION_ROUTE,
    FakeProvider,
    GatewayMisconfigurationError,
    GatewayTransientError,
    LiteLLMProvider,
    ModelCostConfigurationError,
    ModelProviderError,
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

    assert MODEL_GATEWAY_TIMEOUT_SECONDS == 120
    assert MODEL_CLIENT_TIMEOUT_SECONDS == 150
    assert client_timeouts == [MODEL_CLIENT_TIMEOUT_SECONDS]


def test_agent_completion_distinguishes_transport_timeout(monkeypatch) -> None:
    class Client(_Client):
        async def post(self, *_args, **_kwargs):
            raise httpx.ReadTimeout("upstream stalled")

    monkeypatch.setattr("chitti.provider.httpx.AsyncClient", lambda **_kwargs: Client())

    with pytest.raises(ModelProviderError, match="retries exhausted") as raised:
        asyncio.run(
            LiteLLMProvider("http://127.0.0.1:4000", "configured").agent_completion(
                [{"role": "user", "content": "work"}], "coder"
            )
        )
    assert raised.value.failure_class == "transport failure"
    assert raised.value.attempts == MODEL_CALL_MAX_ATTEMPTS


def test_agent_completion_retries_5xx_then_succeeds(monkeypatch) -> None:
    responses = [
        httpx.Response(
            503,
            request=httpx.Request("POST", "http://gateway"),
            json={
                "error": "upstream overload",
                "usage": {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10},
                "cost": 0.004,
            },
        ),
        httpx.Response(
            200,
            request=httpx.Request("POST", "http://gateway"),
            json={
                "model": "coder",
                "choices": [{"message": {"content": "done"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            },
        ),
    ]
    calls = 0

    class Client(_Client):
        async def post(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            return responses.pop(0)

    monkeypatch.setattr("chitti.provider.httpx.AsyncClient", lambda **_kwargs: Client())
    async def no_sleep(*_args) -> None:
        return None

    monkeypatch.setattr("chitti.provider.asyncio.sleep", no_sleep)
    completion = asyncio.run(
        LiteLLMProvider("http://127.0.0.1:4000", "configured").agent_completion(
            [{"role": "user", "content": "work"}], "coder"
        )
    )

    assert calls == 2
    assert completion.attempts == 2
    assert completion.retry_failures == ("http 5xx",)
    assert completion.total_tokens == 15
    assert completion.retry_total_tokens == 10
    assert completion.retry_cost_usd == 0.004


def test_agent_completion_retries_truncated_body_then_succeeds(monkeypatch) -> None:
    responses = [
        httpx.Response(
            200,
            request=httpx.Request("POST", "http://gateway"),
            content=b'{"choices":[{"message":{"content":"truncated',
        ),
        httpx.Response(
            200,
            request=httpx.Request("POST", "http://gateway"),
            json={
                "choices": [{"message": {"content": "done"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        ),
    ]

    class Client(_Client):
        async def post(self, *_args, **_kwargs):
            return responses.pop(0)

    monkeypatch.setattr("chitti.provider.httpx.AsyncClient", lambda **_kwargs: Client())
    async def no_sleep(*_args) -> None:
        return None

    monkeypatch.setattr("chitti.provider.asyncio.sleep", no_sleep)
    completion = asyncio.run(
        LiteLLMProvider("http://127.0.0.1:4000", "configured").agent_completion(
            [{"role": "user", "content": "work"}], "coder"
        )
    )

    assert completion.attempts == 2
    assert completion.retry_failures == ("malformed response",)


def test_malformed_response_retains_bounded_diagnostics(monkeypatch) -> None:
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "http://gateway"),
        content=(
            b'{"choices":[{"finish_reason":"tool_calls","message":{"tool_calls":'
            b'[{"function":{"name":"write_file","arguments":"not-json"}}]}}],'
            b'"usage":{"prompt_tokens":2,"completion_tokens":3,"total_tokens":5},'
            b'"debug":"secret=should-not-be-retained"}'
        ),
    )

    class Client(_Client):
        async def post(self, *_args, **_kwargs):
            return response

    monkeypatch.setattr("chitti.provider.httpx.AsyncClient", lambda **_kwargs: Client())
    with pytest.raises(ModelProviderError) as raised:
        asyncio.run(
            LiteLLMProvider("http://127.0.0.1:4000", "configured").agent_completion(
                [{"role": "user", "content": "work"}],
                "coder",
                tools=[{"type": "function", "function": {"name": "write_file"}}],
                tool_choice="required",
            )
        )

    assert raised.value.failure_class == "malformed response"
    assert raised.value.response_diagnostics[0].startswith(
        "response_exception=ValueError:"
    )
    assert "response_finish_reason=tool_calls" in raised.value.response_diagnostics
    assert "not-json" in raised.value.response_diagnostics[-1]
    assert "should-not-be-retained" not in raised.value.response_diagnostics[-1]
    assert len(raised.value.response_diagnostics[-1]) <= 2060


def test_output_ceiling_malformed_tool_call_continues_without_retry(monkeypatch) -> None:
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "http://gateway"),
        json={
            "model": "glm-5.2",
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "write_file",
                                    "arguments": "not-json",
                                }
                            }
                        ],
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": CODER_MAX_OUTPUT_TOKENS,
                "total_tokens": CODER_MAX_OUTPUT_TOKENS + 2,
            },
        },
    )
    calls = 0

    class Client(_Client):
        async def post(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            return response

    monkeypatch.setattr("chitti.provider.httpx.AsyncClient", lambda **_kwargs: Client())
    completion = asyncio.run(
        LiteLLMProvider("http://127.0.0.1:4000", "configured").agent_completion(
            [{"role": "user", "content": "work"}],
            "coder",
            tools=[{"type": "function", "function": {"name": "write_file"}}],
            tool_choice="required",
        )
    )

    assert calls == 1
    assert completion.finish_reason == "length"
    assert completion.tool_calls == ()
    assert completion.message_fields == ("response_failure_class=output limit",)
    assert "response_exception=ValueError" in completion.response_diagnostics[0]


def test_vision_output_ceiling_has_rubric_headroom(monkeypatch) -> None:
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "http://gateway"),
        json={
            "model": "glm-4.6v-flashx",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": '{"verdict":"pass"}'},
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        },
    )
    calls = []

    class Client(_Client):
        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return response

    monkeypatch.setattr("chitti.provider.httpx.AsyncClient", lambda **_kwargs: Client())
    asyncio.run(
        LiteLLMProvider("http://127.0.0.1:4000", "configured").agent_completion(
            [{"role": "user", "content": "inspect"}],
            VISION_ROUTE,
        )
    )

    assert VISION_MAX_OUTPUT_TOKENS == 4096
    assert calls[0][1]["json"]["max_tokens"] == VISION_MAX_OUTPUT_TOKENS


def test_agent_completion_retries_408(monkeypatch) -> None:
    calls = 0

    class Client(_Client):
        async def post(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(
                    408, request=httpx.Request("POST", "http://gateway")
                )
            return httpx.Response(
                200,
                request=httpx.Request("POST", "http://gateway"),
                json={
                    "choices": [{"message": {"content": "done"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

    monkeypatch.setattr("chitti.provider.httpx.AsyncClient", lambda **_kwargs: Client())
    async def no_sleep(*_args) -> None:
        return None

    monkeypatch.setattr("chitti.provider.asyncio.sleep", no_sleep)
    completion = asyncio.run(
        LiteLLMProvider("http://127.0.0.1:4000", "configured").agent_completion(
            [{"role": "user", "content": "work"}], "coder"
        )
    )
    assert calls == 2
    assert completion.retry_failures == ("http 408",)


def test_agent_completion_retries_429_and_honors_retry_after(monkeypatch) -> None:
    calls = 0
    delays = []

    class Client(_Client):
        async def post(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(
                    429,
                    request=httpx.Request("POST", "http://gateway"),
                    headers={"Retry-After": "7"},
                )
            return httpx.Response(
                200,
                request=httpx.Request("POST", "http://gateway"),
                json={
                    "choices": [{"message": {"content": "done"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

    monkeypatch.setattr("chitti.provider.httpx.AsyncClient", lambda **_kwargs: Client())

    async def record_sleep(delay) -> None:
        delays.append(delay)

    monkeypatch.setattr("chitti.provider.asyncio.sleep", record_sleep)
    asyncio.run(
        LiteLLMProvider("http://127.0.0.1:4000", "configured").agent_completion(
            [{"role": "user", "content": "work"}], "coder"
        )
    )
    assert calls == 2
    assert delays == [7.0]


def test_agent_completion_refuses_retry_after_above_bound(monkeypatch) -> None:
    calls = 0
    requested_delay = MODEL_CALL_MAX_RETRY_AFTER_SECONDS + 1

    class Client(_Client):
        async def post(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            return httpx.Response(
                429,
                request=httpx.Request("POST", "http://gateway"),
                headers={"Retry-After": str(requested_delay)},
            )

    monkeypatch.setattr("chitti.provider.httpx.AsyncClient", lambda **_kwargs: Client())
    with pytest.raises(ModelProviderError, match="exceeding Chitti's"):
        asyncio.run(
            LiteLLMProvider("http://127.0.0.1:4000", "configured").agent_completion(
                [{"role": "user", "content": "work"}], "coder"
            )
        )
    assert calls == 1


def test_agent_completion_does_not_retry_other_4xx(monkeypatch) -> None:
    calls = 0

    class Client(_Client):
        async def post(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            return httpx.Response(
                400, request=httpx.Request("POST", "http://gateway")
            )

    monkeypatch.setattr("chitti.provider.httpx.AsyncClient", lambda **_kwargs: Client())
    with pytest.raises(Exception, match="HTTP 400"):
        asyncio.run(
            LiteLLMProvider("http://127.0.0.1:4000", "configured").agent_completion(
                [{"role": "user", "content": "work"}], "coder"
            )
        )
    assert calls == 1


def test_agent_completion_preserves_mixed_failure_evidence_and_usage(monkeypatch) -> None:
    responses = [
        httpx.Response(
            503,
            request=httpx.Request("POST", "http://gateway"),
            json={
                "usage": {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10},
                "cost": 0.004,
            },
        ),
        httpx.Response(
            408, request=httpx.Request("POST", "http://gateway")
        ),
        httpx.Response(
            400, request=httpx.Request("POST", "http://gateway")
        ),
    ]

    class Client(_Client):
        async def post(self, *_args, **_kwargs):
            return responses.pop(0)

    monkeypatch.setattr("chitti.provider.httpx.AsyncClient", lambda **_kwargs: Client())

    async def no_sleep(*_args) -> None:
        return None

    monkeypatch.setattr("chitti.provider.asyncio.sleep", no_sleep)
    with pytest.raises(ModelProviderError, match="request failed") as raised:
        asyncio.run(
            LiteLLMProvider("http://127.0.0.1:4000", "configured").agent_completion(
                [{"role": "user", "content": "work"}], "coder"
            )
        )
    error = raised.value
    assert error.failure_class == "http 4xx"
    assert error.attempts == 3
    assert error.retry_failures == ("http 5xx", "http 408", "http 4xx")
    assert error.prompt_tokens == 4
    assert error.completion_tokens == 6
    assert error.total_tokens == 10
    assert error.cost_usd == 0.004


def test_agent_completion_does_not_retry_policy_refusal(monkeypatch) -> None:
    calls = 0

    class Client(_Client):
        async def post(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            return httpx.Response(
                200,
                request=httpx.Request("POST", "http://gateway"),
                json={
                    "choices": [
                        {"message": {"content": "", "refusal": "policy refusal"}}
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

    monkeypatch.setattr("chitti.provider.httpx.AsyncClient", lambda **_kwargs: Client())
    with pytest.raises(Exception, match="content-policy refusal"):
        asyncio.run(
            LiteLLMProvider("http://127.0.0.1:4000", "configured").agent_completion(
                [{"role": "user", "content": "work"}], "coder"
            )
        )
    assert calls == 1


def test_agent_completion_does_not_retry_budget_refusal(monkeypatch) -> None:
    calls = 0

    class Client(_Client):
        async def post(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            return httpx.Response(
                200,
                request=httpx.Request("POST", "http://gateway"),
                json={
                    "error": {
                        "type": "budget_exceeded",
                        "message": "model budget limit reached",
                    }
                },
            )

    monkeypatch.setattr("chitti.provider.httpx.AsyncClient", lambda **_kwargs: Client())
    with pytest.raises(Exception, match="budget or limit refusal"):
        asyncio.run(
            LiteLLMProvider("http://127.0.0.1:4000", "configured").agent_completion(
                [{"role": "user", "content": "work"}], "coder"
            )
        )
    assert calls == 1


def test_agent_completion_exhausted_retries_name_class_and_attempts(monkeypatch) -> None:
    class Client(_Client):
        async def post(self, *_args, **_kwargs):
            return httpx.Response(503, request=httpx.Request("POST", "http://gateway"))

    monkeypatch.setattr("chitti.provider.httpx.AsyncClient", lambda **_kwargs: Client())
    async def no_sleep(*_args) -> None:
        return None

    monkeypatch.setattr("chitti.provider.asyncio.sleep", no_sleep)
    with pytest.raises(Exception, match="retries exhausted") as raised:
        asyncio.run(
            LiteLLMProvider("http://127.0.0.1:4000", "configured").agent_completion(
                [{"role": "user", "content": "work"}], "coder"
            )
        )
    error = raised.value
    assert error.failure_class == "http 5xx"
    assert error.attempts == MODEL_CALL_MAX_ATTEMPTS
    assert error.retry_failures == ("http 5xx",) * MODEL_CALL_MAX_ATTEMPTS


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
    with pytest.raises(ModelCostConfigurationError, match="usable model cost"):
        asyncio.run(
            LiteLLMProvider("http://127.0.0.1:4000", "configured").agent_completion(
                [{"role": "user", "content": "inspect"}],
                VISION_ROUTE,
            )
        )


def test_invalid_model_cost_fails_once_without_retry(monkeypatch) -> None:
    calls = 0
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "http://127.0.0.1:4000/v1/chat/completions"),
        json={
            "choices": [{"message": {"content": "done"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "cost": "not-a-number",
        },
    )

    class Client(_Client):
        async def post(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            return response

    monkeypatch.setattr("chitti.provider.httpx.AsyncClient", lambda **_kwargs: Client())
    with pytest.raises(ModelCostConfigurationError, match="invalid model cost"):
        asyncio.run(
            LiteLLMProvider("http://127.0.0.1:4000", "configured").agent_completion(
                [{"role": "user", "content": "work"}], "coder"
            )
        )
    assert calls == 1


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
