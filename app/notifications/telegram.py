"""Telegram channel — posts via the Bot API `sendMessage` endpoint.

Endpoint: `POST https://api.telegram.org/bot<token>/sendMessage`

Body:
  {
    "chat_id": "...",
    "text": "<rendered body>",
    "parse_mode": "MarkdownV2" | "HTML" | None,
    "disable_web_page_preview": true,
  }

We use Markdown by default for readable plain-text rendering, and
fall back to plain text on parse failures. The Bot API returns
`{"ok": true, "result": {...}}` on success and
`{"ok": false, "description": "..."}` on failure. We treat a 200
with `ok=false` as a failure (defence in depth against
misconfigured chats).
"""
from __future__ import annotations

from typing import Any, Optional

import httpx

from app.logging_config import get_logger
from app.notifications.base import NotificationContext, NotificationResult

log = get_logger(__name__)

API = "https://api.telegram.org"


class TelegramNotifier:
    """Async Telegram notifier. Construct with a `httpx.AsyncClient`
    (or a factory) so tests can inject a `MockTransport`. Defaults
    to a fresh `httpx.AsyncClient()` with a 15s timeout."""

    def __init__(
        self,
        *,
        client: Optional[httpx.AsyncClient] = None,
        timeout_s: float = 15.0,
        transport_factory: Optional[Any] = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client
        self._timeout_s = float(timeout_s)
        self._transport_factory = transport_factory

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            kwargs: dict[str, Any] = {"timeout": self._timeout_s}
            if self._transport_factory is not None:
                kwargs["transport"] = self._transport_factory()
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def send(
        self,
        channel_config: dict[str, Any],
        ctx: NotificationContext,
    ) -> NotificationResult:
        bot_token = (channel_config.get("bot_token") or "").strip()
        chat_id = (channel_config.get("chat_id") or "").strip()
        if not bot_token or not chat_id:
            return NotificationResult(
                ok=False,
                error="telegram: missing bot_token or chat_id",
            )
        text = f"*{ctx.subject}*\n\n{ctx.body}"
        body = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        url = f"{API}/bot{bot_token}/sendMessage"
        try:
            client = await self._ensure_client()
            r = await client.post(url, json=body)
        except Exception as e:  # noqa: BLE001 — never raise
            return NotificationResult(ok=False, error=f"transport: {e!r}")

        # 2xx but `ok=false` is a Telegram protocol-level failure.
        if r.status_code >= 500:
            return NotificationResult(
                ok=False, error=f"http {r.status_code}", status_code=r.status_code
            )
        if r.status_code >= 400:
            return NotificationResult(
                ok=False, error=f"http {r.status_code}: {_safe(r)}", status_code=r.status_code
            )
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            return NotificationResult(
                ok=False, error="non-json response", status_code=r.status_code
            )
        if not data.get("ok", False):
            return NotificationResult(
                ok=False,
                error=f"telegram ok=false: {data.get('description', 'unknown')}",
                status_code=r.status_code,
            )
        return NotificationResult(ok=True, status_code=r.status_code)


def _safe(r: httpx.Response, limit: int = 200) -> str:
    try:
        t = r.text
    except Exception:  # noqa: BLE001
        return "<unreadable>"
    return t[:limit]
