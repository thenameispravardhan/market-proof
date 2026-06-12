"""Async DeepSeek chat-completions client.

Endpoint: https://api.deepseek.com/v1/chat/completions

Design choices:

- async httpx with a configurable transport so tests can inject a
  `MockTransport` (see `test_deepseek_client.py`).
- 30s default timeout, 3 retries on HTTP 429/5xx with exponential
  backoff (1s, 2s, 4s). 4xx other than 429 is *not* retried — a
  malformed request won't fix itself.
- Per-call structured log: model, prompt_tokens, completion_tokens,
  latency_ms, cost_usd. The API key and prompt body are never logged.
- Cost calculation is a configurable dict (USD per 1k tokens) — the
  default reflects DeepSeek's public pricing for `deepseek-chat` at
  the time of writing; operators can override via `cost_per_1k_tokens`.

The `DeepSeekError` exception type lets `service.py` distinguish a
"DeepSeek failed" path from a "validation failed" path.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import httpx

from app.config import get_settings
from app.logging_config import get_logger

log = get_logger(__name__)


# ---- Pricing ------------------------------------------------------------

# USD per 1k tokens (prompt, completion) for the default model.
# Defaults reflect DeepSeek's published pricing for `deepseek-chat`
# (input $0.14 / 1M tokens, output $0.28 / 1M tokens → $0.00014 and
# $0.00028 per 1k). Operators can override at construction time.
DEFAULT_COST_PER_1K = {
    "deepseek-chat": {"prompt": 0.00014, "completion": 0.00028},
    "deepseek-reasoner": {"prompt": 0.00055, "completion": 0.00219},
}


# ---- Public types -------------------------------------------------------


@dataclass(frozen=True)
class DeepSeekResult:
    """Successful DeepSeek response, normalised."""

    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: float
    cost_usd: float
    raw: dict[str, Any] = field(default_factory=dict)


class DeepSeekError(Exception):
    """Raised on transport / non-2xx / parse failures.

    `status_code` is None for transport errors; otherwise the HTTP
    status from the DeepSeek response.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        body: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


# ---- Transport protocol -------------------------------------------------

# An httpx AsyncTransport (or anything compatible). Exposed so tests
# can pass `httpx.MockTransport(handler)` without monkeypatching.
TransportFactory = Callable[[], httpx.AsyncBaseTransport]


def _default_transport_factory() -> httpx.AsyncBaseTransport:
    return httpx.AsyncHTTPTransport()


# ---- Client -------------------------------------------------------------


class DeepSeekClient:
    """Thin async wrapper over the DeepSeek chat-completions API.

    Usage:
        client = DeepSeekClient()
        result = await client.complete(
            system="...",
            user="...",
            model="deepseek-chat",
            temperature=0.2,
            max_tokens=2000,
        )

    The client owns an httpx.AsyncClient and an httpx transport. Pass
    a custom `transport` (factory) to inject a mock for testing.
    """

    ENDPOINT = "https://api.deepseek.com/v1/chat/completions"
    DEFAULT_TIMEOUT_S = 30.0
    DEFAULT_MAX_RETRIES = 3
    # 429 + 5xx are retried; everything else (400/401/403/etc.) bubbles
    # up immediately.
    RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        endpoint: Optional[str] = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        cost_per_1k: Optional[dict[str, dict[str, float]]] = None,
        transport_factory: Optional[TransportFactory] = None,
    ) -> None:
        if api_key is None:
            api_key = get_settings().DEEPSEEK_API_KEY
        self._api_key = api_key
        self._endpoint = endpoint or self.ENDPOINT
        self._timeout_s = float(timeout_s)
        self._max_retries = int(max_retries)
        self._cost_per_1k = cost_per_1k or DEFAULT_COST_PER_1K
        self._transport_factory = transport_factory or _default_transport_factory
        self._client: Optional[httpx.AsyncClient] = None

    # -- lifecycle -------------------------------------------------------

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=self._transport_factory(),
                timeout=self._timeout_s,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "DeepSeekClient":
        await self._ensure_client()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # -- cost ------------------------------------------------------------

    def estimate_cost_usd(
        self, model: str, prompt_tokens: int, completion_tokens: int
    ) -> float:
        """Compute the USD cost for a (model, prompt, completion) tuple.

        Unknown models return 0.0 — operators should configure pricing
        for any non-default model.
        """
        rates = self._cost_per_1k.get(model)
        if rates is None:
            return 0.0
        return (
            prompt_tokens * rates.get("prompt", 0.0)
            + completion_tokens * rates.get("completion", 0.0)
        ) / 1000.0

    # -- main entry point ----------------------------------------------

    async def complete(
        self,
        *,
        system: str,
        user: str,
        model: str = "deepseek-chat",
        temperature: float = 0.2,
        max_tokens: int = 2000,
        response_format_json: bool = True,
    ) -> DeepSeekResult:
        """Call DeepSeek chat/completions once (with retries).

        On 429 / 5xx: retry up to `max_retries` times with 1s, 2s, 4s
        backoff. On 4xx other than 429: raise `DeepSeekError` immediately.
        On transport / timeout: also retried (status is None).
        """
        if not self._api_key:
            raise DeepSeekError("DEEPSEEK_API_KEY is not configured")

        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
            "stream": False,
        }
        if response_format_json:
            body["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        last_err: Optional[DeepSeekError] = None
        for attempt in range(self._max_retries + 1):
            started = time.perf_counter()
            try:
                client = await self._ensure_client()
                response = await client.post(
                    self._endpoint, json=body, headers=headers
                )
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_err = DeepSeekError(f"transport error: {e!r}")
                if attempt >= self._max_retries:
                    log.warning(
                        "deepseek.transport_failed",
                        attempt=attempt,
                        error=str(e),
                    )
                    raise last_err from e
                await self._backoff(attempt)
                continue

            latency_ms = (time.perf_counter() - started) * 1000.0

            if response.status_code in self.RETRYABLE_STATUS:
                last_err = DeepSeekError(
                    f"deepseek {response.status_code}",
                    status_code=response.status_code,
                    body=_safe_body(response),
                )
                if attempt >= self._max_retries:
                    log.warning(
                        "deepseek.retry_exhausted",
                        status=response.status_code,
                        attempts=attempt + 1,
                    )
                    raise last_err
                log.info(
                    "deepseek.retry",
                    status=response.status_code,
                    attempt=attempt + 1,
                )
                await self._backoff(attempt)
                continue

            if response.status_code >= 400:
                # Non-retryable 4xx — usually 401 (bad key) or 400
                # (malformed request).
                raise DeepSeekError(
                    f"deepseek {response.status_code}: {_safe_body(response)}",
                    status_code=response.status_code,
                    body=_safe_body(response),
                )

            # 2xx — parse + return.
            try:
                data = response.json()
            except Exception as e:  # noqa: BLE001
                raise DeepSeekError(
                    f"deepseek returned non-JSON body: {e!r}",
                    status_code=response.status_code,
                    body=_safe_body(response),
                ) from e

            return self._build_result(data, model, latency_ms)

        # Unreachable: the loop either returns or raises.
        assert last_err is not None
        raise last_err

    async def _backoff(self, attempt: int) -> None:
        # 1s, 2s, 4s — small, deterministic so tests can be fast with a
        # patched sleep.
        delay = 2 ** attempt
        await asyncio.sleep(delay)

    def _build_result(
        self, data: dict[str, Any], requested_model: str, latency_ms: float
    ) -> DeepSeekResult:
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise DeepSeekError(
                f"deepseek response missing choices[0].message.content: {e!r}",
                body=str(data)[:500],
            ) from e

        if not isinstance(content, str):
            # Defensive: some models return a list of content parts.
            content = str(content)

        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
        model = str(data.get("model") or requested_model)
        cost_usd = self.estimate_cost_usd(model, prompt_tokens, completion_tokens)

        log.info(
            "deepseek.call",
            model=model,
            requested_model=requested_model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            latency_ms=round(latency_ms, 2),
            cost_usd=round(cost_usd, 6),
        )
        return DeepSeekResult(
            content=content,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            latency_ms=latency_ms,
            cost_usd=cost_usd,
            raw=data,
        )


# ---- helpers ------------------------------------------------------------


def _safe_body(response: httpx.Response) -> str:
    """Truncate response body for logging / error messages."""
    try:
        text = response.text
    except Exception:  # noqa: BLE001
        return "<unreadable>"
    if len(text) > 500:
        return text[:500] + "...<truncated>"
    return text
