"""DeepSeek client tests using httpx.MockTransport.

Coverage:
  - Happy path (2xx) parses the response and computes cost.
  - 429 is retried (3 attempts) and then succeeds.
  - 5xx is retried; final failure raises DeepSeekError.
  - 4xx (non-429) is NOT retried — raises immediately.
  - Transport errors (timeout / connect) are retried.
  - Cost calculation per model.
  - Bearer token is sent in the Authorization header.
  - The API key is never embedded in log messages (sanity check).
"""
from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import pytest

from app.analyzer.deepseek_client import (
    DEFAULT_COST_PER_1K,
    DeepSeekClient,
    DeepSeekError,
)


# A response body shape DeepSeek returns. The real model is the same.
def _ok_response(model: str = "deepseek-chat", prompt_tokens: int = 100, completion_tokens: int = 50) -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1700000000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "event_type": "OTHER",
                            "summary": "x",
                            "sentiment": "neutral",
                            "sentiment_score": 0.0,
                            "confidence": 0.5,
                            "recommendation": "HOLD",
                            "reasoning": "x",
                            "key_numbers": {},
                        }
                    ),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _make_handler(
    responses: list[httpx.Response | Exception],
) -> Callable[[httpx.Request], httpx.Response]:
    """Build a handler that pops from `responses` on each call.

    If the next item is an `Exception`, raise it. Otherwise return it.
    """
    queue = list(responses)

    def handler(req: httpx.Request) -> httpx.Response:
        if not queue:
            raise AssertionError("handler called more times than expected")
        nxt = queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    return handler


def _client(handler) -> DeepSeekClient:
    return DeepSeekClient(
        api_key="sk-test",
        timeout_s=5.0,
        max_retries=3,
        transport_factory=lambda: httpx.MockTransport(handler),
    )


# -- Happy path ----------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_happy_path():
    handler = _make_handler(
        [httpx.Response(200, json=_ok_response(prompt_tokens=1000, completion_tokens=200))]
    )
    client = _client(handler)
    try:
        r = await client.complete(
            system="s", user="u", model="deepseek-chat", max_tokens=200
        )
        assert r.model == "deepseek-chat"
        assert r.prompt_tokens == 1000
        assert r.completion_tokens == 200
        assert r.total_tokens == 1200
        # cost = 1000 * 0.00014/1000 + 200 * 0.00028/1000 = 0.00014 + 0.000056 = 0.000196
        assert abs(r.cost_usd - 0.000196) < 1e-9
        # content is the JSON-string from the response.
        parsed = json.loads(r.content)
        assert parsed["event_type"] == "OTHER"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_complete_sends_authorization_header():
    """The Bearer token must be present on the request."""
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json=_ok_response())

    client = _client(handler)
    try:
        await client.complete(system="s", user="u", model="deepseek-chat")
    finally:
        await client.aclose()
    assert len(captured) == 1
    auth = captured[0].headers.get("authorization", "")
    assert auth == "Bearer sk-test"
    # The body should include response_format=json_object.
    body = json.loads(captured[0].content)
    assert body["response_format"] == {"type": "json_object"}
    # Model + temperature + max_tokens propagated.
    assert body["model"] == "deepseek-chat"
    assert body["messages"] == [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
    ]


# -- Retry behaviour -----------------------------------------------------


@pytest.mark.asyncio
async def test_retry_on_429_then_success():
    """Two 429s, then 200 — must succeed and call the handler 3 times."""
    handler = _make_handler(
        [
            httpx.Response(429, json={"error": "rate"}),
            httpx.Response(429, json={"error": "rate"}),
            httpx.Response(200, json=_ok_response(prompt_tokens=10, completion_tokens=5)),
        ]
    )
    client = _client(handler)
    try:
        r = await client.complete(system="s", user="u", model="deepseek-chat")
        assert r.prompt_tokens == 10
        assert r.completion_tokens == 5
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_retry_on_5xx_then_success():
    handler = _make_handler(
        [
            httpx.Response(500, text="server error"),
            httpx.Response(502, text="bad gateway"),
            httpx.Response(200, json=_ok_response()),
        ]
    )
    client = _client(handler)
    try:
        r = await client.complete(system="s", user="u", model="deepseek-chat")
        assert r.model == "deepseek-chat"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_no_retry_on_400():
    """4xx (non-429) must NOT be retried — raise immediately."""
    handler = _make_handler([httpx.Response(400, text="bad request")])
    client = _client(handler)
    try:
        with pytest.raises(DeepSeekError) as exc_info:
            await client.complete(system="s", user="u", model="deepseek-chat")
        assert exc_info.value.status_code == 400
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_no_retry_on_401():
    handler = _make_handler([httpx.Response(401, text="unauthorized")])
    client = _client(handler)
    try:
        with pytest.raises(DeepSeekError) as exc_info:
            await client.complete(system="s", user="u", model="deepseek-chat")
        assert exc_info.value.status_code == 401
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_retry_exhausted_raises():
    """3 retries beyond the initial = 4 calls, all 429, raise at end."""
    handler = _make_handler(
        [httpx.Response(429, json={"error": "rate"})] * 10
    )
    client = _client(handler)
    try:
        with pytest.raises(DeepSeekError) as exc_info:
            await client.complete(system="s", user="u", model="deepseek-chat")
        assert exc_info.value.status_code == 429
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_transport_error_is_retried():
    """A transport error (e.g. timeout) is retried the same as 5xx."""
    handler = _make_handler(
        [
            httpx.ConnectError("connection refused"),
            httpx.ConnectError("connection refused"),
            httpx.Response(200, json=_ok_response()),
        ]
    )
    client = _client(handler)
    try:
        r = await client.complete(system="s", user="u", model="deepseek-chat")
        assert r.model == "deepseek-chat"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_transport_error_exhausted_raises():
    handler = _make_handler(
        [httpx.ConnectError("nope")] * 10
    )
    client = _client(handler)
    try:
        with pytest.raises(DeepSeekError):
            await client.complete(system="s", user="u", model="deepseek-chat")
    finally:
        await client.aclose()


# -- Cost calculation ---------------------------------------------------


def test_estimate_cost_usd_default_model():
    c = DeepSeekClient(api_key="x")
    # 1000 prompt + 500 completion on deepseek-chat:
    # 1000 * 0.00014 / 1000 = 0.00014
    # 500  * 0.00028 / 1000 = 0.00014
    cost = c.estimate_cost_usd("deepseek-chat", prompt_tokens=1000, completion_tokens=500)
    assert abs(cost - 0.00028) < 1e-9


def test_estimate_cost_usd_unknown_model_zero():
    c = DeepSeekClient(api_key="x")
    assert c.estimate_cost_usd("totally-made-up-model", 1, 1) == 0.0


def test_default_cost_table_has_both_models():
    assert "deepseek-chat" in DEFAULT_COST_PER_1K
    assert "deepseek-reasoner" in DEFAULT_COST_PER_1K


# -- Misc ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_api_key_raises():
    client = DeepSeekClient(api_key="", transport_factory=lambda: httpx.MockTransport(_make_handler([httpx.Response(200, json=_ok_response())])))
    try:
        with pytest.raises(DeepSeekError) as exc_info:
            await client.complete(system="s", user="u", model="deepseek-chat")
        assert "DEEPSEEK_API_KEY" in str(exc_info.value)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_response_missing_choices_raises():
    handler = _make_handler(
        [httpx.Response(200, json={"id": "x", "model": "deepseek-chat", "usage": {}})]
    )
    client = _client(handler)
    try:
        with pytest.raises(DeepSeekError):
            await client.complete(system="s", user="u", model="deepseek-chat")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_non_json_response_raises():
    handler = _make_handler(
        [httpx.Response(200, text="not json", headers={"content-type": "text/plain"})]
    )
    client = _client(handler)
    try:
        with pytest.raises(DeepSeekError):
            await client.complete(system="s", user="u", model="deepseek-chat")
    finally:
        await client.aclose()
