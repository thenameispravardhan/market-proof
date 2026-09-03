"""LLM provider switch: DeepSeek (default) vs the fine-tuned SLM.

The branch that matters is `Service._llm_client` — everything downstream
(prompt, schema, rules, risk) is identical for both providers.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.analyzer.service import Service


def _settings(**kw):
    base = dict(
        LLM_PROVIDER="deepseek",
        LLM_SLM_ENDPOINT="",
        LLM_SLM_MODEL="tradebot-slm-v1",
        LLM_SLM_API_KEY="",
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_default_provider_is_deepseek():
    svc = Service()
    client, model = await svc._llm_client(_settings())
    assert client is svc._deepseek
    assert model is None          # template.model wins
    await svc.aclose()


@pytest.mark.asyncio
async def test_slm_routes_to_its_own_endpoint():
    svc = Service()
    client, model = await svc._llm_client(
        _settings(LLM_PROVIDER="slm", LLM_SLM_ENDPOINT="http://box:8000/v1/chat/completions")
    )
    assert client is not svc._deepseek
    assert client._endpoint == "http://box:8000/v1/chat/completions"
    assert model == "tradebot-slm-v1"
    assert client._api_key                      # never empty; blank key -> "-"
    await svc.aclose()


@pytest.mark.asyncio
async def test_slm_without_endpoint_falls_back_instead_of_blocking():
    """The toggle may degrade, never block a signal."""
    svc = Service()
    client, model = await svc._llm_client(_settings(LLM_PROVIDER="slm"))
    assert client is svc._deepseek
    assert model is None
    await svc.aclose()


@pytest.mark.asyncio
async def test_endpoint_change_rebuilds_the_client():
    svc = Service()
    first, _ = await svc._llm_client(
        _settings(LLM_PROVIDER="slm", LLM_SLM_ENDPOINT="http://a/v1/chat/completions")
    )
    second, _ = await svc._llm_client(
        _settings(LLM_PROVIDER="slm", LLM_SLM_ENDPOINT="http://b/v1/chat/completions")
    )
    assert second is not first
    assert second._endpoint == "http://b/v1/chat/completions"
    await svc.aclose()
