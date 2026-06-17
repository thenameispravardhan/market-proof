"""In-process market data bus for the paper backend.

The paper backend needs last-price / quote data to trigger pending
SL and LIMIT orders. We don't have a real price feed in v1; the bus
is fed by:

    1. A `set_quote(symbol, last_price, ...)` call from a backtest /
       smoke harness, or
    2. The live Fyers quote endpoint (live backend only), or
    3. A `tick(symbol, last_price)` convenience used in tests.

A `MarketDataBus` exposes:
    - `get_quote(symbol)` -> Optional[Quote]: last cached quote.
    - `subscribe(symbol)` -> asyncio.Queue[Quote]: subscribers
      receive every subsequent tick for that symbol.
    - `publish(symbol, last_price, ...)` / `tick(symbol, last_price)`:
      update the cache and fan out to subscribers.

This is intentionally minimal. T5+ may add a real WebSocket feed;
the bus is the single integration point.

Concurrency: the cache is a plain dict protected by a single
`asyncio.Lock` — the volume is low (one event per quote per second)
and the operations are O(1). No need for a queue + heap here.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass
class Quote:
    """A snapshot of a symbol's market data.

    Fields the risk engine uses:
        - `last_price` for sizing math (entry - stop_loss).
        - `average_daily_volume_crore` for the `min_liquidity_crore`
          rule. Optional — risk engine treats None as "unavailable,
          warn and allow" per the spec.
    """
    symbol: str
    last_price: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    volume: Optional[int] = None
    average_daily_volume_crore: Optional[float] = None
    # Day change vs previous close (absolute + percent) and the prev
    # close itself. Populated by the live Fyers feed; the status-bar
    # index ticker renders change_pct. None when the source omits them.
    change: Optional[float] = None
    change_pct: Optional[float] = None
    prev_close: Optional[float] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    extra: dict[str, Any] = field(default_factory=dict)


class MarketDataBus:
    """Last-price / quote cache + per-symbol subscriber queues."""

    def __init__(self) -> None:
        self._quotes: dict[str, Quote] = {}
        self._subs: dict[str, set[asyncio.Queue[Quote]]] = {}
        self._lock = asyncio.Lock()

    # -- producer side ---------------------------------------------------

    async def publish(
        self,
        symbol: str,
        last_price: float,
        *,
        bid: Optional[float] = None,
        ask: Optional[float] = None,
        volume: Optional[int] = None,
        average_daily_volume_crore: Optional[float] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> Quote:
        """Update the cache and notify all subscribers for `symbol`.

        Returns the new Quote.
        """
        q = Quote(
            symbol=symbol,
            last_price=float(last_price),
            bid=bid,
            ask=ask,
            volume=volume,
            average_daily_volume_crore=average_daily_volume_crore,
            extra=dict(extra or {}),
        )
        async with self._lock:
            self._quotes[symbol] = q
            subs = list(self._subs.get(symbol, ()))
        for queue in subs:
            try:
                queue.put_nowait(q)
            except asyncio.QueueFull:
                # Slow consumer — drop. Better to drop a tick than
                # block the publisher.
                pass
        return q

    async def tick(self, symbol: str, last_price: float) -> Quote:
        """Convenience: `publish` with a single last_price argument."""
        return await self.publish(symbol, last_price)

    # -- consumer side ---------------------------------------------------

    async def get_quote(self, symbol: str) -> Optional[Quote]:
        async with self._lock:
            return self._quotes.get(symbol)

    def subscribe(self, symbol: str, maxsize: int = 1000) -> asyncio.Queue[Quote]:
        """Subscribe to ticks for `symbol`. Caller is responsible for
        calling `unsubscribe` when done."""
        q: asyncio.Queue[Quote] = asyncio.Queue(maxsize=maxsize)
        self._subs.setdefault(symbol, set()).add(q)
        return q

    def unsubscribe(self, symbol: str, queue: asyncio.Queue[Quote]) -> None:
        subs = self._subs.get(symbol)
        if not subs:
            return
        subs.discard(queue)
        if not subs:
            self._subs.pop(symbol, None)

    def symbols(self) -> list[str]:
        return list(self._quotes.keys())

    # -- testing helpers -------------------------------------------------

    def set_quote_sync(self, symbol: str, last_price: float, **kwargs: Any) -> Quote:
        """Synchronous cache write — no subscribers notified. Tests
        use this to seed deterministic quotes without spinning up
        an event loop."""
        q = Quote(symbol=symbol, last_price=float(last_price), **kwargs)
        self._quotes[symbol] = q
        return q

    def reset(self) -> None:
        """Drop all cached quotes and subscribers. Used in tests
        between cases."""
        self._quotes.clear()
        self._subs.clear()
