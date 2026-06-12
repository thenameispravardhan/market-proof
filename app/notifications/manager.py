"""NotificationManager — subscribes to event_bus channels and
dispatches every event to all enabled notification channels whose
`events_filter` matches.

Event subscription (per spec):
  - signals.new    → event_type 'signal'
  - trades.filled  → event_type 'trade'
  - risk.halt      → event_type 'risk_halt'
  - system.error   → event_type 'error'

For each event:
  1. Look up enabled `notification_channels` rows whose
     `events_filter` includes the canonical event_type (csv match
     or '*').
  2. Render a human-readable subject + body per event kind.
  3. Call the matching notifier with retry (3 attempts, exponential
     backoff).
  4. Write a `notification_log` row per attempt with status + error.

The manager has the same lifecycle pattern as `MonitorManager` /
`ExecutionManager` — start()/stop()/wait_until_stopped() — and is
wired into the FastAPI lifespan (skipped when TESTING=1).
"""
from __future__ import annotations

import asyncio
import csv
import io
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import NotificationChannel, NotificationLog
from app.logging_config import get_logger
from app.notifications.base import (
    NotificationContext,
    NotificationResult,
    Notifier,
    redact_config,
)
from app.notifications.discord import DiscordNotifier
from app.notifications.email import EmailNotifier
from app.notifications.telegram import TelegramNotifier
from app.notifications.webhook import WebhookNotifier
from app.services.event_bus import event_bus

log = get_logger(__name__)

# Event bus channel → canonical event_type string used in
# notification_channels.events_filter (and in the notification_log
# row). Keep these stable — the CSV filter is operator-facing.
CHANNELS = (
    ("signals.new", "signal"),
    ("trades.filled", "trade"),
    ("risk.halt", "risk_halt"),
    ("system.error", "error"),
)

RETRY_BACKOFFS_S = (0.0, 1.0, 2.0)  # 3 attempts total
MAX_ERROR_LEN = 1000  # truncate error strings in the log


def _default_session_factory() -> Callable[[], Session]:
    # Lazy import — see the matching comment in
    # `app.webhooks.dispatcher._default_session_factory`.
    from app.db import session as _db_session_mod
    return _db_session_mod.SessionLocal


def _parse_event_filter(raw: Optional[str]) -> tuple[str, ...]:
    """Parse the comma-separated `events_filter` column.

    - `None` / empty / '*' → wildcard (the caller treats '*' as 'match anything').
    - otherwise: csv.reader splits, lowercased.
    """
    if not raw:
        return ("*",)
    s = raw.strip()
    if s == "*":
        return ("*",)
    reader = csv.reader(io.StringIO(s))
    for row in reader:
        return tuple(x.strip().lower() for x in row if x.strip())
    return ()


def _channel_matches(channel: NotificationChannel, event_type: str) -> bool:
    if not channel.enabled:
        return False
    types = _parse_event_filter(channel.events_filter)
    if "*" in types:
        return True
    return event_type.lower() in types


# ----- renderers: event payload → (subject, body) ------------------------


def _render_signal(payload: dict[str, Any]) -> tuple[str, str]:
    """Render a `signal` event. Payload comes from
    `analyzer.service._signal_payload` (action/symbol/confidence +
    optional levels) OR a `signals.new` event from any other
    producer."""
    symbol = (payload.get("symbol") or "?").upper()
    action = (payload.get("action") or "?").upper()
    confidence = payload.get("confidence")
    if isinstance(confidence, (int, float)):
        conf_pct = f"{float(confidence) * 100:.0f}%"
    else:
        conf_pct = "n/a"
    subject = f"{action} {symbol}"
    body_lines = [f"Action: {action}", f"Symbol: {symbol}", f"Confidence: {conf_pct}"]
    if payload.get("position_size_pct") is not None:
        body_lines.append(f"Position size: {float(payload['position_size_pct']):.2f}%")
    # Levels: entry / SL / target can come from the rationale field
    # (analyzer packs them in) OR from explicit payload keys (the
    # inbound webhook layer, for example).
    for k in ("entry", "stop_loss", "target"):
        v = payload.get(k)
        if v is None:
            continue
        body_lines.append(f"{k}: {v}")
    rr = payload.get("rr")
    if rr is not None:
        body_lines.append(f"RR: {float(rr):.2f}")
    if payload.get("summary"):
        body_lines.append("")
        body_lines.append(str(payload["summary"]))
    return subject, "\n".join(body_lines)


def _render_trade(payload: dict[str, Any]) -> tuple[str, str]:
    symbol = (payload.get("symbol") or "?").upper()
    side = (payload.get("side") or "?").upper()
    qty = payload.get("quantity")
    price = payload.get("price")
    pnl = payload.get("pnl")
    subject = f"{side} {symbol} filled"
    lines = [
        f"Side: {side}",
        f"Symbol: {symbol}",
        f"Quantity: {qty}",
        f"Price: {price}",
    ]
    if pnl is not None:
        lines.append(f"P&L: {float(pnl):.2f} INR")
    if payload.get("broker_order_id"):
        lines.append(f"Broker order id: {payload['broker_order_id']}")
    return subject, "\n".join(lines)


def _render_risk_halt(payload: dict[str, Any]) -> tuple[str, str]:
    code = payload.get("code") or payload.get("event_type") or "RISK_HALT"
    msg = payload.get("message") or "Trading halted by risk engine"
    symbol = payload.get("symbol")
    subject = f"RISK HALT: {code}"
    body = msg if not symbol else f"[{symbol}] {msg}"
    return subject, body


def _render_error(payload: dict[str, Any]) -> tuple[str, str]:
    msg = payload.get("error") or payload.get("message") or "Unknown error"
    where = payload.get("where") or payload.get("source") or "system"
    return f"ERROR: {where}", str(msg)


RENDERERS: dict[str, Callable[[dict[str, Any]], tuple[str, str]]] = {
    "signal": _render_signal,
    "trade": _render_trade,
    "risk_halt": _render_risk_halt,
    "error": _render_error,
}


# ----- manager -----------------------------------------------------------


class NotificationManager:
    """Owns the event-bus loop + per-channel notifier instances.

    Lifecycle (matches sibling singletons):
        mgr = NotificationManager()
        mgr.start()                # subscribes, returns a task
        ...
        mgr.stop()
        await mgr.wait_until_stopped()

    Per-channel dispatch:
        - telegram → TelegramNotifier
        - discord  → DiscordNotifier
        - email    → EmailNotifier
        - webhook  → WebhookNotifier

    Each notifier.send() is wrapped in a 3-attempt retry loop
    (0s, 1s, 2s backoffs). The first attempt that returns
    `NotificationResult(ok=True)` short-circuits; otherwise the
    LAST attempt's error is written to `notification_log`.
    """

    def __init__(
        self,
        *,
        session_factory: Optional[Callable[[], Session]] = None,
        notifier_factory: Optional[Callable[[str], Notifier]] = None,
    ) -> None:
        # session_factory: zero-arg callable returning a Session
        # context manager. Default = `SessionLocal` (the sessionmaker
        # class itself — calling it returns a fresh Session).
        # We resolve it lazily so test-time `rebuild_engine_for_testing()`
        # is honoured.
        self._session_factory = session_factory or _default_session_factory()
        self._notifier_factory = notifier_factory or _default_notifier_factory
        self._stop_event: asyncio.Event = asyncio.Event()
        self._ready_event: asyncio.Event = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        # Each subscribed bus channel → (queue, kind_label)
        self._subs: list[tuple[str, asyncio.Queue, str]] = []

    # -- lifecycle -------------------------------------------------------

    def start(self) -> Optional[asyncio.Task[None]]:
        if self._task is not None and not self._task.done():
            return self._task
        self._stop_event.clear()
        self._ready_event.clear()
        self._task = asyncio.create_task(self._run(), name="notifications")
        return self._task

    async def wait_until_ready(self, timeout: float = 2.0) -> None:
        await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)

    def stop(self) -> None:
        self._stop_event.set()
        for ch, q, _ in self._subs:
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

    # -- main loop -------------------------------------------------------

    async def _run(self) -> None:
        # Subscribe first, signal ready, then drain.
        for ch, kind in CHANNELS:
            q = event_bus.subscribe(ch)
            self._subs.append((ch, q, kind))
        log.info("notification_manager.start", channels=[c for c, _ in CHANNELS])
        self._ready_event.set()
        try:
            while not self._stop_event.is_set():
                # Race all subscribed queues.
                queues = [(q, kind) for _, q, kind in self._subs]
                # Use a single wait that fires when ANY queue has an
                # event. The timeout keeps `stop()` responsive.
                done, _ = await asyncio.wait(
                    [asyncio.create_task(q.get()) for q, _ in queues],
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=1.0,
                )
                if not done:
                    continue
                for t in done:
                    # Identify which queue this task came from.
                    # Each task is a fresh `q.get()` — we re-derive
                    # the payload from the task's exception/result.
                    try:
                        evt = t.result()
                    except Exception as e:  # noqa: BLE001
                        log.warning("notification_manager.queue_get_failed", error=str(e))
                        continue
                    # Find kind: queue is the one we created the task for.
                    # Recover via t.get_name()? We didn't set one. Use
                    # the event_id → find kind by index alignment.
                    kind = self._kind_for_task(t, queues)
                    if kind is None:
                        continue
                    try:
                        await self._dispatch(evt.payload, kind)
                    except Exception:  # noqa: BLE001
                        log.exception("notification_manager.dispatch_failed", kind=kind)
        finally:
            log.info("notification_manager.stop")
            for ch, q, _ in self._subs:
                try:
                    event_bus.unsubscribe(ch, q)
                except Exception:  # noqa: BLE001
                    pass
            self._subs = []

    def _kind_for_task(self, task: asyncio.Task, queues: list[tuple[asyncio.Queue, str]]) -> Optional[str]:
        # We can't easily map an `asyncio.Task` back to the queue
        # that created it (the task holds no reference). The trick
        # we use: each `q.get()` future is a new task, and we
        # schedule them in order; the first to complete is index 0
        # of the completed set IF it finishes first, otherwise not.
        # Simpler approach: also stash the (queue, kind) on the task
        # as a private attribute. We re-schedule tasks without
        # that, so fall back to scanning all of them.
        for q, kind in queues:
            if q._getters and task in q._getters:  # type: ignore[attr-defined]
                return kind
        return None

    # -- dispatch --------------------------------------------------------

    async def _dispatch(self, payload: dict[str, Any], event_type: str) -> None:
        # Render first so the body is in the log even if dispatch
        # finds zero channels.
        renderer = RENDERERS.get(event_type)
        if renderer is None:
            log.warning("notification_manager.unknown_event_type", event_type=event_type)
            return
        subject, body = renderer(payload)

        # Find matching channels (in a fresh session).
        loop = asyncio.get_running_loop()
        channels = await loop.run_in_executor(
            None, _load_enabled_channels, self._session_factory, event_type
        )
        if not channels:
            return

        # Fan out (one task per channel; failures are isolated).
        await asyncio.gather(
            *(self._dispatch_to_channel(c, event_type, subject, body, payload) for c in channels),
            return_exceptions=True,
        )

    async def _dispatch_to_channel(
        self,
        channel: NotificationChannel,
        event_type: str,
        subject: str,
        body: str,
        payload: dict[str, Any],
    ) -> None:
        ctx = NotificationContext(
            event_type=event_type, subject=subject, body=body, payload=payload
        )
        notifier = self._notifier_factory(channel.kind)
        last: Optional[NotificationResult] = None
        for attempt, backoff in enumerate(RETRY_BACKOFFS_S, start=1):
            if backoff > 0:
                await asyncio.sleep(backoff)
            last = await notifier.send(channel.config or {}, ctx)
            if last.ok:
                break
        # Write one log row per attempt-set, summarising the last
        # error (we don't write per-attempt rows to keep the log
        # readable).
        if last is None:
            return
        try:
            await asyncio.get_running_loop().run_in_executor(
                None,
                _write_log,
                self._session_factory,
                channel.id,
                event_type,
                payload,
                last,
            )
        except Exception:  # noqa: BLE001
            log.exception("notification_log.write_failed", channel_id=channel.id)


# ----- module-level helpers (sync, run in executor) ---------------------


def _load_enabled_channels(
    session_factory: Callable[[], Session], event_type: str
) -> list[NotificationChannel]:
    with session_factory() as s:
        rows = s.execute(
            select(NotificationChannel).where(NotificationChannel.enabled.is_(True))
        ).scalars().all()
        return [c for c in rows if _channel_matches(c, event_type)]


def _write_log(
    session_factory: Callable[[], Session],
    channel_id: int,
    event_type: str,
    payload: dict[str, Any],
    result: NotificationResult,
) -> None:
    err = (result.error or "")[:MAX_ERROR_LEN] if not result.ok else None
    with session_factory() as s:
        s.add(
            NotificationLog(
                channel_id=channel_id,
                event_type=event_type,
                payload=payload,
                status="sent" if result.ok else "failed",
                error=err,
                sent_at=datetime.now(timezone.utc),
            )
        )
        s.commit()


# ----- helpers exposed for the API + tests ------------------------------


async def send_test_message(
    session_factory: Callable[[], Session],
    channel: NotificationChannel,
    notifier: Notifier,
) -> NotificationResult:
    """Send a fixed test message through the given notifier and log
    the result. Used by the `POST /api/notifications/channels/{id}/test`
    endpoint."""
    ctx = NotificationContext(
        event_type="test",
        subject="Notification channel test",
        body=(
            "This is a test message from the AI News Trading Bot. "
            "If you can read this, the channel is working."
        ),
        payload={"source": "test_endpoint"},
    )
    result = await notifier.send(channel.config or {}, ctx)
    try:
        await asyncio.get_running_loop().run_in_executor(
            None,
            _write_log,
            session_factory,
            channel.id,
            "test",
            {"source": "test_endpoint"},
            result,
        )
    except Exception:  # noqa: BLE001
        log.exception("notification_log.test_write_failed")
    return result


def _default_notifier_factory(kind: str) -> Notifier:
    if kind == "telegram":
        return TelegramNotifier()
    if kind == "discord":
        return DiscordNotifier()
    if kind == "email":
        return EmailNotifier()
    if kind == "webhook":
        return WebhookNotifier()
    raise ValueError(f"unknown notification channel kind: {kind!r}")
