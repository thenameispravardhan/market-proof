"""One shared order-rate budget per Fyers app.

SEBI's 2026 retail-algo framework lets a self-built algo skip exchange
registration only while it stays at or under 10 orders per second per
exchange, and Fyers counts placements, modifications and cancels (stop-loss
and target legs included) against that ceiling. The news bot, the Trade
page and the Algo Lab all authenticate as the SAME app, so their order
calls draw on one budget. Each component staying "well under 10/s" alone
is no guarantee once a news burst and an Algo Lab bar close land in the
same second.

A token bucket per app id, refilled at ORDER_RATE_LIMIT_PER_SEC (default
5/s, half the ceiling). A call that finds the bucket empty waits for a
token up to ORDER_RATE_MAX_WAIT_SECONDS. Past that it is refused locally
with a 429-shaped error, so the order paths already map it to "rate-limited,
not placed": safe to retry, and never a duplicate.

The limiter is process-local. The bot runs as a single uvicorn worker, so
that one process is the only thing using the key.
"""
from __future__ import annotations

import asyncio
import time
from typing import Callable, Optional

from app.config import get_settings
from app.logging_config import get_logger

log = get_logger(__name__)


class OrderRateLimited(Exception):
    """No order slot within the wait budget; the request was NOT sent."""


class TokenBucket:
    def __init__(self, rate: float, capacity: Optional[float] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.rate = float(rate)
        self.capacity = float(capacity if capacity is not None else max(1.0, rate))
        self._tokens = self.capacity
        self._clock = clock
        self._last = clock()
        self._lock: Optional[asyncio.Lock] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now

    def try_take(self) -> float:
        """Take a token if one is available. Returns 0.0 on success, else
        the seconds until the next token will exist."""
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return 0.0
        return (1.0 - self._tokens) / self.rate

    def _get_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._loop is not loop:
            self._lock, self._loop = asyncio.Lock(), loop
        return self._lock

    async def acquire(self, max_wait: float) -> float:
        """Wait for a token for at most `max_wait` seconds. Returns how long
        it waited. Raises OrderRateLimited if the wait would be longer."""
        start = self._clock()
        # Serialised so waiters are served in arrival order and two callers
        # never both decide the same future token is theirs.
        async with self._get_lock():
            while True:
                wait = self.try_take()
                if wait == 0.0:
                    return self._clock() - start
                if (self._clock() - start) + wait > max_wait:
                    raise OrderRateLimited(
                        f"order-rate budget ({self.rate:g}/s) exhausted; waited "
                        f"{self._clock() - start:.2f}s of {max_wait:g}s"
                    )
                await asyncio.sleep(wait)


_buckets: dict[str, TokenBucket] = {}


def bucket_for(app_id: str) -> Optional[TokenBucket]:
    """The shared bucket for `app_id`, rebuilt if the configured rate
    changed; None when the limiter is switched off (rate 0)."""
    rate = float(getattr(get_settings(), "ORDER_RATE_LIMIT_PER_SEC", 0.0) or 0.0)
    if rate <= 0:
        return None
    b = _buckets.get(app_id)
    if b is None or b.rate != rate:
        b = TokenBucket(rate)
        _buckets[app_id] = b
    return b


async def acquire_order_slot(app_id: str, what: str) -> None:
    """Block until `app_id` may send one more order-type request."""
    b = bucket_for(app_id or "_")
    if b is None:
        return
    max_wait = float(getattr(get_settings(), "ORDER_RATE_MAX_WAIT_SECONDS", 1.0) or 0.0)
    try:
        waited = await b.acquire(max_wait)
    except OrderRateLimited:
        log.warning("order_rate.refused", app_id=app_id, what=what, rate=b.rate)
        raise
    if waited > 0.05:
        log.info("order_rate.delayed", app_id=app_id, what=what, waited_s=round(waited, 3))


def reset() -> None:
    """Tests only."""
    _buckets.clear()
