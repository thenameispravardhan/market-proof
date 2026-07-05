"""In-process PDF prefetch cache (Phase 1 item 3).

The monitors call `prefetch(url)` the moment a filing is detected, so
the download runs in parallel with the event-bus hop and the analyzer's
gates. By the time the analyzer needs the bytes (extracted-text mode or
the hybrid fast track), `get_pdf(url)` usually just awaits an
already-finished task instead of starting a fresh download — saving
0.2-1.2s per analysis.

Design:
  - Module-level cache keyed by URL: {url: (created_monotonic, task)}.
    Monitors and the analyzer live in the same process/event loop, so a
    plain dict is enough — no locks needed (asyncio, single-threaded).
  - Entries expire after TTL_SECONDS and the cache is capped at
    MAX_ENTRIES (oldest evicted) so a busy filing day can't grow it
    unbounded. A finished task holding PDF bytes is at most ~25 MB
    (download_pdf's own cap); 64 entries bounds worst-case memory.
  - Fail-safe: a prefetch that errors just yields None from get_pdf,
    which then falls back to a direct download; a direct-download
    failure returns None and the caller falls back to the legacy
    metadata prompt. Nothing here can block a signal.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from app.analyzer.pdf_extract import download_pdf
from app.logging_config import get_logger

log = get_logger(__name__)

TTL_SECONDS = 600.0     # a prefetched PDF older than this is useless anyway
MAX_ENTRIES = 64

_cache: dict[str, tuple[float, "asyncio.Task[Optional[bytes]]"]] = {}


def _evict() -> None:
    """Drop expired entries; if still over cap, drop oldest first."""
    now = time.monotonic()
    expired = [u for u, (t, _task) in _cache.items() if now - t > TTL_SECONDS]
    for u in expired:
        _cache.pop(u, None)
    while len(_cache) > MAX_ENTRIES:
        oldest = min(_cache, key=lambda u: _cache[u][0])
        _cache.pop(oldest, None)


def prefetch(url: Optional[str], *, timeout_s: float = 6.0) -> None:
    """Fire-and-forget download start. Deduped by URL; never raises."""
    if not url:
        return
    try:
        if url in _cache:
            return
        _evict()
        task = asyncio.get_running_loop().create_task(
            download_pdf(url, timeout_s=timeout_s), name="pdf-prefetch"
        )
        # A failed prefetch must never surface as an "unretrieved
        # exception" warning — get_pdf handles the None/raise either way.
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        _cache[url] = (time.monotonic(), task)
    except RuntimeError:
        # No running loop (sync test context) — skip silently; the
        # analyzer will download directly.
        pass


async def get_pdf(url: Optional[str], *, timeout_s: float = 6.0) -> Optional[bytes]:
    """PDF bytes for `url`: from the prefetch cache when available,
    otherwise a direct download. Returns None on any failure."""
    if not url:
        return None
    entry = _cache.get(url)
    if entry is not None:
        created, task = entry
        if time.monotonic() - created <= TTL_SECONDS:
            try:
                data = await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)
                if data is not None:
                    return data
            except Exception:  # noqa: BLE001 — timeout / failed prefetch
                pass
        # Expired / failed / empty — fall through to a direct attempt.
        _cache.pop(url, None)
    return await download_pdf(url, timeout_s=timeout_s)


def clear() -> None:
    """Test helper."""
    _cache.clear()
