"""The exhaustive stop-loss & target management suite (plan T-01 .. T-30).

Categories:
  1. Standard exits            T-01, T-02, T-03
  2. Time & consolidation      T-04, T-05, T-06
  3. Trailing stop             T-07, T-08, T-09
  4. Slippage & gaps           T-10, T-11, T-12, T-13
  5. Concurrency & recovery    T-14, T-15, T-16
  6. Stop/target calculation   T-17, T-18, T-19, T-20
  7. Scale-out                 T-21, T-22, T-23
  8. Broker/network anomalies  T-24, T-25, T-26, T-27
  9. Risk & sizing             T-28, T-29, T-30

Exit paths are exercised for BOTH modes: paper (direct settle — the bot
is the paper broker) and live (broker-routed marketable-limit → market
fallback, via a stub Fyers-style backend). A realtime test at the end
verifies exits fire from a MarketDataBus tick (the Fyers WS path)
without waiting for a sweep.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from app.db.models import Position as PositionRow, RiskEvent, Trade as TradeRow
from app.execution.base import (
    OrderResult,
    OrderSide,
    OrderState,
    OrderStatus,
    OrderType,
    Position,
)
from app.execution.market_data import MarketDataBus, Quote
from app.execution.trade_manager import ManagedPosition, TradeManager
from app.risk import volatility
from app.risk.engine import RiskEngine
from app.services.event_bus import event_bus


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _fast_exit_settings(monkeypatch):
    """Millisecond-scale broker windows so the retry/fallback logic runs
    fast, and generous defaults everywhere else."""
    monkeypatch.setenv("EXIT_BROKER_TIMEOUT_SECONDS", "0.25")
    monkeypatch.setenv("EXIT_FILL_TIMEOUT_SECONDS", "0.3")
    monkeypatch.setenv("EXIT_RETRY_MAX", "2")
    from app import config as app_config

    app_config.reset_settings_cache()
    yield
    app_config.reset_settings_cache()


# ---- helpers ---------------------------------------------------------------


def make_quote(
    price: float,
    *,
    symbol: str = "RELIANCE",
    bid: Optional[float] = None,
    ask: Optional[float] = None,
    ts: Optional[datetime] = None,
    source: Optional[str] = None,
) -> Quote:
    q = Quote(
        symbol=symbol,
        last_price=float(price),
        bid=bid,
        ask=ask,
        extra={"source": source} if source else {},
    )
    if ts is not None:
        q.timestamp = ts
    return q


def exit_filled(kw: dict[str, Any], *, price: Optional[float] = None,
                qty: Optional[int] = None, order_id: str = "EXIT-1") -> OrderResult:
    return OrderResult(
        broker_order_id=order_id,
        state=OrderState.FILLED,
        symbol=kw["symbol"],
        side=kw["side"],
        quantity=int(kw["quantity"]),
        order_type=kw["order_type"],
        filled_quantity=int(qty if qty is not None else kw["quantity"]),
        average_price=float(price if price is not None else (kw.get("limit_price") or 0.0)),
        limit_price=kw.get("limit_price"),
    )


def exit_pending(kw: dict[str, Any], *, order_id: str = "EXIT-1") -> OrderResult:
    return OrderResult(
        broker_order_id=order_id,
        state=OrderState.PENDING,
        symbol=kw["symbol"],
        side=kw["side"],
        quantity=int(kw["quantity"]),
        order_type=kw["order_type"],
        limit_price=kw.get("limit_price"),
    )


def exit_rejected(kw: dict[str, Any], *, error: str = "circuit filter") -> OrderResult:
    return OrderResult(
        broker_order_id="",
        state=OrderState.REJECTED,
        symbol=kw["symbol"],
        side=kw["side"],
        quantity=int(kw["quantity"]),
        order_type=kw["order_type"],
        error=error,
    )


class ExitStubBackend:
    """Programmable live-style backend for exit routing."""

    name = "stub-live"
    broker_account_id = 1

    def __init__(self) -> None:
        self.orders: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.place_delay = 0.0
        self._place: list[Any] = []
        self._statuses: list[OrderStatus] = []

    def queue_place(self, handler: Any) -> None:
        self._place.append(handler)

    def queue_status(self, status: OrderStatus) -> None:
        self._statuses.append(status)

    async def place_order(self, **kw: Any) -> OrderResult:
        self.orders.append(dict(kw))
        if self.place_delay:
            await asyncio.sleep(self.place_delay)
        assert self._place, "no place_order handler queued"
        h = self._place.pop(0)
        return h(dict(kw)) if callable(h) else h

    async def cancel_order(self, broker_order_id: str) -> bool:
        self.cancelled.append(broker_order_id)
        return True

    async def get_order_status(self, broker_order_id: str) -> OrderStatus:
        assert self._statuses, "no get_order_status response queued"
        if len(self._statuses) > 1:
            return self._statuses.pop(0)
        return self._statuses[0]

    async def get_positions(self) -> list[Position]:
        return []


def make_account(db_session) -> int:
    """A real broker_accounts row — settle writes carry its id as an FK."""
    from app.db.models import BrokerAccount

    acct = BrokerAccount(name="live", broker="fyers", paper_mode=False, enabled=True)
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct.id


def make_tm(
    md: MarketDataBus,
    *,
    backend: Optional[ExitStubBackend] = None,
    live_positions: Any = None,
    quote_feed: Any = None,
) -> TradeManager:
    exit_fn = None
    if backend is not None:
        async def exit_fn(acc_id):  # noqa: ANN001
            return backend if acc_id is not None else None
    live_fn = None
    if live_positions is not None:
        async def live_fn():
            return live_positions() if callable(live_positions) else live_positions
    return TradeManager(
        market_data=md,
        quote_feed=quote_feed,
        exit_backend_fn=exit_fn,
        live_positions_fn=live_fn,
    )


async def register(
    tm: TradeManager,
    *,
    symbol: str = "RELIANCE",
    qty: int = 1000,
    entry: float = 100.0,
    stop: Optional[float] = 97.0,
    target: Optional[float] = None,
    account_id: Optional[int] = None,
    max_hold: int = 100_000,
) -> ManagedPosition:
    await tm.register(
        symbol=symbol, quantity=qty, entry=entry, stop_loss=stop,
        target=target, broker_account_id=account_id,
        max_hold_seconds=max_hold,
    )
    return tm._book[symbol]


async def check(tm: TradeManager, mp: ManagedPosition, quote: Quote,
                now: Optional[datetime] = None) -> None:
    await tm._check_position(quote.symbol, mp, quote, now)


# =========================================================================
# Category 1: Standard exits
# =========================================================================


@pytest.mark.asyncio
async def test_t01_hard_stop_routes_marketable_limit(db_session, isolated_db):
    """Entry ₹100, stop ₹97, price falls to ₹96.8: the LIVE exit goes to
    the broker as a marketable LIMIT at bid − 0.2% (≈₹96.6) and settles
    at the broker's fill."""
    md = MarketDataBus()
    backend = ExitStubBackend()
    backend.queue_place(lambda kw: exit_filled(kw, price=kw["limit_price"]))
    tm = make_tm(md, backend=backend)
    try:
        md.set_quote_sync("RELIANCE", 96.8, bid=96.8, ask=96.9)
        mp = await register(tm, entry=100.0, stop=97.0, target=106.0, account_id=make_account(db_session))
        sub = event_bus.subscribe("trade.closed")
        await check(tm, mp, make_quote(96.8, bid=96.8))
        assert "RELIANCE" not in tm._book
        order = backend.orders[0]
        assert order["order_type"] == OrderType.LIMIT
        assert order["side"] == OrderSide.SELL
        assert order["validity"] == "IOC"
        assert order["limit_price"] == pytest.approx(96.8 * 0.998)
        evt = sub.get_nowait()
        event_bus.unsubscribe("trade.closed", sub)
        assert evt.payload["reason"] == "STOP"
        assert evt.payload["exit_price"] == pytest.approx(96.6, rel=0.01)
        trade = db_session.query(TradeRow).filter_by(symbol="RELIANCE").one()
        assert trade.status == "filled" and trade.broker_order_id == "EXIT-1"
        assert trade.pnl == pytest.approx((96.8 * 0.998 - 100.0) * 1000)
    finally:
        tm.stop()


@pytest.mark.asyncio
async def test_t02_target_hit_books_profit(db_session, isolated_db):
    """Entry ₹100, target ₹106, price ₹106.1 → exit TARGET, +6.1%."""
    md = MarketDataBus()
    tm = make_tm(md)  # paper: direct settle
    try:
        mp = await register(tm, entry=100.0, stop=97.0, target=106.0)
        sub = event_bus.subscribe("trade.closed")
        await check(tm, mp, make_quote(106.1))
        evt = sub.get_nowait()
        event_bus.unsubscribe("trade.closed", sub)
        assert evt.payload["reason"] == "TARGET"
        assert evt.payload["exit_price"] == pytest.approx(106.1)
        assert evt.payload["pnl"] == pytest.approx(6.1 * 1000)
        assert "RELIANCE" not in tm._book
    finally:
        tm.stop()


@pytest.mark.asyncio
async def test_t03_breakeven_locks_then_exits(db_session, isolated_db):
    """At +2% the stop moves to entry + 0.2%; the later dip exits with a
    tiny profit instead of a loss."""
    md = MarketDataBus()
    tm = make_tm(md)
    try:
        mp = await register(tm, entry=100.0, stop=97.0, target=106.0)
        await check(tm, mp, make_quote(102.0))
        assert mp.breakeven_armed
        assert mp.stop_loss == pytest.approx(100.2)
        assert "RELIANCE" in tm._book  # not exited yet
        sub = event_bus.subscribe("trade.closed")
        await check(tm, mp, make_quote(100.1))
        evt = sub.get_nowait()
        event_bus.unsubscribe("trade.closed", sub)
        assert evt.payload["reason"] == "STOP"
        assert evt.payload["exit_price"] == pytest.approx(100.1)
        assert evt.payload["pnl"] > 0  # capital preserved
    finally:
        tm.stop()


# =========================================================================
# Category 2: Time & consolidation exits
# =========================================================================


@pytest.mark.asyncio
async def test_t04_time_exit_after_max_hold(db_session, isolated_db):
    md = MarketDataBus()
    tm = make_tm(md)
    try:
        mp = await register(tm, entry=100.0, stop=97.0, target=106.0, max_hold=60)
        mp.opened_at = utcnow() - timedelta(seconds=120)
        sub = event_bus.subscribe("trade.closed")
        await check(tm, mp, make_quote(99.0))
        evt = sub.get_nowait()
        event_bus.unsubscribe("trade.closed", sub)
        assert evt.payload["reason"] == "TIME_EXIT"
        assert evt.payload["exit_price"] == pytest.approx(99.0)
    finally:
        tm.stop()


@pytest.mark.asyncio
async def test_t05_consolidation_exit_frees_capital(db_session, isolated_db):
    """+1.4% winner pinned inside a 0.4% range for >120s → exit
    CONSOLIDATION at ~₹101.4."""
    md = MarketDataBus()
    tm = make_tm(md)
    try:
        mp = await register(tm, entry=100.0, stop=97.0, target=106.0)
        now = utcnow()
        for age_s, px in ((130, 101.3), (90, 101.5), (60, 101.2), (30, 101.6)):
            mp.record_price(px, now - timedelta(seconds=age_s))
        sub = event_bus.subscribe("trade.closed")
        await check(tm, mp, make_quote(101.4), now)
        evt = sub.get_nowait()
        event_bus.unsubscribe("trade.closed", sub)
        assert evt.payload["reason"] == "CONSOLIDATION"
        assert evt.payload["exit_price"] == pytest.approx(101.4)
    finally:
        tm.stop()


@pytest.mark.asyncio
async def test_t06_stall_exit_captures_profit(db_session, isolated_db):
    """+4% winner whose 90s rate-of-change collapsed below 0.3% → exit
    STALL before the reversion."""
    md = MarketDataBus()
    tm = make_tm(md)
    try:
        mp = await register(tm, entry=100.0, stop=97.0, target=110.0)
        now = utcnow()
        for age_s, px in ((100, 103.95), (80, 104.0), (40, 104.05), (5, 104.0)):
            mp.record_price(px, now - timedelta(seconds=age_s))
        sub = event_bus.subscribe("trade.closed")
        await check(tm, mp, make_quote(104.0), now)
        evt = sub.get_nowait()
        event_bus.unsubscribe("trade.closed", sub)
        assert evt.payload["reason"] == "STALL"
        assert evt.payload["exit_price"] == pytest.approx(104.0)
    finally:
        tm.stop()


# =========================================================================
# Category 3: Trailing stop
# =========================================================================


async def _trailing_position(tm: TradeManager) -> ManagedPosition:
    """Entry 100, stop 97 (R=3), NO hard target (trailing manages it),
    activation at 2R, trail distance 0.5R — the plan's numbers."""
    mp = await register(tm, entry=100.0, stop=97.0, target=None)
    mp.scale_out_enabled = False   # isolate the trailing behaviour
    mp.trail_activate_r = 2.0
    mp.trail_distance_r = 0.5
    return mp


@pytest.mark.asyncio
async def test_t07_trail_activates_at_2r(db_session, isolated_db):
    md = MarketDataBus()
    tm = make_tm(md)
    try:
        mp = await _trailing_position(tm)
        await check(tm, mp, make_quote(106.0))  # 2R with R=3
        assert mp.trail_active
        assert mp.peak_price == pytest.approx(106.0)
        assert mp.stop_loss == pytest.approx(104.5)  # 106 − 0.5R (₹1.5)
        assert "RELIANCE" in tm._book
    finally:
        tm.stop()


@pytest.mark.asyncio
async def test_t08_trail_ratchets_up_never_down(db_session, isolated_db):
    md = MarketDataBus()
    tm = make_tm(md)
    try:
        mp = await _trailing_position(tm)
        await check(tm, mp, make_quote(106.0))
        await check(tm, mp, make_quote(115.0))
        assert mp.stop_loss == pytest.approx(113.5)  # 115 − 1.5
        # A pullback ABOVE the stop must not move the stop back down.
        await check(tm, mp, make_quote(114.0))
        assert mp.stop_loss == pytest.approx(113.5)
        assert "RELIANCE" in tm._book
    finally:
        tm.stop()


@pytest.mark.asyncio
async def test_t09_trail_triggers_exit(db_session, isolated_db):
    md = MarketDataBus()
    tm = make_tm(md)
    try:
        mp = await _trailing_position(tm)
        await check(tm, mp, make_quote(106.0))
        await check(tm, mp, make_quote(115.0))
        sub = event_bus.subscribe("trade.closed")
        await check(tm, mp, make_quote(113.2))  # <= 113.5 trail stop
        evt = sub.get_nowait()
        event_bus.unsubscribe("trade.closed", sub)
        assert evt.payload["reason"] == "STOP"
        assert evt.payload["exit_price"] == pytest.approx(113.2)
        assert evt.payload["pnl"] == pytest.approx(13.2 * 1000)
    finally:
        tm.stop()


# =========================================================================
# Category 4: Slippage, gaps & execution emergencies
# =========================================================================


@pytest.mark.asyncio
async def test_t10_flash_crash_limit_then_market_fallback(db_session, isolated_db):
    """Market gaps to ₹92: the marketable limit dies unfilled, the
    remainder is chased at MARKET (~₹91.5). Severe slippage, but OUT."""
    md = MarketDataBus()
    backend = ExitStubBackend()
    backend.queue_place(lambda kw: exit_pending(kw, order_id="L1"))
    backend.queue_status(OrderStatus(broker_order_id="L1", state=OrderState.PENDING,
                                     filled_quantity=0))
    backend.queue_place(lambda kw: exit_filled(kw, price=91.5, order_id="M1"))
    tm = make_tm(md, backend=backend)
    try:
        md.set_quote_sync("RELIANCE", 92.0, bid=92.0)
        mp = await register(tm, entry=100.0, stop=97.0, target=106.0, account_id=make_account(db_session))
        sub = event_bus.subscribe("trade.closed")
        await check(tm, mp, make_quote(92.0, bid=92.0))
        evt = sub.get_nowait()
        event_bus.unsubscribe("trade.closed", sub)
        assert evt.payload["reason"] == "STOP"
        assert evt.payload["exit_price"] == pytest.approx(91.5)
        assert [o["order_type"] for o in backend.orders] == [OrderType.LIMIT, OrderType.MARKET]
        assert backend.cancelled == ["L1"]
        assert "RELIANCE" not in tm._book
    finally:
        tm.stop()


@pytest.mark.asyncio
async def test_t11_partial_exit_fill_chased_at_market(db_session, isolated_db):
    """Only 500 of 1,000 fill on the limit: cancel the rest and market
    the remaining 500 immediately; settle at the weighted average."""
    md = MarketDataBus()
    backend = ExitStubBackend()
    backend.queue_place(lambda kw: exit_pending(kw, order_id="L1"))
    backend.queue_status(OrderStatus(broker_order_id="L1", state=OrderState.PENDING,
                                     filled_quantity=500, average_price=96.6))
    backend.queue_place(lambda kw: exit_filled(kw, price=96.4, qty=500, order_id="M1"))
    tm = make_tm(md, backend=backend)
    try:
        md.set_quote_sync("RELIANCE", 96.8, bid=96.8)
        mp = await register(tm, entry=100.0, stop=97.0, target=106.0, account_id=make_account(db_session))
        sub = event_bus.subscribe("trade.closed")
        await check(tm, mp, make_quote(96.8, bid=96.8))
        evt = sub.get_nowait()
        event_bus.unsubscribe("trade.closed", sub)
        assert backend.cancelled == ["L1"]
        assert backend.orders[1]["order_type"] == OrderType.MARKET
        assert backend.orders[1]["quantity"] == 500
        assert evt.payload["exit_price"] == pytest.approx((96.6 * 500 + 96.4 * 500) / 1000)
        assert "RELIANCE" not in tm._book
    finally:
        tm.stop()


@pytest.mark.asyncio
async def test_t12_rejected_exit_retries_then_escalates(db_session, isolated_db):
    """Broker keeps rejecting the exit: market retries with backoff, and
    once the budget is spent → CRITICAL RiskEvent + system.error alert.
    The position is NEVER dropped from management."""
    md = MarketDataBus()
    backend = ExitStubBackend()
    for _ in range(8):
        backend.queue_place(lambda kw: exit_rejected(kw))
    tm = make_tm(md, backend=backend)
    try:
        md.set_quote_sync("RELIANCE", 96.8, bid=96.8)
        mp = await register(tm, entry=100.0, stop=97.0, target=106.0, account_id=make_account(db_session))
        err_sub = event_bus.subscribe("system.error")
        # First attempt: LIMIT rejected + market retry rejected → failure 1.
        out1 = await tm._exit("RELIANCE", reason="STOP", exit_price=96.8)
        assert out1 is None
        assert "RELIANCE" in tm._book            # still managed
        assert mp.exit_failures == 1
        assert mp.next_exit_retry_at is not None  # cooldown armed
        # Second attempt exhausts EXIT_RETRY_MAX(=2) → escalation fires.
        out2 = await tm._exit("RELIANCE", reason="STOP", exit_price=96.8)
        assert out2 is None
        assert mp.escalated
        evt = err_sub.get_nowait()
        event_bus.unsubscribe("system.error", err_sub)
        assert evt.payload["code"] == "EXIT_FAILED"
        ev = db_session.query(RiskEvent).filter_by(event_type="EXIT_FAILED").one()
        assert ev.severity == "critical"
        assert "RELIANCE" in tm._book            # never silently abandoned
    finally:
        tm.stop()


def test_t13_hard_stop_takes_precedence_over_target():
    """When a gap puts both levels in play on one tick, STOP wins."""
    mp = ManagedPosition(symbol="X", quantity=100, entry=100.0,
                         stop_loss=97.0, target=96.8)
    # last=96.9: stop (<=97) AND target (>=96.8) both true → STOP.
    assert mp.exit_reason(96.9) == "STOP"


# =========================================================================
# Category 5: Concurrency & recovery
# =========================================================================


@pytest.mark.asyncio
async def test_t14_duplicate_symbol_fill_not_double_counted(db_session, isolated_db):
    """A second FILLED event for a symbol already under management is
    ignored — no double-counting."""
    md = MarketDataBus()
    tm = make_tm(md)
    try:
        payload = {
            "state": "FILLED", "side": "BUY", "symbol": "RELIANCE",
            "quantity": 100, "entry": 100.0, "stop_loss": 97.0,
            "target": 106.0, "signal_id": 1, "account_id": None,
            "max_hold_seconds": 100000,
        }
        await tm._on_trade_executed(payload)
        assert tm._book["RELIANCE"].quantity == 100
        await tm._on_trade_executed({**payload, "quantity": 50, "signal_id": 2})
        assert tm._book["RELIANCE"].quantity == 100   # unchanged
        assert len(tm._book) == 1
    finally:
        tm.stop()


@pytest.mark.asyncio
async def test_t15_manual_close_marks_closed_external(db_session, isolated_db):
    """Operator closes the position in the Fyers app: reconciliation sees
    the broker flat, stops managing, settles CLOSED_EXTERNAL — placing
    NO order."""
    md = MarketDataBus()
    backend = ExitStubBackend()
    tm = make_tm(md, backend=backend, live_positions=lambda: [])
    try:
        mp = await register(tm, entry=100.0, stop=97.0, target=106.0, account_id=make_account(db_session))
        closed_sub = event_bus.subscribe("trade.closed")
        err_sub = event_bus.subscribe("system.error")
        await tm._reconcile_positions()
        assert "RELIANCE" not in tm._book
        evt = closed_sub.get_nowait()
        assert evt.payload["reason"] == "CLOSED_EXTERNAL"
        alert = err_sub.get_nowait()
        assert alert.payload["code"] == "CLOSED_EXTERNAL"
        event_bus.unsubscribe("trade.closed", closed_sub)
        event_bus.unsubscribe("system.error", err_sub)
        assert backend.orders == []   # no order placed for an external close
        trade = db_session.query(TradeRow).filter_by(symbol="RELIANCE").one()
        assert trade.status == "filled"
    finally:
        tm.stop()


@pytest.mark.asyncio
async def test_t16_restart_rehydrates_saved_levels(db_session, isolated_db):
    """Bot restarts mid-trade: the managed book rebuilds from the
    position row with the persisted stop/target and resumes."""
    db_session.add(PositionRow(
        symbol="RELIANCE", quantity=100, average_price=100.0,
        last_price=103.0, unrealized_pnl=300.0,
        stop_loss=97.0, target=106.0,
    ))
    db_session.commit()
    md = MarketDataBus()
    tm = make_tm(md)
    try:
        await tm._hydrate_book()
        mp = tm._book["RELIANCE"]
        assert mp.entry == pytest.approx(100.0)
        assert mp.stop_loss == pytest.approx(97.0)
        assert mp.target == pytest.approx(106.0)
        # ... and management actually works post-restart.
        sub = event_bus.subscribe("trade.closed")
        await check(tm, mp, make_quote(106.2))
        evt = sub.get_nowait()
        event_bus.unsubscribe("trade.closed", sub)
        assert evt.payload["reason"] == "TARGET"
    finally:
        tm.stop()


# =========================================================================
# Category 6: Stop & target calculation edges
# =========================================================================


@pytest.mark.asyncio
async def test_t17_invalid_target_clamped_to_half_r(db_session, isolated_db):
    """BUY with target BELOW entry is malformed → clamp to entry + 0.5R."""
    md = MarketDataBus()
    tm = make_tm(md)
    try:
        mp = await register(tm, entry=100.0, stop=97.0, target=95.0)
        assert mp.target == pytest.approx(101.5)  # 100 + 0.5 × 3
    finally:
        tm.stop()


@pytest.mark.asyncio
async def test_t18_zero_stop_distance_blocked(db_session, isolated_db):
    """stop == entry → zero risk distance → INVALID_STOP block."""
    md = MarketDataBus()
    engine = RiskEngine(market_data=md, portfolio_value=10_000_000.0)
    sig = SimpleNamespace(id=None, symbol="RELIANCE", action="BUY",
                          confidence=0.9, position_size_pct=0.0,
                          strategy_id=None, rationale="")
    decision = await engine.evaluate(signal=sig, entry=100.0, stop_loss=100.0)
    assert decision.approved is False
    assert "RISK_INVALID_STOP_DISTANCE" in decision.codes


def test_t19_atr_unavailable_falls_back_to_default_pct():
    dist = volatility.stop_distance(
        entry=100.0, atr=None, mult=2.0, default_pct=6.0,
        min_pct=0.5, max_pct=8.0, smallcap_price=50.0, smallcap_pct=1.0,
    )
    assert dist == pytest.approx(6.0)  # 6% of 100


def test_t20_atr_stop_capped_at_max_pct():
    """ATR suggests a 20% stop → capped at ATR_MAX_STOP_PCT (8%)."""
    dist = volatility.stop_distance(
        entry=100.0, atr=10.0, mult=2.0, default_pct=6.0,
        min_pct=0.5, max_pct=8.0, smallcap_price=50.0, smallcap_pct=1.0,
    )
    assert dist == pytest.approx(8.0)


# =========================================================================
# Category 7: Scale-out & partial exits
# =========================================================================


@pytest.mark.asyncio
async def test_t21_scale_out_takes_half_and_trails_rest(db_session, isolated_db):
    """At +2R half the position books profit; the remainder runs with at
    least a breakeven stop and keeps trailing."""
    md = MarketDataBus()
    tm = make_tm(md)
    try:
        mp = await register(tm, entry=100.0, stop=97.0, target=None)
        mp.scale_out_enabled = True
        mp.scale_out_r = 2.0
        sub = event_bus.subscribe("trade.closed")
        await check(tm, mp, make_quote(106.0))   # 2R hit
        evt = sub.get_nowait()
        event_bus.unsubscribe("trade.closed", sub)
        assert evt.payload["reason"] == "SCALE_OUT"
        assert evt.payload.get("partial") is True
        assert abs(evt.payload["quantity"]) == 500
        assert mp.scaled_out
        assert tm._book["RELIANCE"].quantity == 500     # runner remains
        assert mp.stop_loss >= 100.0                    # breakeven or better
        trade = db_session.query(TradeRow).filter_by(symbol="RELIANCE").one()
        assert trade.quantity == 500 and trade.pnl == pytest.approx(6.0 * 500)
    finally:
        tm.stop()


@pytest.mark.asyncio
async def test_t22_scale_out_partial_fill_not_chased(db_session, isolated_db):
    """LIVE scale-out: only 300 of the 500 profit-take fill. The
    remainder is NOT chased — 700 shares stay managed; risk only fell."""
    md = MarketDataBus()
    backend = ExitStubBackend()
    backend.queue_place(lambda kw: exit_pending(kw, order_id="S1"))
    backend.queue_status(OrderStatus(broker_order_id="S1", state=OrderState.PENDING,
                                     filled_quantity=300, average_price=106.1))
    tm = make_tm(md, backend=backend)
    try:
        md.set_quote_sync("RELIANCE", 106.0, bid=106.0)
        mp = await register(tm, entry=100.0, stop=97.0, target=None, account_id=make_account(db_session))
        mp.scale_out_enabled = True
        mp.scale_out_r = 2.0
        await check(tm, mp, make_quote(106.0, bid=106.0))
        assert mp.scaled_out
        assert tm._book["RELIANCE"].quantity == 700     # 1000 − 300 filled
        assert backend.cancelled == ["S1"]              # unfilled 200 cancelled
        assert len(backend.orders) == 1                 # never chased at market
        trade = db_session.query(TradeRow).filter_by(symbol="RELIANCE").one()
        assert trade.quantity == 300
    finally:
        tm.stop()


@pytest.mark.asyncio
async def test_t23_trailing_continues_after_scale_out(db_session, isolated_db):
    md = MarketDataBus()
    tm = make_tm(md)
    try:
        mp = await register(tm, entry=100.0, stop=97.0, target=None)
        mp.scale_out_enabled = True
        mp.scale_out_r = 2.0
        mp.trail_activate_r = 2.0
        mp.trail_distance_r = 0.5
        await check(tm, mp, make_quote(106.0))          # scale out at 2R
        assert tm._book["RELIANCE"].quantity == 500
        await check(tm, mp, make_quote(112.0))          # runner trails
        assert mp.trail_active
        assert mp.stop_loss == pytest.approx(110.5)     # 112 − 0.5R
        sub = event_bus.subscribe("trade.closed")
        await check(tm, mp, make_quote(110.4))
        evt = sub.get_nowait()
        event_bus.unsubscribe("trade.closed", sub)
        assert evt.payload["reason"] == "STOP"
        assert evt.payload["quantity"] == 500           # only the runner
    finally:
        tm.stop()


# =========================================================================
# Category 8: Broker & network anomalies
# =========================================================================


@pytest.mark.asyncio
async def test_t24_broker_timeout_one_market_retry_then_flag(db_session, isolated_db):
    """Broker hangs past the timeout: exactly ONE market retry, then the
    position stays open under a retry cooldown — never a blind duplicate."""
    md = MarketDataBus()
    backend = ExitStubBackend()
    backend.place_delay = 0.6      # > EXIT_BROKER_TIMEOUT_SECONDS (0.25)
    backend.queue_place(lambda kw: exit_filled(kw))
    backend.queue_place(lambda kw: exit_filled(kw))
    tm = make_tm(md, backend=backend)
    try:
        md.set_quote_sync("RELIANCE", 96.8, bid=96.8)
        mp = await register(tm, entry=100.0, stop=97.0, target=106.0, account_id=make_account(db_session))
        out = await tm._exit("RELIANCE", reason="STOP", exit_price=96.8)
        assert out is None
        assert len(backend.orders) == 2   # LIMIT attempt + ONE market retry
        assert backend.orders[1]["order_type"] == OrderType.MARKET
        assert "RELIANCE" in tm._book     # still open, still managed
        assert mp.exit_failures == 1
        assert mp.next_exit_retry_at is not None
        # No trade row was booked for an unconfirmed exit.
        assert db_session.query(TradeRow).filter_by(symbol="RELIANCE").count() == 0
    finally:
        tm.stop()


@pytest.mark.asyncio
async def test_t25_stale_feed_never_fires_price_exits(db_session, isolated_db):
    """WS feed frozen for 10s: a stop-crossing STALE tick must NOT exit.
    The same price on a FRESH tick exits normally once the feed returns."""
    md = MarketDataBus()
    live_feed = SimpleNamespace(
        has_live_feed=lambda: True,
        watch=lambda *a, **k: 0.0,
        unwatch=lambda *a, **k: None,
    )
    tm = make_tm(md, quote_feed=live_feed)
    try:
        mp = await register(tm, entry=100.0, stop=97.0, target=106.0)
        stale = make_quote(96.0, source="fyers_ws", ts=utcnow() - timedelta(seconds=10))
        await check(tm, mp, stale)
        assert "RELIANCE" in tm._book        # static stop held, no exit
        fresh = make_quote(96.0, source="fyers_ws")
        sub = event_bus.subscribe("trade.closed")
        await check(tm, mp, fresh)
        evt = sub.get_nowait()
        event_bus.unsubscribe("trade.closed", sub)
        assert evt.payload["reason"] == "STOP"
    finally:
        tm.stop()


@pytest.mark.asyncio
async def test_t26_reconcile_leaves_matching_positions_alone(db_session, isolated_db):
    """Reconciliation with a broker book that MATCHES ours must be a
    no-op (no false CLOSED_EXTERNAL)."""
    md = MarketDataBus()
    backend = ExitStubBackend()
    broker_book = [Position(symbol="NSE:RELIANCE-EQ", quantity=1000, average_price=100.0)]
    tm = make_tm(md, backend=backend, live_positions=lambda: broker_book)
    try:
        mp = await register(tm, entry=100.0, stop=97.0, target=106.0, account_id=make_account(db_session))
        await tm._reconcile_positions()
        assert "RELIANCE" in tm._book
        assert tm._book["RELIANCE"].quantity == 1000
    finally:
        tm.stop()


@pytest.mark.asyncio
async def test_t27_margin_call_resize_syncs_local_book(db_session, isolated_db):
    """Broker squared off part of the position (margin call): the local
    quantity resizes down to the broker's truth."""
    db_session.add(PositionRow(symbol="RELIANCE", quantity=1000,
                               average_price=100.0, last_price=100.0))
    db_session.commit()
    md = MarketDataBus()
    backend = ExitStubBackend()
    broker_book = [Position(symbol="NSE:RELIANCE-EQ", quantity=400, average_price=100.0)]
    tm = make_tm(md, backend=backend, live_positions=lambda: broker_book)
    try:
        mp = await register(tm, entry=100.0, stop=97.0, target=106.0, account_id=make_account(db_session))
        await tm._reconcile_positions()
        assert tm._book["RELIANCE"].quantity == 400
        db_session.expire_all()
        row = db_session.query(PositionRow).filter_by(symbol="RELIANCE").one()
        assert row.quantity == 400
    finally:
        tm.stop()


# =========================================================================
# Category 9: Risk & sizing edges
# =========================================================================


@pytest.mark.asyncio
async def test_t28_stop_on_wrong_side_rejected(db_session, isolated_db):
    md = MarketDataBus()
    engine = RiskEngine(market_data=md, portfolio_value=10_000_000.0)
    sig = SimpleNamespace(id=None, symbol="RELIANCE", action="BUY",
                          confidence=0.9, position_size_pct=0.0,
                          strategy_id=None, rationale="")
    decision = await engine.evaluate(signal=sig, entry=100.0, stop_loss=105.0)
    assert decision.approved is False
    assert "RISK_STOP_ABOVE_ENTRY" in decision.codes
    # Mirror for shorts.
    sig_short = SimpleNamespace(id=None, symbol="RELIANCE", action="SELL",
                                confidence=0.9, position_size_pct=0.0,
                                strategy_id=None, rationale="")
    decision2 = await engine.evaluate(signal=sig_short, entry=100.0, stop_loss=95.0)
    assert decision2.approved is False
    assert "RISK_STOP_BELOW_ENTRY" in decision2.codes


@pytest.mark.asyncio
async def test_t29_quantity_rounds_to_zero_blocked(db_session, isolated_db):
    """Risk budget too small for one share → QTY block, no trade."""
    md = MarketDataBus()
    engine = RiskEngine(market_data=md, portfolio_value=100.0)
    sig = SimpleNamespace(id=None, symbol="RELIANCE", action="BUY",
                          confidence=0.9, position_size_pct=0.0,
                          strategy_id=None, rationale="")
    decision = await engine.evaluate(signal=sig, entry=1000.0, stop_loss=900.0)
    assert decision.approved is False
    assert "RISK_QTY_BELOW_1" in decision.codes


@pytest.mark.asyncio
async def test_t30_sizing_follows_current_portfolio_value(db_session, isolated_db):
    """Risk %% is computed from the CURRENT portfolio value: doubling the
    capital doubles the size for the identical signal."""
    md = MarketDataBus()
    sig = SimpleNamespace(id=None, symbol="RELIANCE", action="BUY",
                          confidence=0.9, position_size_pct=0.0,
                          strategy_id=None, rationale="")
    d1 = await RiskEngine(market_data=md, portfolio_value=1_000_000.0).evaluate(
        signal=sig, entry=100.0, stop_loss=97.0
    )
    d2 = await RiskEngine(market_data=md, portfolio_value=2_000_000.0).evaluate(
        signal=sig, entry=100.0, stop_loss=97.0
    )
    assert d1.approved and d2.approved
    assert d2.sizing.qty == pytest.approx(2 * d1.sizing.qty, rel=0.01)


# =========================================================================
# Realtime: exits fire from the WS tick, not the poll cadence
# =========================================================================


@pytest.mark.asyncio
async def test_realtime_tick_triggers_exit_without_sweep(db_session, isolated_db):
    """A tick PUBLISHED on the MarketDataBus (the Fyers WS path) reaches
    the tick listener and exits the position — no sweep involved."""
    md = MarketDataBus()
    tm = make_tm(md)
    try:
        await register(tm, entry=100.0, stop=97.0, target=106.0)
        sub = event_bus.subscribe("trade.closed")
        await md.publish("RELIANCE", 96.5)   # stop crossed, via the bus
        evt = await asyncio.wait_for(sub.get(), timeout=2.0)
        event_bus.unsubscribe("trade.closed", sub)
        assert evt.payload["reason"] == "STOP"
        assert evt.payload["exit_price"] == pytest.approx(96.5)
        assert "RELIANCE" not in tm._book
    finally:
        tm.stop()
