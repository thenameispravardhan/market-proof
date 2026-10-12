import asyncio

import app.api.market as market
import app.execution.symbols as symbols
from app.risk import engine


def test_warm_adv_asks_fyers_with_the_broker_symbol(monkeypatch):
    seen = {}

    async def fake_history(sym, resolution="5", days=5):
        seen["sym"] = sym
        return [[0, 0, 0, 0, 100.0, 2_000_000]] * 20     # close 100 x 20 lakh shares = Rs 20 cr/day

    monkeypatch.setattr(market, "fetch_history", fake_history)
    monkeypatch.setattr(symbols, "resolve_fyers_symbol", lambda s: f"NSE:{s}-EQ")
    engine._ADV_CACHE.clear()
    assert asyncio.run(engine.warm_adv("lumino")) == 20.0
    assert seen["sym"] == "NSE:LUMINO-EQ"
    assert engine.cached_adv("LUMINO") == 20.0           # cache stays keyed by the bare symbol
