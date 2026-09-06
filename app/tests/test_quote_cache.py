"""Quote prefetch cache — the market-context half of pdf_cache.

The contract that matters: `get_quote` must never block the signal path
and must never hand a rule a stale or half-finished quote.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.analyzer import quote_cache


@pytest.fixture(autouse=True)
def _clean():
    quote_cache.clear()
    yield
    quote_cache.clear()


@pytest.mark.asyncio
async def test_prefetched_quote_is_returned_once_the_fetch_finishes(monkeypatch):
    async def fake(symbol):
        return {"last_price": 101.5, "change_pct": 3.2}

    monkeypatch.setattr(quote_cache, "_fetch", fake)
    quote_cache.prefetch("reliance")
    # Nothing is awaited by get_quote, so before the task runs there is
    # no quote — that is the design, not a bug.
    assert quote_cache.get_quote("RELIANCE") is None
    await asyncio.sleep(0)  # let the prefetch task run
    assert quote_cache.get_quote("RELIANCE") == {"last_price": 101.5, "change_pct": 3.2}
    # Symbol lookup is case/whitespace insensitive.
    assert quote_cache.get_quote("  reliance ") is not None


@pytest.mark.asyncio
async def test_get_quote_never_blocks(monkeypatch):
    """A fetch that hangs must not hold up the signal path."""
    async def hangs(symbol):
        await asyncio.sleep(30)
        return {"last_price": 1.0}

    monkeypatch.setattr(quote_cache, "_fetch", hangs)
    quote_cache.prefetch("SLOWCO")
    await asyncio.sleep(0)
    t = time.perf_counter()
    assert quote_cache.get_quote("SLOWCO") is None
    assert (time.perf_counter() - t) < 0.05


@pytest.mark.asyncio
async def test_failed_fetch_degrades_to_none(monkeypatch):
    async def boom(symbol):
        raise RuntimeError("fyers down")

    monkeypatch.setattr(quote_cache, "_fetch", boom)
    quote_cache.prefetch("BROKEN")
    await asyncio.sleep(0)
    assert quote_cache.get_quote("BROKEN") is None


@pytest.mark.asyncio
async def test_stale_entry_is_dropped(monkeypatch):
    """A stale price answers a different question than 'how far has this
    run today', so the TTL must evict rather than serve."""
    async def fake(symbol):
        return {"last_price": 10.0}

    monkeypatch.setattr(quote_cache, "_fetch", fake)
    quote_cache.prefetch("OLDCO")
    await asyncio.sleep(0)
    assert quote_cache.get_quote("OLDCO") is not None
    created, task = quote_cache._cache["OLDCO"]
    quote_cache._cache["OLDCO"] = (created - quote_cache.TTL_SECONDS - 1, task)
    assert quote_cache.get_quote("OLDCO") is None
    assert "OLDCO" not in quote_cache._cache


@pytest.mark.asyncio
async def test_prefetch_is_deduped(monkeypatch):
    calls = []

    async def counting(symbol):
        calls.append(symbol)
        return {"last_price": 1.0}

    monkeypatch.setattr(quote_cache, "_fetch", counting)
    for _ in range(5):
        quote_cache.prefetch("DUPE")
    await asyncio.sleep(0)
    assert calls == ["DUPE"]


def test_prefetch_without_an_event_loop_is_a_noop():
    """Sync context (a test harness, a CLI) must not raise."""
    quote_cache.prefetch("NOLOOP")
    assert quote_cache.get_quote("NOLOOP") is None


@pytest.mark.asyncio
async def test_cache_is_capped(monkeypatch):
    async def fake(symbol):
        return {"last_price": 1.0}

    monkeypatch.setattr(quote_cache, "_fetch", fake)
    for i in range(quote_cache.MAX_ENTRIES + 20):
        quote_cache.prefetch(f"SYM{i}")
    await asyncio.sleep(0)
    assert len(quote_cache._cache) <= quote_cache.MAX_ENTRIES + 1


@pytest.mark.asyncio
async def test_quote_reaches_the_rule_context(monkeypatch):
    """End to end through the real enrich helper: a prefetched quote must
    populate the `price` / `change_pct` fields rules gate on."""
    from app.analyzer.rules_engine import enrich_analysis_context, evaluate

    async def fake(symbol):
        return {"last_price": 250.0, "change_pct": 6.4}

    monkeypatch.setattr(quote_cache, "_fetch", fake)
    quote_cache.prefetch("HOTCO")
    await asyncio.sleep(0)

    from types import SimpleNamespace

    ctx = enrich_analysis_context(
        {"symbol": "HOTCO", "event_type": "ORDER_WIN", "recommendation": "BUY"},
        quote=SimpleNamespace(**quote_cache.get_quote("HOTCO")),
    )
    assert ctx["price"] == 250.0
    assert ctx["change_pct"] == 6.4

    # The rule this whole feature exists for: don't chase something that
    # already ran. Measured on 6,861 rows, filings on a symbol already
    # >2% extended averaged -0.63% over the next five minutes.
    dont_chase = [{
        "id": 1, "priority": 1, "enabled": True, "action": "BLOCK",
        "conditions": {"all_of": [{"field": "change_pct", "op": ">", "value": 5.0}]},
    }]
    assert evaluate(ctx, dont_chase).action == "BLOCK"
    # Same rule, calm symbol -> no match -> falls through to the HOLD default.
    calm = enrich_analysis_context(
        {"symbol": "CALMCO", "event_type": "ORDER_WIN"},
        quote=SimpleNamespace(last_price=250.0, change_pct=0.4),
    )
    assert evaluate(calm, dont_chase).action == "HOLD"
    # And with no quote at all the rule must fail SAFE (non-match), not
    # block everything.
    assert evaluate({"symbol": "NOQUOTE"}, dont_chase).action == "HOLD"
