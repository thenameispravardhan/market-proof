"""Generic outbound webhook channel — POSTs the event payload as JSON.

This is the 'webhook' kind of the `notification_channels` table
(distinct from the `webhooks` table used by the plugin extensibility
layer in `app/webhooks/`). It's a simpler outbound notifier that
posts the payload verbatim to `config.url` — no HMAC signing, no
retry config, no filter table.

Plugin / 3rd-party extension use cases: paste a Pipedream / Make /
n8n URL and get every matching event as JSON.

Retry policy is the same as the other notifiers (3x with backoff
in the manager) — but we still keep it in a separate notifier
class so the manager can call it via the same Notifier Protocol.
"""
from __future__ import annotations

from typing import Any, Optional

import httpx

from app.logging_config import get_logger
from app.notifications.base import NotificationContext, NotificationResult

log = get_logger(__name__)


class WebhookNotifier:
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
        url = (channel_config.get("url") or "").strip()
        if not url:
            return NotificationResult(ok=False, error="webhook: missing url")
        # Send the canonical envelope — the payload is wrapped in a
        # {event_type, subject, body, data} shape so receivers can
        # build a UI off the top-level fields.
        body = {
            "event_type": ctx.event_type,
            "subject": ctx.subject,
            "body": ctx.body,
            "data": ctx.payload,
        }
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
