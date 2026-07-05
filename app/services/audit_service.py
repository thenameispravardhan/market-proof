"""Buffered audit-log writer.

Writes ``AuditLog`` rows in batches (every 1 s or every 100 rows,
whichever comes first) so that high-frequency events (trade closures,
risk blocks, etc.) don't hammer SQLite with one INSERT per event.
Reduces write-lock contention during announcement bursts.

Lifecycle mirrors other services (start / stop):

    svc = AuditService(session_factory)
    svc.start()
    ...
    svc.stop()
    await svc.wait_until_stopped()
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from app.logging_config import get_logger

log = get_logger(__name__)

FLUSH_INTERVAL_S = 1.0   # flush the buffer at most once per second
FLUSH_BATCH_SIZE = 100    # or when the buffer reaches this many rows


def _default_session_factory():
    from app.db.session import SessionLocal
    return SessionLocal


class AuditService:
    """Buffered, async audit-log writer.

    Call ``log(...)`` from any coroutine — it appends to an in-memory
    buffer and returns immediately.  A background task flushes the
    buffer to SQLite every second (or when the buffer hits 100 rows).
    """

    def __init__(
        self,
        session_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._session_factory = session_factory or _default_session_factory
        self._buffer: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._flush_event = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None

    # -- public API -------------------------------------------------------

    def log(
        self,
        actor: str,
        action: str,
        target: str,
        *,
        before: Optional[dict[str, Any]] = None,
        after: Optional[dict[str, Any]] = None,
    ) -> None:
        """Enqueue an audit entry.  Thread/async-safe, never blocks."""
        from app.db.models import AuditLog

        entry = {
            "actor": actor,
            "action": action,
            "target": target,
            "before": before,
            "after": after,
        }
        # Fire-and-forget: schedule the append on the event loop.
        # Using call_soon_threadsafe is safe from both sync and async
        # contexts (the callers in executor threads and async handlers).
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no running loop — skip (e.g. during early startup)
        loop.call_soon_threadsafe(self._append, entry)

    def _append(self, entry: dict[str, Any]) -> None:
        """Synchronously append to buffer (runs on the event loop thread)."""
        from app.db.models import AuditLog

        self._buffer.append(entry)
        if len(self._buffer) >= FLUSH_BATCH_SIZE:
            self._flush_event.set()

    # -- lifecycle -------------------------------------------------------

    def start(self) -> asyncio.Task[None]:
        if self._task is not None and not self._task.done():
            return self._task
        self._stop_event.clear()
        self._flush_event.clear()
        self._task = asyncio.create_task(self._run(), name="audit-service")
        return self._task

    def stop(self) -> None:
        self._stop_event.set()
        self._flush_event.set()

    async def wait_until_stopped(self) -> None:
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._task, timeout=3.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        finally:
            self._task = None

    @property
    def buffer_size(self) -> int:
        return len(self._buffer)

    # -- internals -------------------------------------------------------

    async def _run(self) -> None:
        log.info("audit_service.start")
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._flush_event.wait(), timeout=FLUSH_INTERVAL_S
                    )
                except asyncio.TimeoutError:
                    pass  # time-based flush
                self._flush_event.clear()
                await self._flush()
            # Final flush on shutdown.
            await self._flush()
        finally:
            log.info("audit_service.stop", remaining=len(self._buffer))

    async def _flush(self) -> None:
        """Write buffered entries to the DB in a single transaction."""
        if not self._buffer:
            return
        # Atomically drain the buffer.
        async with self._lock:
            batch = self._buffer[:]
            self._buffer.clear()
        if not batch:
            return

        from app.db.models import AuditLog

        def _do_flush() -> None:
            with self._session_factory() as session:
                for entry in batch:
                    session.add(
                        AuditLog(
                            actor=entry["actor"],
                            action=entry["action"],
                            target=entry["target"],
                            before=entry.get("before"),
                            after=entry.get("after"),
                        )
                    )
                session.commit()

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, _do_flush)
        except Exception:  # noqa: BLE001
            log.exception("audit_service.flush_failed", count=len(batch))


# Module-level singleton — imported and wired in the app lifespan.
audit_service = AuditService()
