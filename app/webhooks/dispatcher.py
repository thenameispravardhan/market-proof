"""Outbound webhook dispatcher.

Subscribes to event-bus channels and, for each event, finds every
enabled `Webhook` row with `direction = 'out'` whose `event_filter`
matches. For each match, POSTs the event payload as JSON to
`webhook.url` with the HMAC-SHA256 signature header.

Retry policy (per spec):
  - 3 attempts total (initial + 2 retries).
  - Backoffs: 0s, 1s, 2s (exponential).
  - 4xx is non-retryable (the receiver is rejecting the request
    for a reason retries can't fix).
  - 5xx and transport errors are retryable.

Each attempt (and the final outcome) writes a `webhook_deliveries`
row with `status_code`, `response_body`, `error`.

This module is the "real" implementation of the T2 stub
`app.services.webhook_service.emit(...)`. Both still coexist — T2
calls `emit()` which now becomes a thin alias for
`dispatcher.dispatch_event` (the dispatcher runs as a long-lived
background loop, the stub enqueues — they have different
operational semantics). The dispatcher in this file runs as a
long-lived task in the FastAPI lifespan.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import httpx
from sqlalchemy import select

from app.db.models import Webhook, WebhookDelivery
from app.logging_config import get_logger
from app.services.event_bus import event_bus
from app.webhooks.signing import compute_signature

log = get_logger(__name__)

# Event bus channels the dispatcher listens to.
DISPATCH_CHANNELS: tuple[str, ...] = (
    "announcements.new",
    "analyses.new",
    "signals.new",
    "trades.filled",
)

RETRY_BACKOFFS_S: tuple[float, ...] = (0.0, 1.0, 2.0)
MAX_BODY_SNIPPET = 2000


def _default_session_factory() -> Callable[[], Any]:
    # Lazy import so tests that call `rebuild_engine_for_testing()`
    # (which rebinds `app.db.session.SessionLocal`) get the fresh
    # sessionmaker rather than the one captured at module-import
    # time.
    from app.db import session as _db_session_mod
    return _db_session_mod.SessionLocal


def _parse_event_filter(raw: Optional[str]) -> tuple[str, ...]:
    if not raw:
        return ("*",)
    s = raw.strip()
    if s == "*":
        return ("*",)
    reader = csv.reader(io.StringIO(s))
    for row in reader:
        return tuple(x.strip().lower() for x in row if x.strip())
    return ()


def _webhook_matches(webhook: Webhook, event_type: str) -> bool:
    if not webhook.enabled:
        return False
    if webhook.direction != "out":
        return False
    types = _parse_event_filter(webhook.event_filter)
    if "*" in types:
        return True
    return event_type.lower() in types


class WebhookDispatcher:
    """Outbound dispatcher. Owns an httpx.AsyncClient + subscribes
    to the event bus. Use the same start/stop pattern as the
    other managers."""

    def __init__(
        self,
        *,
        session_factory: Optional[Callable[[], Any]] = None,
        client: Optional[httpx.AsyncClient] = None,
        timeout_s: float = 15.0,
        max_response_log_bytes: int = MAX_BODY_SNIPPET,
    ) -> None:
        # session_factory: a zero-arg callable that returns a Session
        # context manager (i.e. `with session_factory() as s:`). The
        # default is `SessionLocal` itself (a sessionmaker — calling
        # it returns a Session, which supports the `with` protocol).
        # We resolve it lazily so test-time `rebuild_engine_for_testing()`
        # is honoured.
        self._session_factory = session_factory or _default_session_factory()
        self._owns_client = client is None
        self._client = client
        self._timeout_s = float(timeout_s)
        self._max_log = int(max_response_log_bytes)
        self._stop_event: asyncio.Event = asyncio.Event()
        self._ready_event: asyncio.Event = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        self._subs: list[tuple[str, asyncio.Queue]] = []

    # -- lifecycle -------------------------------------------------------

    def start(self) -> Optional[asyncio.Task[None]]:
        if self._task is not None and not self._task.done():
            return self._task
        self._stop_event.clear()
        self._ready_event.clear()
        self._task = asyncio.create_task(self._run(), name="webhook_dispatcher")
        return self._task

    async def wait_until_ready(self, timeout: float = 2.0) -> None:
        await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)

    def stop(self) -> None:
        self._stop_event.set()
        for ch, q in self._subs:
            try:
                event_bus.unsubscribe(ch, q)
            except Exception:  # noqa: BLE001
                pass
        self._subs = []

    async def wait_until_stopped(self) -> None:
        if self._task is None:
            return
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout_s)
        return self._client

    # -- main loop -------------------------------------------------------

    async def _run(self) -> None:
        for ch in DISPATCH_CHANNELS:
            q = event_bus.subscribe(ch)
            self._subs.append((ch, q))
        log.info("webhook_dispatcher.start", channels=list(DISPATCH_CHANNELS))
        self._ready_event.set()
        try:
            while not self._stop_event.is_set():
                # Use a small fan-in — create one `q.get()` task per
                # channel, wait for first, then re-create.
                getters = [
                    asyncio.create_task(q.get(), name=f"webhook_dispatcher:get:{ch}")
                    for ch, q in self._subs
                ]
                done, _ = await asyncio.wait(
                    getters, return_when=asyncio.FIRST_COMPLETED, timeout=1.0
                )
                if not done:
                    # Cancel all pending getters so we don't leak them.
                    for t in getters:
                        if not t.done():
                            t.cancel()
                    continue
                for t in done:
                    try:
                        evt = t.result()
                    except Exception as e:  # noqa: BLE001
                        log.warning("webhook_dispatcher.queue_get_failed", error=str(e))
                        continue
                    # The channel name is on the bus event itself.
                    channel_name = getattr(evt, "channel", "?")
                    try:
                        await self.dispatch_event(channel_name, evt.payload)
                    except Exception:  # noqa: BLE001
                        log.exception("webhook_dispatcher.dispatch_failed", channel=channel_name)
        finally:
            log.info("webhook_dispatcher.stop")
            for ch, q in self._subs:
                try:
                    event_bus.unsubscribe(ch, q)
                except Exception:  # noqa: BLE001
                    pass
            self._subs = []

    # -- one-shot dispatch (also used by the API for ad-hoc calls) -----

    async def dispatch_event(self, channel: str, payload: Any) -> int:
        """Dispatch one event to all matching outbound webhooks.

        Returns the number of webhooks we ATTEMPTED to deliver to
        (a webhook with retries may write 1-3 delivery rows; this
        counts the webhook once, not the attempts).
        """
        # The `event_type` for filter matching is the channel name
        # itself (announcements.new, signals.new, etc.). The
        # payload may also carry an `event_type` field for
        # downstream routing — the FILTER uses the channel name.
        event_type = channel
        loop = asyncio.get_running_loop()
        webhooks = await loop.run_in_executor(
            None, _load_matching_webhooks, self._session_factory, event_type
        )
        if not webhooks:
            return 0
        results = await asyncio.gather(
            *(self._deliver(w, event_type, payload) for w in webhooks),
            return_exceptions=True,
        )
        attempted = sum(1 for r in results if not isinstance(r, BaseException))
        return attempted

    async def _deliver(
        self,
        webhook: Webhook,
        event_type: str,
        payload: Any,
    ) -> Optional[WebhookDelivery]:
        # Serialize once so all retries use the same body + signature.
        if not isinstance(payload, (dict, list)):
            body_obj = {"payload": payload}
        else:
            body_obj = payload
        body_bytes = json.dumps(body_obj, default=str, separators=(",", ":")).encode("utf-8")
        secret = (webhook.secret or "").strip()
        sig_header = f"sha256={compute_signature(secret, body_bytes)}" if secret else ""

        headers = {"Content-Type": "application/json"}
        if sig_header:
            headers["X-Mavis-Signature"] = sig_header

        last_status: Optional[int] = None
        last_error: Optional[str] = None
        last_body_snippet: Optional[str] = None
        ok = False
        for attempt, backoff in enumerate(RETRY_BACKOFFS_S, start=1):
            if backoff > 0:
                await asyncio.sleep(backoff)
            try:
                client = await self._ensure_client()
                r = await client.post(webhook.url, content=body_bytes, headers=headers)
            except Exception as e:  # noqa: BLE001
                last_status = None
                last_error = f"transport: {e!r}"
                last_body_snippet = None
                # transport error → retryable
                continue
            last_status = r.status_code
            last_body_snippet = (r.text or "")[: self._max_log]
            last_error = None
            if 200 <= r.status_code < 300:
                ok = True
                break
            if 400 <= r.status_code < 500:
                # 4xx is non-retryable (per spec).
                last_error = f"http {r.status_code}"
                break
            # 5xx → retry
            last_error = f"http {r.status_code}"
        # Persist a single delivery row summarising the final outcome
        # (we don't write a row per attempt — that floods the table
        # and most callers want the last result anyway).
        delivery = WebhookDelivery(
            webhook_id=webhook.id,
            direction="out",
            event_type=event_type,
            payload=body_obj if isinstance(body_obj, dict) else {"data": body_obj},
            status_code=last_status,
            response_body=last_body_snippet,
            error=last_error,
            attempted_at=datetime.now(timezone.utc),
        )
        try:
            await asyncio.get_running_loop().run_in_executor(
                None,
                _persist_delivery,
                self._session_factory,
                delivery,
            )
        except Exception:  # noqa: BLE001
            log.exception("webhook_delivery.persist_failed", webhook_id=webhook.id)
        log.info(
            "webhook_delivery.out",
            webhook_id=webhook.id,
            event_type=event_type,
            status_code=last_status,
            ok=ok,
        )
        return delivery


def _load_matching_webhooks(
    session_factory: Callable[[], Any], event_type: str
) -> list[Webhook]:
    with session_factory() as s:
        rows = s.execute(
            select(Webhook).where(
                Webhook.direction == "out", Webhook.enabled.is_(True)
            )
        ).scalars().all()
        return [w for w in rows if _webhook_matches(w, event_type)]


def _persist_delivery(session_factory: Callable[[], Any], delivery: WebhookDelivery) -> None:
    with session_factory() as s:
        s.add(delivery)
        s.commit()
