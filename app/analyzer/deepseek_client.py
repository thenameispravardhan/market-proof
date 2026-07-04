"""Async DeepSeek chat-completions client.

Endpoint: https://api.deepseek.com/v1/chat/completions

Design choices:

- async httpx with a configurable transport so tests can inject a
  `MockTransport` (see `test_deepseek_client.py`).
- 30s default timeout. Retries on HTTP 429/5xx follow settings:
  LLM_MAX_RETRIES (default 1) with exponential backoff starting at
  LLM_RETRY_BACKOFF_SECONDS (default 0.5s). 4xx other than 429 is
  *not* retried — a malformed request won't fix itself.
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
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

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
    # Retry policy defaults come from settings (LLM_MAX_RETRIES /
    # LLM_RETRY_BACKOFF_SECONDS). For a speed-news pipeline the old
    # 3-retry 1s-2s-4s ladder added up to 7s of dead time to a failing
    # call — one fast retry then give up is the right trade.
    RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        endpoint: Optional[str] = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_retries: Optional[int] = None,
        cost_per_1k: Optional[dict[str, dict[str, float]]] = None,
        transport_factory: Optional[TransportFactory] = None,
    ) -> None:
        settings = get_settings()
        if api_key is None:
            api_key = settings.DEEPSEEK_API_KEY
        self._api_key = api_key
        self._endpoint = endpoint or self.ENDPOINT
        self._timeout_s = float(timeout_s)
        if max_retries is None:
            max_retries = int(getattr(settings, "LLM_MAX_RETRIES", 1))
        self._max_retries = int(max_retries)
        self._backoff_base_s = float(
            getattr(settings, "LLM_RETRY_BACKOFF_SECONDS", 0.5)
        )
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
        reasoning_effort: Optional[str] = None,
        thinking: bool = True,
        stream: bool = False,
        response_format_json: bool = True,
    ) -> DeepSeekResult:
        """Call DeepSeek chat/completions once (with retries).

        On 429 / 5xx: retry up to `max_retries` times with exponential
        backoff (base LLM_RETRY_BACKOFF_SECONDS). On 4xx other than 429:
        raise `DeepSeekError` immediately.
        On transport / timeout: also retried (status is None).

        v4 inference controls:
          - `reasoning_effort` (low|medium|high) tunes how deeply the model
            reasons; sent whenever provided.
          - `thinking=False` sends the DeepSeek
            extra_body={"thinking": {"type": "disabled"}} flag, turning the
            reasoning trace off (faster / cheaper).
          - `stream=True` consumes the SSE stream and re-assembles it into
            the same `DeepSeekResult`, so callers downstream (JSON parse →
            signal → trade) are unaffected by the transport choice.
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
            "stream": bool(stream),
        }
        if response_format_json:
            body["response_format"] = {"type": "json_object"}
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        if not thinking:
            # extra_body={"thinking": {"type": "disabled"}}
            body["thinking"] = {"type": "disabled"}
        if stream:
            # Ask the API to emit a final usage chunk so cost/token
            # accounting still works in streaming mode.
            body["stream_options"] = {"include_usage": True}

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
                if stream:
                    status, data, body_text = await self._attempt_stream(
                        client, body, headers, model
                    )
                else:
                    status, data, body_text = await self._attempt_post(
                        client, body, headers
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

            if status in self.RETRYABLE_STATUS:
                last_err = DeepSeekError(
                    f"deepseek {status}",
                    status_code=status,
                    body=body_text,
                )
                if attempt >= self._max_retries:
                    log.warning(
                        "deepseek.retry_exhausted",
                        status=status,
                        attempts=attempt + 1,
                    )
                    raise last_err
                log.info(
                    "deepseek.retry",
                    status=status,
                    attempt=attempt + 1,
                )
                await self._backoff(attempt)
                continue

            if status >= 400:
                # Non-retryable 4xx — usually 401 (bad key) or 400
                # (malformed request).
                raise DeepSeekError(
                    f"deepseek {status}: {body_text}",
                    status_code=status,
                    body=body_text,
                )

            # 2xx — `data` is already parsed (post) or assembled (stream).
            return self._build_result(data, model, latency_ms)

        # Unreachable: the loop either returns or raises.
        assert last_err is not None
        raise last_err

    async def _attempt_post(
        self,
        client: httpx.AsyncClient,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> tuple[int, Optional[dict[str, Any]], str]:
        """Single non-streaming POST. Returns (status, data, body_text).

        For non-2xx, `data` is None and `body_text` carries the (truncated)
        error body. For 2xx, `data` is the parsed JSON; a non-JSON 2xx body
        raises `DeepSeekError` immediately (not retried).
        """
        response = await client.post(self._endpoint, json=body, headers=headers)
        if response.status_code >= 400:
            return response.status_code, None, _safe_body(response)
        try:
            data = response.json()
        except Exception as e:  # noqa: BLE001
            raise DeepSeekError(
                f"deepseek returned non-JSON body: {e!r}",
                status_code=response.status_code,
                body=_safe_body(response),
            ) from e
        return response.status_code, data, ""

    async def _attempt_stream(
        self,
        client: httpx.AsyncClient,
        body: dict[str, Any],
        headers: dict[str, str],
        requested_model: str,
    ) -> tuple[int, Optional[dict[str, Any]], str]:
        """Single streaming POST. Re-assembles the SSE deltas into a normal
        chat-completion dict so `_build_result` can stay unchanged.

        Returns (status, data, body_text) with the same contract as
        `_attempt_post`.
        """
        content_parts: list[str] = []
        usage: dict[str, Any] = {}
        model_seen: Optional[str] = None
        async with client.stream(
            "POST", self._endpoint, json=body, headers=headers
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                return response.status_code, None, _safe_body(response)
            async for raw_line in response.aiter_lines():
                line = raw_line.strip()
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    # Tolerate keep-alive / partial frames.
                    continue
                if chunk.get("model"):
                    model_seen = chunk["model"]
                choices = chunk.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        content_parts.append(piece)
                if chunk.get("usage"):
                    usage = chunk["usage"]
            status = response.status_code

        data = {
            "model": model_seen or requested_model,
            "choices": [
                {"message": {"content": "".join(content_parts)}}
            ],
            "usage": usage,
        }
        return status, data, ""

    async def _backoff(self, attempt: int) -> None:
        # base × 1, 2, 4… — deterministic so tests can be fast with a
        # patched sleep. Base defaults to 0.5s (LLM_RETRY_BACKOFF_SECONDS):
        # news alpha decays in seconds, so waiting whole seconds between
        # retries just converts a recoverable blip into a stale trade.
        delay = self._backoff_base_s * (2 ** attempt)
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
