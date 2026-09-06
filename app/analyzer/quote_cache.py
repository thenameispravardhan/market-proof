"""In-process quote prefetch — the market-context half of `pdf_cache`.

The rules engine has declared `price` and `change_pct` since T1, but
nothing ever handed the analyzer a quote, so every rule referencing them
was a permanent non-match. This closes that: the monitors call
`prefetch(symbol)` the moment a filing is detected, so the quote is
fetched in parallel with the PDF download and the LLM call, and
`get_quote(symbol)` reads whatever finished.

Why it matters, measured on this bot's own corpus (6,861 rows with a
complete reaction window): a symbol that had already moved >2% in the
five minutes BEFORE the filing went on to average **-0.626%** over the
next five, against roughly flat for every other bucket — and its MFE was
the *lowest* of the four despite being the most volatile group. When the
move already happened, what is left is the give-back. A rule cannot
avoid that without knowing the day's change, which is what this supplies.

Design, deliberately the same shape as `pdf_cache` (it is the pattern
this project already proved):

  - Module-level dict keyed by symbol: {symbol: (created_monotonic, task)}.
    Monitors and the analyzer share one event loop, so no lock is needed.
  - TTL_SECONDS is short. A stale price is worse than no price: the whole
    point is "how far has this already run TODAY", and a five-minute-old
    quote answers a different question than the one being asked.
  - `get_quote` is SYNC and never waits at all: it returns the prefetch
    result only if the task has already finished. That is the difference
    from `pdf_cache.get_pdf`, and it is deliberate — the PDF is worth
    blocking for, a filter field is not. The rules run on a sync path
    anyway, and a REST quote (~200 ms, started at detection) is
    comfortably done before the LLM returns (~2.4 s). A quote that is
    not back leaves the fields absent, which the engine treats as a
    fail-safe non-match.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from app.logging_config import get_logger

log = get_logger(__name__)

TTL_SECONDS = 60.0      # "how far has it run today" goes stale fast
MAX_ENTRIES = 64
# A filing burst must not turn into a quote-API burst. Fyers rate-limits,
# and a 429 here would also cost the entry path its own quotes.
_fetch_semaphore = asyncio.Semaphore(3)

_cache: dict[str, tuple[float, "asyncio.Task[Optional[dict[str, Any]]]"]] = {}


def _evict() -> None:
    """Drop expired entries; if still over cap, drop oldest first."""
    now = time.monotonic()
    for s in [s for s, (t, _task) in _cache.items() if now - t > TTL_SECONDS]:
        _cache.pop(s, None)
    while len(_cache) > MAX_ENTRIES:
        _cache.pop(min(_cache, key=lambda s: _cache[s][0]), None)


async def _fetch(symbol: str) -> Optional[dict[str, Any]]:
    """One Fyers REST quote. Fyers is the only price source in this
    system by design — see the fyers-only-pricing invariant — so this
    deliberately has no fallback feed."""
    async with _fetch_semaphore:
        try:
            from app.api.market import fetch_quote
            from app.execution.symbols import resolve_fyers_symbol

            return await fetch_quote(resolve_fyers_symbol(symbol) or symbol)
        except Exception:  # noqa: BLE001 — an absent quote is a valid outcome
            log.debug("quote_cache.fetch_failed", symbol=symbol)
            return None


def prefetch(symbol: Optional[str]) -> None:
    """Fire-and-forget quote fetch. Deduped by symbol; never raises."""
    if not symbol:
        return
    sym = symbol.upper().strip()
    try:
        if sym in _cache:
            return
        _evict()
        task = asyncio.get_running_loop().create_task(
            _fetch(sym), name="quote-prefetch"
        )
        task.add_done_callback(
            lambda t: t.exception() if not t.cancelled() else None
        )
        _cache[sym] = (time.monotonic(), task)
    except RuntimeError:
        # No running loop (sync context) — skip. get_quote returns None
        # and the rule fields stay absent.
        pass


def get_quote(symbol: Optional[str]) -> Optional[dict[str, Any]]:
    """The prefetched quote, or None. Starts nothing and waits for
    nothing — this runs inside the signal hot path, where a blocking
    call would cost more than the filter is worth."""
    if not symbol:
        return None
    sym = symbol.upper().strip()
    entry = _cache.get(sym)
    if entry is None:
        return None
    created, task = entry
    if time.monotonic() - created > TTL_SECONDS:
        _cache.pop(sym, None)
        return None
    if not task.done() or task.cancelled():
        return None
    try:
        return task.result()
    except Exception:  # noqa: BLE001 — the fetch itself failed
        return None


def clear() -> None:
    """Test helper."""
    _cache.clear()
