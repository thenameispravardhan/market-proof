"""Tests for the MarketDataBus → /ws quote-sink bridge.

The sink is how every price tick reaches the dashboard in real time
(`main.py` wires it to `event_bus.publish("quotes", ...)`). These cover
the seam itself: a registered sink is awaited once per publish with the
new Quote, a sink error never breaks the local feed, and no sink is a
no-op.
"""
from __future__ import annotations

import pytest

from app.execution.market_data import MarketDataBus


@pytest.mark.asyncio
async def test_sink_receives_each_published_quote():
    md = MarketDataBus()
    seen: list[tuple[str, float, str | None]] = []

    async def sink(q):
        seen.append((q.symbol, q.last_price, (q.extra or {}).get("source")))

    md.set_quote_sink(sink)
    await md.publish("SBIN", 543.2, extra={"source": "fyers_ws"})
    await md.publish("TCS", 3900.0)

    assert seen == [("SBIN", 543.2, "fyers_ws"), ("TCS", 3900.0, None)]


@pytest.mark.asyncio
async def test_sink_error_does_not_break_feed():
    """A broken sink must not stop the cache update or local subscribers —
    the price feed fills orders and marks P&L and can't depend on the /ws
    bridge being healthy."""
    md = MarketDataBus()

    async def boom(_q):
        raise RuntimeError("ws bridge down")

    md.set_quote_sink(boom)
    q = await md.publish("INFY", 1500.0)  # must not raise

    assert q.last_price == 1500.0
    assert (await md.get_quote("INFY")).last_price == 1500.0


@pytest.mark.asyncio
async def test_no_sink_is_a_noop():
    md = MarketDataBus()
    await md.publish("WIPRO", 410.0)  # no sink wired → just caches
    assert (await md.get_quote("WIPRO")).last_price == 410.0
