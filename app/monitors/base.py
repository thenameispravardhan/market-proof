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
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional, Union

try:
    import orjson as _fast_json

    def _json_dumps(obj: Any) -> str:
        return _fast_json.dumps(obj).decode("utf-8")

    def _json_loads(s: Union[str, bytes]) -> Any:
        if isinstance(s, str):
            s = s.encode("utf-8")
        return _fast_json.loads(s)
except ImportError:
    import json as _json_lib

    def _json_dumps(obj: Any) -> str:
        return _json_lib.dumps(obj)

    def _json_loads(s: Union[str, bytes]) -> Any:
        if isinstance(s, bytes):
            s = s.decode("utf-8")
        return _json_lib.loads(s)

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

# Rolling per-exchange tick telemetry (in-process; one monitor per
# exchange per process). The metrics API reads this to show the
# BOT-side share of detection latency (poll wait + fetch/parse/store)
# next to the exchange-side publish lag it cannot control.
MONITOR_TICK_STATS: dict[str, dict[str, Any]] = {}

# Channel for "newly inserted" announcements. Subscribers (T3 analyzer
# etc.) bind to this and process the payload.
CHANNEL_NEW = "announcements.new"

# Backoff envelope — exponential with a hard cap.
BACKOFF_INITIAL = 1.0      # seconds
BACKOFF_FACTOR = 2.0
BACKOFF_MAX = 60.0
BACKOFF_JITTER = 0.1       # +/- 10% so monitors don't synchronise

# Normal-cadence jitter. A poller that hits the exchange at exactly the
# same sub-second boundary every tick, forever, is an obvious bot pattern
# and the easiest thing for a WAF to rate-limit. We spread each sleep by
# +/- this fraction so NSE and BSE (and successive ticks) desynchronise.
# The mean stays at the configured interval, so detection speed is
# unchanged — this only removes the *regularity*, not the speed.
POLL_JITTER_FRAC = 0.15


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

    # Channel label distinguishing multiple monitors on the SAME
    # exchange (API poller vs RSS racer). Used for telemetry keys.
    channel: str = "API"

    def __init__(
        self,
        *,
        fetcher: Fetcher,
        parser: Parser,
        poll_interval: Optional[float] = None,
        source_url: Optional[str] = None,
        session_factory: Optional[Callable[[], Session]] = None,
        start_offset: float = 0.0,
        enabled_fn: Optional[Callable[[], bool]] = None,
        interval_fn: Optional[Callable[[], float]] = None,
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
        self._poll_interval_fixed: Optional[float] = poll_interval
        if source_url is not None:
            self.source_url = source_url
        self._stop_event: asyncio.Event = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        self._consecutive_failures = 0
        self._current_backoff = reset_backoff()
        # Cursor-based dedup: track the most recent `posted_at` seen
        # so the next tick can skip already-processed rows early,
        # avoiding redundant DB existence checks for 99% of the
        # day-long response window.
        self._last_seen: Optional[datetime] = None
        # Phase offset before the FIRST tick. The manager staggers the
        # NSE and BSE monitors by half the poll interval so a dual-listed
        # filing is seen by SOME monitor within ~interval/2 instead of a
        # full interval when both poll in lockstep.
        self._start_offset = max(0.0, float(start_offset))
        # Live on/off switch (frontend toggle). Checked every loop
        # iteration, so enabling/disabling a source applies within one
        # poll interval — no restart. None = always enabled.
        self._enabled_fn = enabled_fn
        # Optional per-source poll interval (live). A cheap channel (the
        # conditional-GET RSS feed) can safely poll faster than the
        # Akamai-fronted APIs. Returns seconds; <=0 or None → follow the
        # global POLL_INTERVAL_SECONDS.
        self._interval_fn = interval_fn
        # Tick telemetry: rolling fetch+parse+store durations feeding
        # MONITOR_TICK_STATS (see module docstring on that dict).
        self._tick_durations: deque[float] = deque(maxlen=50)
        self._last_tick_start: Optional[float] = None

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
        if self._interval_fn is not None:
            try:
                v = float(self._interval_fn())
                if v > 0:
                    return v
            except Exception:  # noqa: BLE001 — fall back to the global setting
                pass
        return float(get_settings().POLL_INTERVAL_SECONDS)

    def _jittered_interval(self) -> float:
        """Effective poll interval with a small +/- jitter applied.

        Keeps the mean at ``_poll_interval`` (detection speed unchanged)
        while breaking the perfectly-regular request cadence that makes a
        scraper trivial to fingerprint and rate-limit. See
        ``POLL_JITTER_FRAC``.
        """
        import random

        base = self._poll_interval
        return max(0.0, base * (1.0 + random.uniform(-POLL_JITTER_FRAC, POLL_JITTER_FRAC)))

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
            start_offset_s=self._start_offset,
        )
        # Phase stagger: wait out the offset before the first tick
        # (cancellable so stop() during the offset exits immediately).
        if self._start_offset > 0:
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._start_offset
                )
                return  # stop was signalled during the offset
            except asyncio.TimeoutError:
                pass
        try:
            while not self._stop_event.is_set():
                # Frontend toggle: when this source is disabled, idle for
                # one interval and re-check. Not a failure; no backoff.
                if self._enabled_fn is not None and not self._enabled_fn():
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(), timeout=self._poll_interval
                        )
                        break
                    except asyncio.TimeoutError:
                        continue
                try:
                    t_tick = time.monotonic()
                    gap_s = (
                        t_tick - self._last_tick_start
                        if self._last_tick_start is not None
                        else None
                    )
                    self._last_tick_start = t_tick
                    await self._tick()
                    self._record_tick_stats(
                        (time.monotonic() - t_tick) * 1000.0, gap_s
                    )
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

                # Normal cadence between successful ticks (jittered so the
                # two monitors don't hit the exchanges in lockstep).
                if not self._stop_event.is_set():
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(), timeout=self._jittered_interval()
                        )
                        # Stop was signalled — exit the loop.
                        break
                    except asyncio.TimeoutError:
                        pass  # interval elapsed, tick again
        finally:
            log.info("monitor.stop", exchange=self.exchange)

    def _record_tick_stats(self, tick_ms: float, gap_s: Optional[float]) -> None:
        """Publish rolling tick telemetry into MONITOR_TICK_STATS."""
        self._tick_durations.append(tick_ms)
        MONITOR_TICK_STATS[f"{self.exchange}-{self.channel}"] = {
            "last_tick_ms": round(tick_ms, 1),
            "avg_tick_ms": round(
                sum(self._tick_durations) / len(self._tick_durations), 1
            ),
            "ticks_sampled": len(self._tick_durations),
            "last_gap_s": round(gap_s, 2) if gap_s is not None else None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    async def _tick(self) -> None:
        """One poll cycle: fetch -> parse -> early-exit -> insert -> publish -> webhook."""
        raw = await self._fetcher(self.source_url)
        try:
            raws = self._parser(raw, self.source_url)
        except _FatalError:
            raise
        except Exception as e:  # noqa: BLE001
            # Parser errors are usually structural (page layout changed) —
            # treat as retryable; the operator can fix the parser.
            raise _RetryableError(f"parse failed: {e}") from e

        # Early-exit: skip rows whose posted_at is at or before our
        # last-seen cursor (with a 5s overlap window so we don't miss
        # rows that arrived during this tick with the same timestamp).
        if self._last_seen is not None and raws:
            cutoff = self._last_seen - timedelta(seconds=5)
            before = len(raws)
            raws = [r for r in raws if r.posted_at > cutoff]
            skipped = before - len(raws)
            if skipped:
                log.debug(
                    "monitor.early_exit",
                    exchange=self.exchange,
                    total=before,
                    kept=len(raws),
                    skipped=skipped,
                )

        log.debug("monitor.fetched", exchange=self.exchange, count=len(raws))

        # Update the last-seen cursor to the newest row we encountered.
        for r in raws:
            if self._last_seen is None or r.posted_at > self._last_seen:
                self._last_seen = r.posted_at

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
        # PDF PREFETCH: start the download NOW, in parallel with the
        # event-bus hop and the analyzer's gates, so extracted-text mode
        # and the hybrid fast track find the bytes already in the cache.
        # Only when a feature that reads PDFs is on, and never for
        # clearly-administrative junk the analyzer will skip anyway.
        try:
            settings = get_settings()
            if raw_a.pdf_url and (
                bool(getattr(settings, "SEND_EXTRACTED_TEXT", False))
                or bool(getattr(settings, "FAST_TRACK_ENABLED", False))
            ):
                from app.analyzer.prompts import is_non_material

                if not is_non_material(raw_a.title or ""):
                    from app.analyzer import pdf_cache

                    pdf_cache.prefetch(
                        raw_a.pdf_url,
                        timeout_s=float(
                            getattr(settings, "PDF_FETCH_TIMEOUT_SECONDS", 6.0)
                        ),
                    )
        except Exception:  # noqa: BLE001 — prefetch must never break publish
            log.debug("monitor.prefetch_failed", announcement_id=new_id)
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

        Returns the new row's id, or None on duplicate. Two dedupe
        layers:
          1. content_hash — exact repeats within one channel.
          2. cross-source: the SAME filing arriving via a different
             channel (RSS racer vs API) carries the same headline but a
             timestamp that can differ by ~1s, so the hash misses it.
             Same exchange + symbol + normalized headline within a 30
             minute window = the same filing; skip.
        """
        with self._session_factory() as session:
            existing = (
                session.query(Announcement)
                .filter(Announcement.content_hash == content_hash)
                .one_or_none()
            )
            if existing is not None:
                return None
            # Cross-source dedupe (cheap: runs only for hash-new rows,
            # scans a handful of same-symbol rows).
            norm_new = _normalize_headline(announcement.headline)
            filed = announcement.filed_at
            if filed is not None and filed.tzinfo is not None:
                filed = filed.astimezone(timezone.utc).replace(tzinfo=None)
            recent = (
                session.query(Announcement)
                .filter(
                    Announcement.exchange == announcement.exchange,
                    Announcement.symbol == announcement.symbol,
                )
                .order_by(Announcement.id.desc())
                .limit(10)
                .all()
            )
            for r in recent:
                if _normalize_headline(r.headline) != norm_new:
                    continue
                r_filed = r.filed_at
                if filed is None or r_filed is None:
                    return None  # same headline, timestamps unknown: dupe
                if abs((r_filed - filed).total_seconds()) <= 1800:
                    log.debug(
                        "monitor.cross_source_dedup",
                        exchange=self.exchange,
                        channel=self.channel,
                        symbol=announcement.symbol,
                        existing_id=r.id,
                    )
                    return None
            session.add(announcement)
            session.commit()
            session.refresh(announcement)
            return announcement.id


def _normalize_headline(s: Optional[str]) -> str:
    """Case/whitespace-insensitive headline identity for cross-source
    dedupe (RSS and API render the same filing text identically apart
    from stray whitespace)."""
    return " ".join((s or "").upper().split())


# -- error categories ---------------------------------------------------


class _RetryableError(Exception):
    """Network / 5xx / 429 — back off and retry."""


class _FatalError(Exception):
    """Permanent failure — stop the monitor."""
