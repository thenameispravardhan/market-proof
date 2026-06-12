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
    ) -> None:
        settings = get_settings()
        if nse_monitor is None:
            nse_monitor = NSEMonitor(poll_interval=settings.POLL_INTERVAL_SECONDS)
        if bse_monitor is None:
            bse_monitor = BSEMonitor(poll_interval=settings.POLL_INTERVAL_SECONDS)
        self._nse = nse_monitor
        self._bse = bse_monitor
        self._started = False

    @property
    def nse(self) -> BaseMonitor:
        return self._nse

    @property
    def bse(self) -> BaseMonitor:
        return self._bse

    async def start(self) -> None:
        """Start both monitors as independent asyncio tasks."""
        if self._started:
            return
        log.info("monitor_manager.start", exchanges=["NSE", "BSE"])
        self._nse.start()
        self._bse.start()
        self._started = True

    async def stop(self) -> None:
        """Stop both monitors and await clean shutdown."""
        if not self._started:
            return
        log.info("monitor_manager.stop")
        self._nse.stop()
        self._bse.stop()
        # Run the awaits concurrently so we don't serialise shutdown.
        await asyncio.gather(
            self._nse.wait_until_stopped(),
            self._bse.wait_until_stopped(),
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
