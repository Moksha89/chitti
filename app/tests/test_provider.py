import asyncio

import httpx
import pytest

from chitti.provider import (
    GatewayMisconfigurationError,
    GatewayTransientError,
    LiteLLMProvider,
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
