"""Discord channel — posts via the channel webhook URL.

Discord webhook payload:
  POST {webhook_url}
  {
    "content": "<message>",          # 2000 char limit
    "username": "..." (optional),
    "embeds": [{"title": "...", "description": "...", "color": ...}]  # optional
  }

The response is a 204 No Content on success. 4xx/5xx carry a JSON
error body; we surface `message` in the error string.
"""
from __future__ import annotations

from typing import Any, Optional

import httpx

from app.logging_config import get_logger
from app.notifications.base import NotificationContext, NotificationResult

log = get_logger(__name__)


class DiscordNotifier:
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
        url = (channel_config.get("webhook_url") or "").strip()
        if not url:
            return NotificationResult(ok=False, error="discord: missing webhook_url")
        # Discord content limit is 2000 chars; truncate with an ellipsis
        # marker so the operator knows the message was cut.
        content = ctx.body
        if len(content) > 1900:
            content = content[:1900] + "\n...<truncated>"
        body = {"content": f"**{ctx.subject}**\n\n{content}"}
        try:
            client = await self._ensure_client()
            r = await client.post(url, json=body)
        except Exception as e:  # noqa: BLE001
            return NotificationResult(ok=False, error=f"transport: {e!r}")
        if r.status_code >= 500:
            return NotificationResult(
                ok=False, error=f"http {r.status_code}", status_code=r.status_code
            )
        if r.status_code >= 400:
            return NotificationResult(
                ok=False, error=f"http {r.status_code}: {_safe(r)}", status_code=r.status_code
            )
        return NotificationResult(ok=True, status_code=r.status_code)


def _safe(r: httpx.Response, limit: int = 200) -> str:
    try:
        t = r.text
    except Exception:  # noqa: BLE001
        return "<unreadable>"
    return t[:limit]
