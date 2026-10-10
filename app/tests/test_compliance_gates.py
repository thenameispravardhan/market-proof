"""SEBI-regime execution safeguards: the shared order-rate budget, the
exchange gates (surveillance lists, circuit proximity, F&O ban, LPP) and
the postback flood guard.

The gates default OFF, so the most important property is the first test in
each group: switched off, they change nothing.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest

from app.execution import order_rate_limiter as orl
from app.execution.base import OrderSide, OrderState, OrderType
from app.execution.fyers_live import FyersClient, FyersLiveBackend
from app.execution.manager import Manager
from app.execution.market_data import MarketDataBus
from app.services import exchange_lists as xl


# ---------------------------------------------------------------------------
# Token bucket
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_bucket_allows_a_burst_then_paces():
    clock = FakeClock()
    b = orl.TokenBucket(5, clock=clock)
    assert all(b.try_take() == 0.0 for _ in range(5))
    wait = b.try_take()
    assert wait == pytest.approx(0.2)
    clock.t += 0.2
    assert b.try_take() == 0.0


@pytest.mark.asyncio
async def test_acquire_refuses_past_the_wait_budget():
    b = orl.TokenBucket(2)
    await b.acquire(0.0)
    await b.acquire(0.0)
    with pytest.raises(orl.OrderRateLimited):
        await b.acquire(0.1)            # next token is 0.5 s away
    waited = await b.acquire(1.0)       # within budget: waits, then succeeds
    assert 0.3 < waited < 0.8


def _settings(**over):
    base = dict(ORDER_RATE_LIMIT_PER_SEC=5.0, ORDER_RATE_MAX_WAIT_SECONDS=0.0)
    base.update(over)
    return SimpleNamespace(**base)


def test_buckets_are_per_app_and_off_at_zero(monkeypatch):
    monkeypatch.setattr(orl, "get_settings", lambda: _settings())
    assert orl.bucket_for("A-200") is orl.bucket_for("A-200")
    assert orl.bucket_for("A-200") is not orl.bucket_for("B-200")
    monkeypatch.setattr(orl, "get_settings", lambda: _settings(ORDER_RATE_LIMIT_PER_SEC=0))
    assert orl.bucket_for("A-200") is None


def _backend(calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json={"s": "ok", "id": f"OID{len(calls)}"})

    client = FyersClient(app_id="XC1-200", access_token="tok", transport=httpx.MockTransport(handler))
    return FyersLiveBackend(app_id="XC1-200", access_token="tok", broker_account_id=1, client=client)


@pytest.mark.asyncio
async def test_orders_over_budget_are_rejected_locally_and_never_sent(monkeypatch):
    """Six placements in one instant at 5/s: five reach Fyers, the sixth is
    REJECTED (safe to retry) without an HTTP request."""
    monkeypatch.setattr(orl, "get_settings", lambda: _settings())
    calls: list = []
    be = _backend(calls)
    results = [await be.place_order(signal=None, symbol="NSE:SBIN-EQ", side=OrderSide.BUY,
                                    quantity=1, order_type=OrderType.LIMIT, limit_price=100.0)
               for _ in range(6)]
    assert len(calls) == 5
    assert [r.state for r in results[:5]] == [OrderState.PENDING] * 5
    assert results[5].state == OrderState.REJECTED and "rate-limited" in results[5].error
    # Cancels and modifies draw on the same budget.
    assert await be.cancel_order("OID1") is False
    assert len(calls) == 5


@pytest.mark.asyncio
async def test_limiter_off_sends_everything(monkeypatch):
    monkeypatch.setattr(orl, "get_settings", lambda: _settings(ORDER_RATE_LIMIT_PER_SEC=0))
    calls: list = []
    be = _backend(calls)
    for _ in range(12):
        await be.place_order(signal=None, symbol="NSE:SBIN-EQ", side=OrderSide.BUY, quantity=1,
                             order_type=OrderType.LIMIT, limit_price=100.0)
    assert len(calls) == 12


# ---------------------------------------------------------------------------
# Pure exchange helpers
# ---------------------------------------------------------------------------


def test_symbol_series_and_t2t():
    assert xl.bare_symbol("NSE:RELIANCE-EQ") == "RELIANCE"
    assert xl.bare_symbol("BAJAJ-AUTO") == "BAJAJ"   # documented limitation: only for broker ids
    assert xl.is_t2t("NSE:ABC-BE") and xl.is_t2t("NSE:ABC-BZ")
    assert not xl.is_t2t("NSE:ABC-EQ") and not xl.is_t2t("ABC")


def test_parse_fno_ban_csv_and_report_json():
    text = "Securities in Ban For Trade Date 12-OCT-2026:\n1,RBLBANK\n2,IEX\n"
    assert xl.parse_fno_ban_csv(text) == {"RBLBANK", "IEX"}
    report = {"longterm": {"data": [{"symbol": "aaa", "stage": "I"}]},
              "shortterm": {"data": [{"symbol": "BBB"}, {"name": "x"}]}}
    assert xl.collect_symbols(report) == {"AAA", "BBB"}


def test_lpp_bands_follow_nse_rules():
    assert xl.lpp_band(30, "CE") == (10, 50)                      # <= ₹50: ±₹20
    lo, hi = xl.lpp_band(200, "PE")
    assert (lo, hi) == (pytest.approx(120), pytest.approx(280))   # > ₹50: ±40%
    assert xl.lpp_band(10, "CE")[0] == 0.05                      # never below a tick
    assert xl.lpp_band(1000, "FUT") == (pytest.approx(970), pytest.approx(1030))
    assert xl.lpp_band(25000, "FUT", index_underlying=True) == (pytest.approx(24500), pytest.approx(25500))
    with pytest.raises(ValueError):
        xl.lpp_band(100, "EQ")


def test_circuit_headroom_and_clamp():
    assert xl.circuit_headroom_pct("BUY", 100.0, 102.0, 98.0) == pytest.approx(2.0)
    assert xl.circuit_headroom_pct("SELL", 100.0, 102.0, 97.0) == pytest.approx(3.0)
    assert xl.circuit_headroom_pct("BUY", 100.0, None, 98.0) is None
    assert xl.clamp_to_band(110.23, 90.0, 110.0) == 110.0
    assert xl.clamp_to_band(89.97, 90.02, 110.0) == 90.05        # rounds inward
    assert xl.clamp_to_band(100.0, None, None) == 100.0


# ---------------------------------------------------------------------------
# Pre-entry gate on the news bot
# ---------------------------------------------------------------------------


@pytest.fixture()
def fresh_lists(monkeypatch):
    lists = xl.ExchangeLists()
    monkeypatch.setattr(xl, "lists", lists)
    monkeypatch.setattr(lists, "refresh_in_background", lambda: None)
    return lists


def _gate_settings(**over):
    base = dict(GATE_SURVEILLANCE_ENABLED=False, GATE_CIRCUIT_PROXIMITY_PCT=0.0)
    base.update(over)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_gates_off_change_nothing(fresh_lists):
    fresh_lists.set_list("asm", ["ACME"])
    mgr = Manager(market_data=MarketDataBus())
    sig = SimpleNamespace(symbol="ACME", action="BUY")
    assert await mgr._exchange_gate(sig, _gate_settings()) is None


@pytest.mark.asyncio
async def test_surveillance_gate_blocks_listed_symbols(fresh_lists, monkeypatch):
    fresh_lists.set_list("asm", ["ACME"])
    fresh_lists.set_list("gsm", ["SHELL"])
    import app.execution.symbols as symbols
    monkeypatch.setattr(symbols, "resolve_fyers_symbol", lambda s: f"NSE:{s}-BE" if s == "TTT" else f"NSE:{s}-EQ")
    mgr = Manager(market_data=MarketDataBus())
    st = _gate_settings(GATE_SURVEILLANCE_ENABLED=True)
    code, why = await mgr._exchange_gate(SimpleNamespace(symbol="ACME", action="BUY"), st)
    assert code == "SURVEILLANCE_LIST" and "ASM" in why
    assert "GSM" in (await mgr._exchange_gate(SimpleNamespace(symbol="SHELL", action="SELL"), st))[1]
    assert "trade-to-trade" in (await mgr._exchange_gate(SimpleNamespace(symbol="TTT", action="BUY"), st))[1]
    assert await mgr._exchange_gate(SimpleNamespace(symbol="CLEAN", action="BUY"), st) is None


@pytest.mark.asyncio
async def test_surveillance_gate_fails_open_without_lists(fresh_lists, monkeypatch):
    import app.execution.symbols as symbols
    monkeypatch.setattr(symbols, "resolve_fyers_symbol", lambda s: f"NSE:{s}-EQ")
    mgr = Manager(market_data=MarketDataBus())
    st = _gate_settings(GATE_SURVEILLANCE_ENABLED=True)
    assert await mgr._exchange_gate(SimpleNamespace(symbol="ACME", action="BUY"), st) is None


class _DepthBackend:
    def __init__(self, lower, upper):
        self.calls = 0
        self.band = (lower, upper)

    async def get_depth(self, symbol):
        self.calls += 1
        return {"lower_circuit": self.band[0], "upper_circuit": self.band[1]}


@pytest.mark.asyncio
async def test_circuit_gate_blocks_buys_near_the_upper_band(fresh_lists, monkeypatch):
    import app.api.market as market
    import app.execution.symbols as symbols
    backend = _DepthBackend(90.0, 101.0)
    monkeypatch.setattr(market, "_fyers_backend", lambda: backend)
    monkeypatch.setattr(symbols, "resolve_fyers_symbol", lambda s: f"NSE:{s}-EQ")
    md = MarketDataBus()
    md.set_quote_sync("ACME", 100.0)
    mgr = Manager(market_data=md)
    st = _gate_settings(GATE_CIRCUIT_PROXIMITY_PCT=2.0)
    code, why = await mgr._exchange_gate(SimpleNamespace(symbol="ACME", action="BUY"), st)
    assert code == "NEAR_CIRCUIT" and "upper" in why
    # A SELL has 10% room to the lower band: allowed. The band is cached.
    assert await mgr._exchange_gate(SimpleNamespace(symbol="ACME", action="SELL"), st) is None
    assert backend.calls == 1


def test_entry_limit_is_clamped_to_a_known_band(fresh_lists, monkeypatch):
    from app.execution import entry_manager
    import app.execution.symbols as symbols
    monkeypatch.setattr(symbols, "resolve_fyers_symbol", lambda s: f"NSE:{s}-EQ")
    fresh_lists._circuits[("NSE:ACME-EQ", fresh_lists._today())] = (90.0, 100.1)
    assert entry_manager._clamp_to_known_band("ACME", 100.2) == 100.1
    assert entry_manager._clamp_to_known_band("ACME", 99.0) == 99.0
    assert entry_manager._clamp_to_known_band("OTHER", 123.456) == 123.456   # band unknown: untouched


# ---------------------------------------------------------------------------
# LPP on manual F&O limit orders
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lpp_refuses_an_option_limit_outside_the_band():
    md = MarketDataBus()
    md.set_quote_sync("NSE:NIFTY25O1424500CE", 30.0)
    mgr = Manager(market_data=md, settings_provider=lambda: SimpleNamespace(GATE_LPP_ENABLED=True))
    with pytest.raises(ValueError, match="price-protection band"):
        await mgr._check_lpp("NSE:NIFTY25O1424500CE", OrderType.LIMIT, 55.0)
    await mgr._check_lpp("NSE:NIFTY25O1424500CE", OrderType.LIMIT, 45.0)      # inside ±₹20
    await mgr._check_lpp("NSE:SBIN-EQ", OrderType.LIMIT, 1.0)                 # equity: not LPP's business
    off = Manager(market_data=md, settings_provider=lambda: SimpleNamespace(GATE_LPP_ENABLED=False))
    await off._check_lpp("NSE:NIFTY25O1424500CE", OrderType.LIMIT, 55.0)


# ---------------------------------------------------------------------------
# Algo Lab F&O ban
# ---------------------------------------------------------------------------


def test_fno_ban_reason(fresh_lists):
    fresh_lists.set_list("fno_ban", ["IEX"])
    assert "ban" in fresh_lists.fno_ban_reason("NSE:IEX-EQ")
    assert fresh_lists.fno_ban_reason("NSE:SBIN-EQ") is None


# ---------------------------------------------------------------------------
# Postback flood guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postback_burst_for_one_order_costs_one_read_plus_one_deferred(monkeypatch):
    from app.api import fyers_postback as fp

    reads: list = []

    async def fake_sync(order_id):
        reads.append((order_id, time.monotonic()))
        return {"ok": True}

    spawned: list = []
    monkeypatch.setattr(fp, "_sync_from_fyers", fake_sync)
    monkeypatch.setattr(fp, "COOLDOWN_S", 0.05)
    monkeypatch.setattr(fp, "spawn", lambda coro: spawned.append(asyncio.ensure_future(coro)))
    for _ in range(20):
        await fp._sync_throttled("OID9")
    assert len(reads) == 1 and len(spawned) == 1
    await asyncio.gather(*spawned)
    assert len(reads) == 2           # the final state is still read once
