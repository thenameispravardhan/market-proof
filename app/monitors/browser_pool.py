"""Warm Playwright browser pool shared by NSE/BSE monitors.

Instead of launching a fresh Chromium on every fallback (1-2s cold
start), each monitor reuses a persistent browser instance. The browser
is created lazily on first use and stays warm for the process lifetime.
A broken browser (crash, OOM) is replaced on the next call.

Usage::

    from app.monitors.browser_pool import get_browser

    async def fetch_with_playwright(url: str) -> str:
        browser = await get_browser()
        context = await browser.new_context(...)
        ...
"""
from __future__ import annotations

import asyncio
from typing import Optional

from app.logging_config import get_logger

log = get_logger(__name__)

_WARM_BROWSER: Optional[object] = None  # playwright Browser
_WARM_BROWSER_LOCK = asyncio.Lock()
_WARM_BROWSER_PW: Optional[object] = None  # playwright module (for cleanup)


async def get_browser():
    """Return a warm (or freshly-launched) headless Chromium browser.

    Thread-safe via an async lock — concurrent callers wait for the
    first to finish launching instead of racing to create their own.
    """
    global _WARM_BROWSER, _WARM_BROWSER_PW

    if _WARM_BROWSER is not None and _WARM_BROWSER.is_connected():
        return _WARM_BROWSER

    async with _WARM_BROWSER_LOCK:
        # Double-check: another waiter may have created it.
        if _WARM_BROWSER is not None and _WARM_BROWSER.is_connected():
            return _WARM_BROWSER

        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-translate",
                "--disable-default-apps",
                "--mute-audio",
                "--no-first-run",
            ],
        )
        _WARM_BROWSER_PW = pw
        _WARM_BROWSER = browser
        log.info("browser_pool.warm_start")
        return browser


async def shutdown_browser() -> None:
    """Close the warm browser (called at app shutdown)."""
    global _WARM_BROWSER, _WARM_BROWSER_PW
    if _WARM_BROWSER is not None:
        try:
            await _WARM_BROWSER.close()
        except Exception:
            pass
        _WARM_BROWSER = None
    if _WARM_BROWSER_PW is not None:
        try:
            await _WARM_BROWSER_PW.stop()
        except Exception:
            pass
        _WARM_BROWSER_PW = None
