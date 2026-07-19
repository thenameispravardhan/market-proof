"""Owns the lifecycle of the NSE + BSE monitor tasks.

One `MonitorManager` per process; the FastAPI lifespan calls
`start()` on startup and `stop()` on shutdown. Tests can drive
`start()` / `stop()` directly with stubbed fetchers.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from app.config import get_settings
from app.logging_config import get_logger
from app.monitors.base import BaseMonitor
from app.monitors.bse import BSEMonitor, parse_bse_payload
from app.monitors.nse import NSEMonitor, parse_nse_payload

log = get_logger(__name__)


class MonitorManager:
    """Owns the NSE and BSE monitor tasks.

    Construction is cheap and does not touch the network. `start()`
    is what creates the asyncio tasks. `stop()` is idempotent and
    safe to call multiple times.
    """

    def __init__(
        self,
        *,
        nse_monitor: Optional[BaseMonitor] = None,
        bse_monitor: Optional[BaseMonitor] = None,
        nse_rss_monitor: Optional[BaseMonitor] = None,
    ) -> None:
        # Construct with no explicit interval so each monitor follows
        # the live POLL_INTERVAL_SECONDS setting and picks up UI/API
        # changes on the next tick (see BaseMonitor._poll_interval).
        interval = float(get_settings().POLL_INTERVAL_SECONDS)
        if nse_monitor is None:
            nse_monitor = NSEMonitor(
                enabled_fn=lambda: bool(
                    getattr(get_settings(), "NSE_API_ENABLED", True)
                )
            )
        if bse_monitor is None:
            # Stagger BSE by half the poll interval so the two exchanges
            # are polled in anti-phase: a dual-listed filing becomes
            # visible to SOME monitor within ~interval/2 instead of a
            # full interval when both poll in lockstep. The offset is
            # computed once at construction; the per-tick jitter keeps
            # the phases from re-locking if the interval changes later.
            bse_monitor = BSEMonitor(
                start_offset=interval / 2.0,
                enabled_fn=lambda: bool(
                    getattr(get_settings(), "BSE_API_ENABLED", True)
                ),
            )
        if nse_rss_monitor is None:
            # The RSS racer: measured to publish some filings BEFORE the
            # NSE API. Off by default (frontend toggle NSE_RSS_ENABLED);
            # phase-offset at 3/4 interval so the three sources spread
            # across the cycle instead of clustering.
            from app.monitors.nse_rss import NSERSSMonitor

            nse_rss_monitor = NSERSSMonitor(
                start_offset=interval * 0.75,
                enabled_fn=lambda: bool(
                    getattr(get_settings(), "NSE_RSS_ENABLED", False)
                ),
                # The RSS channel polls on its own (faster) interval —
                # conditional GET makes an unchanged feed nearly free.
                interval_fn=lambda: float(
                    getattr(get_settings(), "NSE_RSS_POLL_SECONDS", 0.0) or 0.0
                ),
            )
        self._nse = nse_monitor
        self._bse = bse_monitor
        self._nse_rss = nse_rss_monitor
        self._monitors: list[BaseMonitor] = [nse_monitor, bse_monitor, nse_rss_monitor]
        self._started = False

    @property
    def nse(self) -> BaseMonitor:
        return self._nse

    @property
    def bse(self) -> BaseMonitor:
        return self._bse

    @property
    def nse_rss(self) -> BaseMonitor:
        return self._nse_rss

    @property
    def monitors(self) -> list[BaseMonitor]:
        return list(self._monitors)

    async def start(self) -> None:
        """Start every monitor as an independent asyncio task."""
        if self._started:
            return
        log.info(
            "monitor_manager.start",
            sources=[f"{m.exchange}-{m.channel}" for m in self._monitors],
        )
        for m in self._monitors:
            m.start()
        self._started = True

    async def stop(self) -> None:
        """Stop every monitor and await clean shutdown."""
        if not self._started:
            return
        log.info("monitor_manager.stop")
        for m in self._monitors:
            m.stop()
        # Run the awaits concurrently so we don't serialise shutdown.
        await asyncio.gather(
            *(m.wait_until_stopped() for m in self._monitors),
            return_exceptions=True,
        )
        self._started = False


# Re-export the parsers so callers (and tests) can build a
# `MonitorManager` with stubbed fetchers without depending on the
# NSE / BSE class internals.
__all__ = [
    "MonitorManager",
    "NSEMonitor",
    "BSEMonitor",
    "parse_nse_payload",
    "parse_bse_payload",
]
