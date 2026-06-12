"""Webhook fan-out — stub owned by T2, real delivery by T7.

T2 (this track) calls `webhook_service.emit(event_type, payload)` after
inserting a new announcement. T7 will replace the body of `emit` with
real HTTP delivery (HMAC signing, retries, etc.). For now this stub:

  - Looks up enabled `Webhook` rows with `direction = 'out'` whose
    `event_filter` contains the event_type (CSV match, or "*").
  - Enqueues a `WebhookDelivery` row marked "pending" — T7's worker
    will pick it up and deliver it.

Keeping the lookup in T2 means T2 can ship and be tested without
T7 being done. The contract between the two tracks is the `emit()`
function signature + the Webhook/WebhookDelivery schema.
"""
from __future__ import annotations

import csv
import io
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.db.models import Webhook, WebhookDelivery
from app.db.session import SessionLocal
from app.logging_config import get_logger

log = get_logger(__name__)


def _event_types_for_webhook(event_filter: str | None) -> Iterable[str]:
    """Parse the comma-separated `event_filter` column.

    - `None` / empty / "*" => wildcard (the caller filters by wildcard)
    - otherwise: split on comma, strip whitespace, lowercase.
    """
    if not event_filter:
        return ("*",)
    raw = event_filter.strip()
    if raw == "*":
        return ("*",)
    # csv.reader handles quoted commas inside fields, just in case.
    reader = csv.reader(io.StringIO(raw))
    for row in reader:
        return tuple(s.strip().lower() for s in row if s.strip())
    return ()


def _webhook_matches(webhook: Webhook, event_type: str) -> bool:
    if not webhook.enabled:
        return False
    if webhook.direction != "out":
        return False
    types = _event_types_for_webhook(webhook.event_filter)
    if "*" in types:
        return True
    return event_type.lower() in types


def _enqueue_deliveries_sync(
    event_type: str, payload: dict[str, Any], session_factory: type[Session]
) -> int:
    """Synchronous DB enqueue — runs in the executor."""
    enqueued = 0
    with session_factory() as session:
        rows = session.query(Webhook).filter(
            Webhook.direction == "out", Webhook.enabled.is_(True)
        ).all()
        for w in rows:
            if not _webhook_matches(w, event_type):
                continue
            session.add(
                WebhookDelivery(
                    webhook_id=w.id,
                    direction="out",
                    event_type=event_type,
                    payload=payload,
                    status_code=None,  # not attempted yet
                    error=None,
                )
            )
            enqueued += 1
        if enqueued:
            session.commit()
    return enqueued


async def emit(event_type: str, payload: dict[str, Any]) -> int:
    """Enqueue an outbound webhook for every matching subscription.

    Returns the number of deliveries enqueued. Never raises — webhook
    fan-out is best-effort and must not break the caller.
    """
    import asyncio

    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, _enqueue_deliveries_sync, event_type, payload, SessionLocal
        )
    except Exception:  # noqa: BLE001
        log.exception("webhook_service.emit_failed", event_type=event_type)
        return 0
