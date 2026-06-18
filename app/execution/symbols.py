"""Symbol resolution — short name <-> full Fyers broker id.

The news pipeline and the managed-position book key everything by a bare
short name (e.g. ``THOMASCOOK``). The Fyers REST quote endpoint AND the
realtime data WebSocket both need a *full* broker id (``NSE:THOMASCOOK-EQ``).

`resolve_fyers_symbol` centralises that lookup (instrument-master search,
prefer the NSE EQ listing — the primary / most liquid one) so the quote
feed (`app.main._real_quote`) and the streaming feed
(`app.execution.fyers_stream`) resolve identically. It is deliberately
side-effect free and swallows lookup failures (returns None) so callers
degrade gracefully when the master isn't loaded.

Caching: the reconcile loop in `FyersStreamManager` calls this every 2s
per watched symbol, which would re-search the instrument master every
time. The lookup result is stable for the lifetime of the process
(master reloads only happen via the manual `download_masters` admin
script), so an `lru_cache` is a safe and useful optimization. Failures
(`get_master()` raising or the search returning no hits) are cached as
`None` — that's fine because once the master is loaded it stays loaded
for the whole process lifetime, and the very first lookup is what
populates the seed anyway.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

from app.logging_config import get_logger

log = get_logger(__name__)


def resolve_fyers_symbol(symbol: str) -> Optional[str]:
    """Resolve a bare short name to a full Fyers broker id.

    - If `symbol` already looks like a full broker id (contains ``:``),
      it's returned upper-cased unchanged.
    - Otherwise we search the instrument master for an EQ match,
      preferring the exact short-name hit on NSE, then any exact hit,
      then the first fuzzy hit.
    - Returns None when nothing resolves (caller keeps the bare name).

    Result is cached per short-name. Use `clear_resolution_cache()` if
    you've swapped the instrument master mid-process (e.g. in tests).
    """
    if not symbol:
        return None
    sym = symbol.strip().upper()
    if ":" in sym:
        return sym
    return _resolve_via_master(sym)


@lru_cache(maxsize=512)
def _resolve_via_master(sym: str) -> Optional[str]:
    """Cached instrument-master lookup. See module docstring for why
    failures are cached (the master loads once and stays loaded)."""
    try:
        from app.services.instrument_master import get_master

        hits = get_master().search(sym, limit=8, segments=["EQ"])
    except Exception as e:  # noqa: BLE001
        log.debug("symbols.resolve_failed", symbol=sym, error=str(e))
        return None
    if not hits:
        return None
    exact = [h for h in hits if h.short_name == sym]
    pick = (
        next((h for h in exact if h.exchange == "NSE"), None)
        or (exact[0] if exact else None)
        or hits[0]
    )
    return pick.symbol if pick is not None else None


def clear_resolution_cache() -> None:
    """Drop the resolution cache. Useful in tests that monkeypatch the
    instrument master — without this, a prior test's resolved value is
    served from the cache and the patched master never gets consulted."""
    _resolve_via_master.cache_clear()