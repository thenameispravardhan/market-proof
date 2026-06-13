"""Base announcement monitor.

Subclasses override:
  - `exchange`      — "NSE" or "BSE"
  - `source_url`    — the human-facing URL we want to scrape
  - `fetch_raw`     — async coroutine returning bytes / str (the raw
                       HTML or JSON response from the exchange)
  - `parse`         — pure function: bytes/str -> list[RawAnnouncement]

Everything else (polling, backoff, dedup, publish, webhook) is shared
and is fully testable by injecting a stub fetcher + parser.
"""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, Union

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Announcement
from app.logging_config import get_logger
from app.services import webhook_service
from app.services.event_bus import event_bus


def _default_session_factory():
    """Lazily import SessionLocal so tests that rebuild the engine
    (e.g. swap to in-memory SQLite) are picked up by the monitor."""
    from app.db.session import SessionLocal
    return SessionLocal()

log = get_logger(__name__)

# Channel for "newly inserted" announcements. Subscribers (T3 analyzer
# etc.) bind to this and process the payload.
CHANNEL_NEW = "announcements.new"

# Backoff envelope — exponential with a hard cap.
BACKOFF_INITIAL = 1.0      # seconds
BACKOFF_FACTOR = 2.0
BACKOFF_MAX = 60.0
BACKOFF_JITTER = 0.1       # +/- 10% so monitors don't synchronise


@dataclass(frozen=True)
class RawAnnouncement:
    """Exchange-agnostic announcement parsed from the raw page payload.

    `posted_at` MUST be timezone-aware (UTC) by the time it reaches the
    monitor — parsers normalise to UTC before constructing this.
    """
    company: str
    title: str
    posted_at: datetime
    pdf_url: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "company": self.company,
            "title": self.title,
            "posted_at": self.posted_at.isoformat(),
            "pdf_url": self.pdf_url,
        }


# A fetcher returns the raw payload (HTML / JSON) that the parser will
# then turn into RawAnnouncements. Async so we can use Playwright /
# aiohttp under the hood. Accepts the monitor's `source_url` for
# convenience — some fetchers want the URL as a hint.
Fetcher = Callable[[str], Awaitable[Union[str, bytes]]]

# A parser is pure: takes the raw payload + source URL and returns
# the list of RawAnnouncements. Errors must raise (they're caught and
# logged by the monitor loop, which then backs off).
Parser = Callable[[Union[str, bytes], str], list[RawAnnouncement]]


def make_content_hash(
    *,
    exchange: str,
    company: str,
    title: str,
    posted_at: datetime,
) -> str:
    """SHA-256 hex digest used to dedupe announcements.

    Contract from T2 spec: ``sha256(f"{exchange}|{company}|{title}|{posted_at_iso}")``.

    Note: this is a SEPARATE function from `app.db.models.compute_content_hash`
    (which hashes `exchange|symbol|filed_at|pdf_url`). Both feed the
    same UNIQUE column — whichever is consistent wins, but the T2
    contract here is the canonical one and the scraper is what writes
    the value in production.
    """
    norm_exchange = (exchange or "").strip().upper()
    norm_company = (company or "").strip().upper()
    norm_title = (title or "").strip()
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    norm_posted = posted_at.astimezone(timezone.utc).isoformat()
    payload = f"{norm_exchange}|{norm_company}|{norm_title}|{norm_posted}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def next_backoff(current: float, *, _rng: Optional[Callable[[float, float], float]] = None) -> float:
    """Pure function — exponential backoff with a 60s ceiling.

    - First failure: 1.0s
    - Second: 2.0s
    - Third: 4.0s
    - ... up to 60.0s

    Add a small jitter so two monitors don't synchronise their retries.
    `_rng` is an optional random-number generator injectable for tests
    (signature: `uniform(low, high) -> float`).
    """
    if current <= 0:
        next_val = BACKOFF_INITIAL
    else:
        next_val = min(current * BACKOFF_FACTOR, BACKOFF_MAX)
    # Apply jitter; never below BACKOFF_INITIAL.
    if _rng is None:
        import random
        _rng = random.uniform
    jitter = next_val * BACKOFF_JITTER
    return max(BACKOFF_INITIAL, next_val + _rng(-jitter, jitter))


def reset_backoff() -> float:
    """Return the starting backoff value (call after a successful tick)."""
    return BACKOFF_INITIAL


class BaseMonitor:
    """Polling loop shared by NSE and BSE monitors.

    Lifecycle::

        mon = MyMonitor(...)
        task = asyncio.create_task(mon.run())
        ...
        mon.stop()        # signals the loop to exit
        await task         # waits for clean shutdown
    """

    exchange: str = "BASE"
    source_url: str = ""

    def __init__(
        self,
        *,
        fetcher: Fetcher,
        parser: Parser,
        poll_interval: Optional[float] = None,
        source_url: Optional[str] = None,
        session_factory: Optional[Callable[[], Session]] = None,
    ) -> None:
        self._fetcher = fetcher
        self._parser = parser
        # Default to a lazy factory so we pick up the current
        # `app.db.session.SessionLocal` (which tests may have rebuilt
        # for in-memory SQLite).
        self._session_factory: Callable[[], Session] = session_factory or _default_session_factory
        # When no explicit interval is passed, `_poll_interval`
        # re-reads the live setting on every loop iteration so a UI
        # change applies on the next tick without a restart.
        self._poll_interval_fixed: Optional[float] = (
            float(poll_interval) if poll_interval is not None else None
        )
        if source_url is not None:
            self.source_url = source_url
        self._stop_event: asyncio.Event = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        self._consecutive_failures = 0
        self._current_backoff = reset_backoff()

    @property
    def _poll_interval(self) -> float:
        """Effective poll interval in seconds.

        If the monitor was constructed with an explicit interval that
        value is fixed; otherwise we follow the live
        `POLL_INTERVAL_SECONDS` setting so a UI/API change applies on
        the next loop iteration without restarting the monitor.
        """
        if self._poll_interval_fixed is not None:
            return self._poll_interval_fixed
        return float(get_settings().POLL_INTERVAL_SECONDS)

    # -- lifecycle ------------------------------------------------------

    def start(self) -> asyncio.Task[None]:
        """Create the background task. Idempotent — second call is a no-op."""
        if self._task is not None and not self._task.done():
            return self._task
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name=f"monitor-{self.exchange}")
        return self._task

    def stop(self) -> None:
        """Signal the loop to exit. Safe to call multiple times."""
        self._stop_event.set()

    async def wait_until_stopped(self) -> None:
        """Await the background task (used by Manager for clean shutdown)."""
        if self._task is None:
            return
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    # -- main loop ------------------------------------------------------

    async def _run(self) -> None:
        log.info(
            "monitor.start",
            exchange=self.exchange,
            source_url=self.source_url,
            poll_interval_s=self._poll_interval,
        )
        try:
            while not self._stop_event.is_set():
                try:
                    await self._tick()
                    # Success: reset backoff state.
                    self._consecutive_failures = 0
                    self._current_backoff = reset_backoff()
                except _RetryableError as e:
                    self._consecutive_failures += 1
                    self._current_backoff = next_backoff(self._current_backoff)
                    # The first few retries are expected (NSE/BSE often
                    # rate-limit or block bots). Log at DEBUG so an INFO
                    # runbook read is not flooded with one warning per
                    # tick. Operators can `LOG_LEVEL=DEBUG` to see them.
                    # We also promote to WARNING once per 10 consecutive
                    # failures so long outages are still surfaced.
                    if self._consecutive_failures % 10 == 0:
                        log.warning(
                            "monitor.retry.persistent",
                            exchange=self.exchange,
                            failure_count=self._consecutive_failures,
                            backoff_s=round(self._current_backoff, 2),
                            error=str(e),
                        )
                    else:
                        log.debug(
                            "monitor.retry",
                            exchange=self.exchange,
                            failure_count=self._consecutive_failures,
                            backoff_s=round(self._current_backoff, 2),
                            error=str(e),
                        )
                    # Sleep for the backoff, but stay cancellable.
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(), timeout=self._current_backoff
                        )
                        # Event got set during the wait — exit immediately.
                        break
                    except asyncio.TimeoutError:
                        pass  # backoff elapsed, try again
                except _FatalError as e:
                    log.error("monitor.fatal", exchange=self.exchange, error=str(e))
                    break
                except Exception as e:  # noqa: BLE001
                    # Unclassified — treat as retryable to be safe.
                    self._consecutive_failures += 1
                    self._current_backoff = next_backoff(self._current_backoff)
                    log.exception(
                        "monitor.unhandled",
                        exchange=self.exchange,
                        failure_count=self._consecutive_failures,
                    )
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(), timeout=self._current_backoff
                        )
                        break
                    except asyncio.TimeoutError:
                        pass

                # Normal cadence between successful ticks.
                if not self._stop_event.is_set():
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(), timeout=self._poll_interval
                        )
                        # Stop was signalled — exit the loop.
                        break
                    except asyncio.TimeoutError:
                        pass  # interval elapsed, tick again
        finally:
            log.info("monitor.stop", exchange=self.exchange)

    async def _tick(self) -> None:
        """One poll cycle: fetch -> parse -> insert -> publish -> webhook."""
        raw = await self._fetcher(self.source_url)
        try:
            raws = self._parser(raw, self.source_url)
        except _FatalError:
            raise
        except Exception as e:  # noqa: BLE001
            # Parser errors are usually structural (page layout changed) —
            # treat as retryable; the operator can fix the parser.
            raise _RetryableError(f"parse failed: {e}") from e
        log.debug("monitor.fetched", exchange=self.exchange, count=len(raws))
        for raw_a in raws:
            try:
                await self._insert_and_announce(raw_a)
            except Exception as e:  # noqa: BLE001
                # One bad row must not stop the others.
                log.warning(
                    "monitor.row_failed",
                    exchange=self.exchange,
                    company=raw_a.company,
                    error=str(e),
                )

    # -- dedup / publish ------------------------------------------------

    async def _insert_and_announce(self, raw_a: RawAnnouncement) -> None:
        """Insert one row; on new insert publish to event_bus + webhook."""
        h = make_content_hash(
            exchange=self.exchange,
            company=raw_a.company,
            title=raw_a.title,
            posted_at=raw_a.posted_at,
        )
        announcement = Announcement(
            content_hash=h,
            symbol=raw_a.company,
            exchange=self.exchange,
            event_type="ANNOUNCEMENT",
            headline=raw_a.title,
            pdf_url=raw_a.pdf_url,
            source=self.source_url,
            filed_at=raw_a.posted_at,
        )
        # Run the DB call in a thread to keep the event loop unblocked
        # even on slower disks / under load.
        loop = asyncio.get_running_loop()
        try:
            new_id = await loop.run_in_executor(None, self._insert_one, announcement, h)
        except IntegrityError:
            # Duplicate content_hash — another monitor / poll already
            # inserted this one. Not an error.
            log.debug(
                "monitor.dedup_skip",
                exchange=self.exchange,
                company=raw_a.company,
                content_hash=h,
            )
            return
        except Exception as e:  # noqa: BLE001
            raise _RetryableError(f"db insert failed: {e}") from e

        if new_id is None:
            # Insert returned None — concurrent insert beat us. Treat
            # as dedup.
            return

        payload = {
            "announcement_id": new_id,
            "exchange": self.exchange,
            "company": raw_a.company,
            "title": raw_a.title,
            "pdf_url": raw_a.pdf_url,
            "posted_at": raw_a.posted_at.isoformat(),
        }
        log.info(
            "monitor.new_announcement",
            exchange=self.exchange,
            company=raw_a.company,
            announcement_id=new_id,
        )
        try:
            await event_bus.publish(CHANNEL_NEW, payload)
        except Exception:  # noqa: BLE001
            # A failed publish should not lose the row — log and move on.
            log.exception("monitor.publish_failed", announcement_id=new_id)
        try:
            await webhook_service.emit("announcement", payload)
        except Exception:  # noqa: BLE001
            log.exception("monitor.webhook_failed", announcement_id=new_id)

    def _insert_one(self, announcement: Announcement, content_hash: str) -> Optional[int]:
        """Synchronous insert — runs in the executor.

        Returns the new row's id, or None on duplicate.
        """
        with self._session_factory() as session:
            existing = (
                session.query(Announcement)
                .filter(Announcement.content_hash == content_hash)
                .one_or_none()
            )
            if existing is not None:
                return None
            session.add(announcement)
            session.commit()
            session.refresh(announcement)
            return announcement.id


# -- error categories ---------------------------------------------------


class _RetryableError(Exception):
    """Network / 5xx / 429 — back off and retry."""


class _FatalError(Exception):
    """Permanent failure — stop the monitor."""
