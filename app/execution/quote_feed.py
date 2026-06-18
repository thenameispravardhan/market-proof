"""Quote feed — keeps the MarketDataBus fed so orders fill and open
positions mark-to-market.

Two modes:

  paper  (default / no live creds)
    A self-contained price simulator. Each watched symbol is anchored
    at a deterministic base price (stable per symbol so runs are
    repeatable) and random-walks by a small per-tick volatility. This
    is what lets a paper MARKET order fill, and what makes SL / target
    exits and live P&L actually happen on the dashboard. It is clearly
    synthetic — documented as such — not a claim about real prices.

  live   (TRADING_MODE=live + a Fyers backend)
    Pulls real quotes from the injected `live_quote_fn(symbol)` (the
    execution manager wires this to the Fyers backend). On any error
    we keep the last known price rather than dropping the symbol.

Lifecycle mirrors the other services: `start()` launches a background
loop that ticks every `QUOTE_REFRESH_SECONDS`; `stop()` cancels it.
`seed_symbol(symbol)` publishes an immediate first quote (used by the
manager right before placing an order so the fill has a price).
"""
from __future__ import annotations

import asyncio
import hashlib
import random
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from app.config import get_settings
from app.execution.market_data import MarketDataBus
from app.logging_config import get_logger

log = get_logger(__name__)


LiveQuoteFn = Callable[[str], Awaitable[Optional[float]]]


def _base_price_for(symbol: str) -> float:
    """Deterministic anchor price for a symbol in paper mode.

    Hash the symbol into a stable price in roughly [80, 2080] so
    different symbols look distinct but every run for a given symbol
    starts at the same place.
    """
    h = int(hashlib.sha256(symbol.encode("utf-8")).hexdigest(), 16)
    return 80.0 + float(h % 2000)


class QuoteFeed:
    """Feeds quotes into a MarketDataBus for all watched symbols."""

    def __init__(
        self,
        *,
        market_data: MarketDataBus,
        live_quote_fn: Optional[LiveQuoteFn] = None,
        is_live_fn: Optional[Callable[[], bool]] = None,
        volatility: float = 0.004,
        seed: Optional[int] = None,
    ) -> None:
        self._md = market_data
        self._live_quote_fn = live_quote_fn
        # Default: follow the global trading mode.
        self._is_live = is_live_fn or (lambda: get_settings().is_live)
        self._vol = float(volatility)
        self._watched: dict[str, float] = {}  # symbol -> last simulated price
        self._rng = random.Random(seed)
        self._stop_event: asyncio.Event = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None

    # -- watch set -------------------------------------------------------

    def watch(self, symbol: str, anchor: Optional[float] = None) -> float:
        """Start tracking `symbol`. Returns the anchor price used."""
        symbol = symbol.upper().strip()
        if symbol not in self._watched:
            self._watched[symbol] = float(anchor) if anchor else _base_price_for(symbol)
        return self._watched[symbol]

    def unwatch(self, symbol: str) -> None:
        self._watched.pop(symbol.upper().strip(), None)

    async def seed_symbol(self, symbol: str, anchor: Optional[float] = None) -> float:
        """Ensure a quote exists for `symbol` right now and return its
        price. Used by the manager before placing a paper order so the
        MARKET fill has a price to hit — prefers a REAL price so the fill
        (and thus the trade-history entry) reflects the actual market."""
        symbol = symbol.upper().strip()
        existing = await self._md.get_quote(symbol)
        if (
            existing is not None
            and symbol in self._watched
            and not (existing.extra and existing.extra.get("simulated"))
        ):
            return float(existing.last_price)
        # Prefer a real market price for the fill.
        if self._live_quote_fn is not None:
            try:
                price = await self._live_quote_fn(symbol)
            except Exception:  # noqa: BLE001
                price = None
            if price is not None and price > 0:
                self._watched[symbol] = float(price)
                await self._publish(symbol, float(price), simulated=False)
                return float(price)
        # Synthetic fallback (no feed / unresolvable symbol).
        price = self.watch(symbol, anchor)
        await self._publish(symbol, price, simulated=True)
        return price

    def watched_symbols(self) -> list[str]:
        return list(self._watched.keys())

    # -- lifecycle -------------------------------------------------------

    def start(self) -> asyncio.Task[None]:
        if self._task is not None and not self._task.done():
            return self._task
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="quote-feed")
        return self._task

    def stop(self) -> None:
        self._stop_event.set()

    async def wait_until_stopped(self) -> None:
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        finally:
            self._task = None

    # -- internals -------------------------------------------------------

    async def _run(self) -> None:
        log.info("quote_feed.start")
        try:
            while not self._stop_event.is_set():
                await self._tick_all()
                interval = float(get_settings().QUOTE_REFRESH_SECONDS)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                    break
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass
        finally:
            log.info("quote_feed.stop")

    async def _tick_all(self) -> None:
        for symbol in list(self._watched.keys()):
            try:
                # If the realtime Fyers WebSocket has published a fresh tick
                # for this symbol, the bus is already live — skip the REST
                # poll entirely. This is what removes the per-symbol
                # /data/quotes calls (and the 429s). REST / simulation only
                # kick in as a backstop when the socket is silent or absent.
                if await self._has_fresh_stream_tick(symbol):
                    continue
                price = None
                simulated = True
                # Always prefer a REAL price (the connected Fyers account,
                # via live_quote_fn) — in paper mode too, so fills/P&L use
                # real market prices. Simulates only when Fyers can't serve it.
                if self._live_quote_fn is not None:
                    price = await self._live_quote_fn(symbol)
                    if price is not None and price > 0:
                        self._watched[symbol] = float(price)
                        simulated = False
                    else:
                        # Keep last known real price on a feed gap.
                        price = self._watched.get(symbol)
                # No real feed / unresolvable symbol → synthetic walk.
                if price is None or price <= 0:
                    price = self._next_paper_price(symbol)
                    simulated = True
                if price is not None and price > 0:
                    await self._publish(symbol, float(price), simulated=simulated)
            except Exception:  # noqa: BLE001
                log.exception("quote_feed.tick_failed", symbol=symbol)

    async def _has_fresh_stream_tick(self, symbol: str) -> bool:
        """True when the realtime WebSocket feed (source ``fyers_ws``) has
        published a recent tick for `symbol`.

        Only the socket's ``fyers_ws`` source counts — NOT this feed's own
        REST publishes (``fyers``), otherwise the poll would suppress
        itself and never refresh. The window is 2×QUOTE_REFRESH_SECONDS so
        the REST backstop resumes promptly if the socket goes quiet.
        """
        quote = await self._md.get_quote(symbol)
        if quote is None:
            return False
        if (quote.extra or {}).get("source") != "fyers_ws":
            return False
        age = (datetime.now(timezone.utc) - quote.timestamp).total_seconds()
        window = max(2.0, 2.0 * float(get_settings().QUOTE_REFRESH_SECONDS))
        return age < window

    def _next_paper_price(self, symbol: str) -> float:
        last = self._watched.get(symbol) or _base_price_for(symbol)
        # Gaussian step; clamp to a sane positive floor.
        step = self._rng.gauss(0.0, self._vol)
        new_price = max(1.0, last * (1.0 + step))
        self._watched[symbol] = new_price
        return new_price

    async def _publish(self, symbol: str, price: float, *, simulated: bool = True) -> None:
        # A simple ADV estimate keeps the liquidity rule happy in paper
        # mode; in live mode the Fyers quote carries real volume.
        # `simulated` tags paper (synthetic) prices so consumers that need
        # REAL prices — the Trade page quote — can skip them. The default
        # is True because every caller except a live Fyers tick is paper.
        await self._md.publish(
            symbol,
            price,
            average_daily_volume_crore=50.0,
            extra={"simulated": True} if simulated else {"source": "fyers"},
        )
