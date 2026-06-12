"""In-process pub/sub event bus.

Each subscriber gets a private `asyncio.Queue`. Publish is fire-and-forget;
queues that are full are logged and the event is dropped (to keep the
producer non-blocking). Use `publish_blocking` if you need backpressure.

Channels are arbitrary strings. There is no wildcard matching — subscribers
bind to exact channel names.

This is intentionally simple. It works inside one process. If you ever shard
the bot across processes, swap this for Redis pub/sub or NATS.
"""
from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from app.logging_config import get_logger

log = get_logger(__name__)

# Drop events for a subscriber whose queue is larger than this. Each subscriber
# gets its own queue, so this protects publishers from a single slow consumer.
_MAX_QUEUE = 1000


@dataclass(frozen=True)
class Event:
    channel: str
    payload: Any
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "payload": self.payload,
            "event_id": self.event_id,
            "ts": self.ts.isoformat(),
        }


class EventBus:
    """Per-subscriber asyncio.Queue pub/sub."""

    def __init__(self, max_queue: int = _MAX_QUEUE) -> None:
        self._subs: dict[str, set[asyncio.Queue[Event]]] = defaultdict(set)
        self._max_queue = max_queue
        self._lock = asyncio.Lock()

    # -- subscriber side -------------------------------------------------

    def subscribe(self, channel: str) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._max_queue)
        self._subs[channel].add(q)
        log.debug("event_bus.subscribe", channel=channel, queue_id=id(q))
        return q

    def unsubscribe(self, channel: str, queue: asyncio.Queue[Event]) -> None:
        subs = self._subs.get(channel)
        if not subs:
            return
        subs.discard(queue)
        if not subs:
            self._subs.pop(channel, None)
        log.debug("event_bus.unsubscribe", channel=channel, queue_id=id(queue))

    # -- publisher side --------------------------------------------------

    async def publish(self, channel: str, payload: Any) -> int:
        """Non-blocking publish. Returns the number of subscribers that
        received the event (0 if all queues were full and the event was
        dropped)."""
        event = Event(channel=channel, payload=payload)
        delivered = 0
        dropped = 0
        for q in list(self._subs.get(channel, ())):
            try:
                q.put_nowait(event)
                delivered += 1
            except asyncio.QueueFull:
                dropped += 1
                log.warning(
                    "event_bus.drop_full_queue",
                    channel=channel,
                    queue_id=id(q),
                    event_id=event.event_id,
                )
        if delivered or dropped:
            log.debug(
                "event_bus.publish",
                channel=channel,
                event_id=event.event_id,
                delivered=delivered,
                dropped=dropped,
            )
        return delivered

    async def publish_blocking(self, channel: str, payload: Any, timeout: float = 5.0) -> int:
        """Like `publish` but waits for each subscriber queue to drain if
        full. Use only when you really need backpressure (e.g. shutdown
        drain)."""
        event = Event(channel=channel, payload=payload)
        delivered = 0
        try:
            async with asyncio.timeout(timeout):
                for q in list(self._subs.get(channel, ())):
                    await q.put(event)
                    delivered += 1
        except TimeoutError:
            log.error("event_bus.publish_blocking_timeout", channel=channel, event_id=event.event_id)
        return delivered

    # -- introspection ---------------------------------------------------

    def subscriber_count(self, channel: Optional[str] = None) -> int:
        if channel is None:
            return sum(len(qs) for qs in self._subs.values())
        return len(self._subs.get(channel, ()))

    def channels(self) -> list[str]:
        return list(self._subs.keys())


# Module-level singleton — sibling tracks import this directly.
event_bus = EventBus()
