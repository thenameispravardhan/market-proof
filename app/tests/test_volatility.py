"""Tests for app/risk/volatility.py — ATR math, stop distance, providers."""
from __future__ import annotations

import pytest

from app.risk import volatility


# -- compute_atr ----------------------------------------------------------


def test_compute_atr_constant_range():
    # Every candle has a true range of 10 (high-low=10, no gaps) → ATR=10.
    candles = [(100.0 + i, 90.0 + i, 95.0 + i) for i in range(20)]
    atr = volatility.compute_atr(candles, period=14)
    assert atr is not None
    assert abs(atr - 10.0) < 1e-6


def test_compute_atr_insufficient_data_returns_none():
    candles = [(100.0, 90.0, 95.0), (101.0, 91.0, 96.0)]
    assert volatility.compute_atr(candles, period=14) is None


def test_compute_atr_accepts_fyers_rows():
    # Fyers history row: [ts, open, high, low, close, volume].
    rows = [[i, 95.0, 100.0 + i, 90.0 + i, 95.0 + i, 1000] for i in range(20)]
    atr = volatility.compute_atr(rows, period=14)
    assert atr is not None and atr > 0


def test_compute_atr_skips_malformed():
    candles = [None, "junk", (100.0, 90.0, 95.0)] + [
        (100.0 + i, 90.0 + i, 95.0 + i) for i in range(20)
    ]
    atr = volatility.compute_atr(candles, period=5)
    assert atr is not None and atr > 0


# -- stop_distance --------------------------------------------------------


def _dist(**kw):
    base = dict(
        entry=1000.0, mult=2.0, default_pct=6.0, min_pct=1.0,
        max_pct=8.0, smallcap_price=200.0, smallcap_pct=1.5,
    )
    base.update(kw)
    return volatility.stop_distance(**base)


def test_stop_distance_uses_atr_when_present():
    # ATR=10, mult=2 → 20, inside [1%,8%] of 1000 = [10,80]. → 20.
    assert _dist(atr=10.0) == 20.0


def test_stop_distance_clamps_to_max():
    # ATR=100, mult=2 → 200, capped at 8% of 1000 = 80.
    assert _dist(atr=100.0) == 80.0


def test_stop_distance_clamps_to_floor():
    # ATR tiny → 0.5, floored at 1% of 1000 = 10.
    assert _dist(atr=0.25) == 10.0


def test_stop_distance_falls_back_to_pct_when_no_atr():
    # No ATR → default_pct 6% of 1000 = 60.
    assert _dist(atr=None) == 60.0


def test_stop_distance_smallcap_floor():
    # Sub-200 name uses the 1.5% floor. entry=100, atr None, default 3%.
    d = volatility.stop_distance(
        entry=100.0, atr=None, mult=2.0, default_pct=3.0, min_pct=1.0,
        max_pct=8.0, smallcap_price=200.0, smallcap_pct=1.5,
    )
    assert d == 3.0  # 3% of 100, above the 1.5% floor


# -- providers ------------------------------------------------------------


def test_null_provider_returns_none():
    assert volatility.NullVolatilityProvider().atr("RELIANCE") is None
    assert volatility.resolve_atr(volatility.NullVolatilityProvider(), "X") is None
    assert volatility.resolve_atr(None, "X") is None


def test_tick_window_provider_estimates_after_enough_ticks():
    p = volatility.TickWindowVolatilityProvider(window=20, min_ticks=5)
    assert p.atr("ABC") is None  # no data yet
    for px in (100, 102, 101, 103, 102, 104):
        p.record("ABC", px)
    est = p.atr("ABC")
    assert est is not None and est > 0


def test_tick_window_provider_ignores_bad_prices():
    p = volatility.TickWindowVolatilityProvider(min_ticks=3)
    p.record("ABC", 0)
    p.record("ABC", -5)
    assert p.atr("ABC") is None


# -- VIX regime -----------------------------------------------------------


def test_vix_multiplier_none_is_noop():
    assert volatility.vix_risk_multiplier(None) == 1.0


def test_vix_multiplier_regimes():
    assert volatility.vix_risk_multiplier(12.0) == 1.0
    assert volatility.vix_risk_multiplier(20.0) == 0.75
    assert volatility.vix_risk_multiplier(30.0) == 0.5


def test_null_regime_returns_none():
    assert volatility.NullVolatilityRegime().india_vix() is None


# -- Fyers candle ATR provider (Phase 5) ---------------------------------


def _flat_candles(n=20, high=110.0, low=90.0, close=100.0):
    return [(high, low, close) for _ in range(n)]


@pytest.mark.asyncio
async def test_fyers_candle_provider_warms_then_serves():
    calls = {"n": 0}

    async def fetch(symbol, resolution, days):
        calls["n"] += 1
        return _flat_candles()

    p = volatility.FyersCandleVolatilityProvider(fetch, period=14)
    assert p.atr("RELIANCE") is None  # cold
    await p.refresh("RELIANCE")
    atr = p.atr("RELIANCE")
    assert atr is not None and abs(atr - 20.0) < 1e-6  # flat TR = 20
    # Within TTL → no re-fetch.
    await p.refresh("RELIANCE")
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_fyers_candle_provider_failsafe_on_error():
    async def fetch(symbol, resolution, days):
        raise RuntimeError("broker down")

    p = volatility.FyersCandleVolatilityProvider(fetch)
    await p.refresh("RELIANCE")
    assert p.atr("RELIANCE") is None  # error → no ATR → % fallback


@pytest.mark.asyncio
async def test_fyers_candle_provider_short_series_is_none():
    async def fetch(symbol, resolution, days):
        return [(110.0, 90.0, 100.0)]  # too few for period

    p = volatility.FyersCandleVolatilityProvider(fetch, period=14)
    await p.refresh("RELIANCE")
    assert p.atr("RELIANCE") is None


# -- Fyers VIX regime (Phase 5) ------------------------------------------


@pytest.mark.asyncio
async def test_fyers_vix_regime_warms_then_serves():
    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        return 22.5

    r = volatility.FyersVolatilityRegime(fetch)
    assert r.india_vix() is None
    await r.refresh()
    assert r.india_vix() == 22.5
    await r.refresh()  # within TTL
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_fyers_vix_regime_failsafe_on_error():
    async def fetch():
        raise RuntimeError("no quote")

    r = volatility.FyersVolatilityRegime(fetch)
    await r.refresh()
    assert r.india_vix() is None


# -- Fyers backend get_history parse -------------------------------------


@pytest.mark.asyncio
async def test_backend_get_history_returns_candles():
    from app.execution.fyers_live import FyersLiveBackend

    class _StubClient:
        app_id = "APP"
        access_token = "tok"

        async def get_history(self, symbol, *, resolution, range_from, range_to,
                              date_format=1, cont_flag=1):
            return {"candles": [[1, 100, 110, 90, 100, 1000]] * 20}

    backend = FyersLiveBackend(
        app_id="APP", access_token="tok", broker_account_id=1,
        client=_StubClient(),
    )
    candles = await backend.get_history("NSE:RELIANCE-EQ", resolution="5", days=5)
    assert len(candles) == 20
    # And compute_atr can consume the Fyers rows directly.
    assert volatility.compute_atr(candles, period=14) is not None


@pytest.mark.asyncio
async def test_backend_get_history_failsafe_on_api_error():
    from app.execution.fyers_live import FyersAPIError, FyersLiveBackend

    class _BoomClient:
        app_id = "APP"
        access_token = "tok"

        async def get_history(self, *a, **k):
            raise FyersAPIError("boom", status_code=500)

    backend = FyersLiveBackend(
        app_id="APP", access_token="tok", broker_account_id=1,
        client=_BoomClient(),
    )
    assert await backend.get_history("NSE:RELIANCE-EQ") == []
