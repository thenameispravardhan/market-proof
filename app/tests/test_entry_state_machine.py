"""The definitive entry state machine test cases (plan E-01 .. E-15).

Categories:
  1. Happy path            E-01, E-02
  2. Drift & retracement   E-03, E-04, E-05
  3. Fill outcomes         E-06, E-07, E-08
  4. Broker errors         E-09, E-10, E-11
  5. Concurrency           E-12, E-13
  6. Crash during fill     E-14, E-15

Unit tests drive `EntryManager` / `RetracementMonitor` directly with a
stub backend and millisecond timeouts; the integration tests at the
bottom run the full `Manager.process_signal` pipeline against the DB.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from app.execution.base import (
    OrderResult,
    OrderSide,
    OrderState,
    OrderStatus,
    OrderType,
)
from app.execution.entry_manager import (
    CHANNEL_TRADES_FILLED,
    EntryManager,
    EntryState,
    OUTCOME_BROKER_TIMEOUT,
    OUTCOME_EXCESSIVE_DRIFT,
    OUTCOME_EXPIRED,
    OUTCOME_FLASH_CRASH_EXIT,
    OUTCOME_FULL_FILL,
    OUTCOME_PARTIAL_FILL,
    OUTCOME_REJECTED,
    OUTCOME_SLIPPAGE_BREACH,
    OUTCOME_SYMBOL_LOCKED,
    RetracementMonitor,
)
from app.execution.market_data import MarketDataBus
from app.services.event_bus import event_bus


# ---- helpers ---------------------------------------------------------------


def make_settings(**over: Any) -> Any:
    """Millisecond-scale timeouts so the fill/lock windows don't slow
    the suite down. Values mirror the real Settings fields."""
    base = dict(
        ENTRY_BUFFER_PCT=0.2,
        MAX_SLIPPAGE_PCT=0.25,
        ENTRY_MAX_DRIFT_PCT=1.5,
        RETRACEMENT_WINDOW_SECONDS=60,
        RETRACEMENT_POLL_SECONDS=0.02,
        SYMBOL_LOCK_WAIT_SECONDS=0.1,
        ORDER_FILL_TIMEOUT_SECONDS=0.15,   # REST poll mark
        ENTRY_FILL_TIMEOUT_SECONDS=0.35,   # total fill window
        BROKER_ORDER_TIMEOUT_SECONDS=0.4,  # place_order cap
    )
    base.update(over)
    ns = SimpleNamespace(**base)
    return ns


def filled_result(kw: dict[str, Any], *, price: Optional[float] = None,
                  qty: Optional[int] = None, order_id: str = "ORD-1") -> OrderResult:
    return OrderResult(
        broker_order_id=order_id,
        state=OrderState.FILLED,
        symbol=kw["symbol"],
        side=kw["side"],
        quantity=int(kw["quantity"]),
        order_type=kw["order_type"],
        filled_quantity=int(qty if qty is not None else kw["quantity"]),
        average_price=float(price if price is not None else kw["limit_price"]),
        limit_price=kw.get("limit_price"),
    )


def pending_result(kw: dict[str, Any], *, order_id: str = "ORD-1") -> OrderResult:
    return OrderResult(
        broker_order_id=order_id,
        state=OrderState.PENDING,
        symbol=kw["symbol"],
        side=kw["side"],
        quantity=int(kw["quantity"]),
        order_type=kw["order_type"],
        limit_price=kw.get("limit_price"),
    )


def rejected_result(kw: dict[str, Any], *, error: str) -> OrderResult:
    return OrderResult(
        broker_order_id="",
        state=OrderState.REJECTED,
        symbol=kw["symbol"],
        side=kw["side"],
        quantity=int(kw["quantity"]),
        order_type=kw["order_type"],
        error=error,
    )


class StubBackend:
    """Programmable backend: queue place_order handlers (result objects
    or callables) and get_order_status responses. Handlers pop FIFO;
    the last status is reused so repeated polls are cheap to set up."""

    name = "stub"
    broker_account_id = None

    def __init__(self) -> None:
        self.orders: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.status_calls = 0
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
        self.status_calls += 1
        assert self._statuses, "no get_order_status response queued"
        if len(self._statuses) > 1:
            return self._statuses.pop(0)
        return self._statuses[0]

    async def get_positions(self) -> list[Any]:
        return []


def make_em(md: MarketDataBus, settings: Any = None) -> EntryManager:
    s = settings or make_settings()
    return EntryManager(market_data=md, settings_provider=lambda: s)


async def wait_for(cond, timeout: float = 2.0) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return cond()


# =========================================================================
# Category 1: Happy path
# =========================================================================


@pytest.mark.asyncio
async def test_e01_full_fill_within_drift() -> None:
    """Signal ₹100, live ask ₹100.1 (inside 1.5%). IOC limit ≈ ₹100.3;
    all 1,000 shares fill; ACTIVE_POSITION with the stop untouched."""
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=100.05, bid=100.0, ask=100.1)
    backend = StubBackend()
    backend.queue_place(lambda kw: filled_result(kw, price=100.1))
    em = make_em(md)

    outcome = await em.execute(
        backend=backend, symbol="RELIANCE", side=OrderSide.BUY,
        quantity=1000, signal_entry=100.0, stop_loss=97.0, target=109.0,
    )

    assert outcome.code == OUTCOME_FULL_FILL
    assert outcome.attempt.state == EntryState.ACTIVE_POSITION
    assert outcome.attempt.limit_price == pytest.approx(100.1 * 1.002)
    assert outcome.result is not None
    assert outcome.result.filled_quantity == 1000
    assert outcome.attempt.stop_loss == 97.0
    # The machine walked the expected states.
    for state in ("AWAITING_QUOTE", "DRIFT_CHECK", "ORDER_ROUTING", "ACTIVE_POSITION"):
        assert state in outcome.attempt.history
    # The order really was an IOC limit.
    assert backend.orders[0]["order_type"] == OrderType.LIMIT
    assert backend.orders[0]["validity"] == "IOC"


@pytest.mark.asyncio
async def test_e02_favourable_dip_enters_at_better_price() -> None:
    """Live ask ₹99.8 (stock dipped below the ₹100 signal). Favourable
    drift never blocks; the limit anchors on the LIVE price (≈₹100.0)
    and we fill better than planned."""
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=99.8, bid=99.7, ask=99.8)
    backend = StubBackend()
    backend.queue_place(lambda kw: filled_result(kw, price=99.8))
    em = make_em(md)

    outcome = await em.execute(
        backend=backend, symbol="RELIANCE", side=OrderSide.BUY,
        quantity=1000, signal_entry=100.0, stop_loss=97.0,
    )

    assert outcome.code == OUTCOME_FULL_FILL
    assert outcome.attempt.drift_pct is not None and outcome.attempt.drift_pct < 0
    assert outcome.attempt.limit_price == pytest.approx(99.8 * 1.002)
    assert outcome.result.average_price == pytest.approx(99.8)


# =========================================================================
# Category 2: Drift & retracement (anti-chase)
# =========================================================================


@pytest.mark.asyncio
async def test_e03_excessive_drift_blocks_without_an_order() -> None:
    """Live ask ₹102 = 2% above the ₹100 signal (> 1.5%). No order
    reaches the broker; the attempt parks in RETRACEMENT_WATCH."""
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=102.0, bid=101.9, ask=102.0)
    backend = StubBackend()
    em = make_em(md)

    outcome = await em.execute(
        backend=backend, symbol="RELIANCE", side=OrderSide.BUY,
        quantity=1000, signal_entry=100.0, stop_loss=97.0,
    )

    assert outcome.code == OUTCOME_EXCESSIVE_DRIFT
    assert outcome.attempt.state == EntryState.RETRACEMENT_WATCH
    assert outcome.attempt.drift_pct == pytest.approx(2.0)
    assert backend.orders == []  # nothing was routed


@pytest.mark.asyncio
async def test_e04_retracement_rearms_on_pullback() -> None:
    """Blocked at ₹102; the stock pulls back to ₹101.2 (1.2% < 1.5%)
    inside the window → the monitor fires the re-entry callback once."""
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=102.0, ask=102.0)
    fired: list[Any] = []
    expired: list[Any] = []

    async def reenter(w: Any) -> None:
        fired.append(w)

    async def expire(w: Any) -> None:
        expired.append(w)

    settings = make_settings(RETRACEMENT_WINDOW_SECONDS=5)
    mon = RetracementMonitor(
        market_data=md, reenter_cb=reenter, expire_cb=expire,
        settings_provider=lambda: settings,
    )
    try:
        assert mon.watch(signal_id=1, symbol="RELIANCE", side="BUY", signal_entry=100.0)
        await asyncio.sleep(0.08)          # several polls at ₹102 — no fire
        assert not fired
        md.set_quote_sync("RELIANCE", last_price=101.2, ask=101.2)
        assert await wait_for(lambda: bool(fired))
        assert fired[0].signal_id == 1
        assert fired[0].signal_entry == 100.0   # ORIGINAL anchor rides along
        assert not mon.watching(1)
        assert not expired
    finally:
        mon.stop()


@pytest.mark.asyncio
async def test_e05_retracement_expires_when_price_stays_away() -> None:
    """The stock never pulls back inside the window → the watch expires;
    the signal is done, no trade, no re-arm."""
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=102.0, ask=102.0)
    fired: list[Any] = []
    expired: list[Any] = []

    async def reenter(w: Any) -> None:
        fired.append(w)

    async def expire(w: Any) -> None:
        expired.append(w)

    settings = make_settings(RETRACEMENT_WINDOW_SECONDS=0.1)
    mon = RetracementMonitor(
        market_data=md, reenter_cb=reenter, expire_cb=expire,
        settings_provider=lambda: settings,
    )
    try:
        mon.watch(signal_id=2, symbol="RELIANCE", side="BUY", signal_entry=100.0)
        assert await wait_for(lambda: bool(expired))
        assert expired[0].signal_id == 2
        assert not fired
        assert not mon.watching(2)
    finally:
        mon.stop()


# =========================================================================
# Category 3: Fill outcomes (partial, zero, slippage)
# =========================================================================


@pytest.mark.asyncio
async def test_e06_partial_fill_keeps_stop_and_cancels_remainder() -> None:
    """300 of 1,000 fill inside the window. The position hands over with
    exactly 300 shares, the stop stays at ₹97 (rupee risk scales down
    with quantity), and the remainder is cancelled."""
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=100.1, ask=100.1)
    backend = StubBackend()
    backend.queue_place(lambda kw: pending_result(kw))
    backend.queue_status(OrderStatus(
        broker_order_id="ORD-1", state=OrderState.PENDING,
        filled_quantity=300, average_price=100.2,
    ))
    routed: list[OrderResult] = []

    async def on_routed(r: OrderResult) -> None:
        routed.append(r)

    em = make_em(md)
    outcome = await em.execute(
        backend=backend, symbol="RELIANCE", side=OrderSide.BUY,
        quantity=1000, signal_entry=100.0, stop_loss=97.0,
        on_routed=on_routed,
    )

    assert outcome.code == OUTCOME_PARTIAL_FILL
    assert outcome.attempt.filled_quantity == 300
    assert outcome.attempt.stop_loss == 97.0            # unchanged
    assert outcome.result.quantity == 300               # handover = actual fill
    assert outcome.result.average_price == pytest.approx(100.2)
    assert backend.cancelled == ["ORD-1"]               # remainder cancelled
    assert "PARTIAL_HANDLING" in outcome.attempt.history
    assert routed and routed[0].state == OrderState.PENDING


@pytest.mark.asyncio
async def test_e07_zero_fill_expires_without_retry() -> None:
    """Zero shares fill inside the window (market ran away). The order
    is cancelled, the attempt EXPIRES, and there is NO retry."""
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=100.1, ask=100.1)
    backend = StubBackend()
    backend.queue_place(lambda kw: pending_result(kw))
    backend.queue_status(OrderStatus(
        broker_order_id="ORD-1", state=OrderState.PENDING, filled_quantity=0,
    ))
    em = make_em(md)

    outcome = await em.execute(
        backend=backend, symbol="RELIANCE", side=OrderSide.BUY,
        quantity=1000, signal_entry=100.0, stop_loss=97.0,
    )

    assert outcome.code == OUTCOME_EXPIRED
    assert outcome.attempt.state == EntryState.EXPIRED
    assert outcome.result.state == OrderState.EXPIRED
    assert backend.cancelled == ["ORD-1"]
    assert len(backend.orders) == 1                     # no retry, no duplicate


@pytest.mark.asyncio
async def test_e08_slippage_breach_flattens_immediately() -> None:
    """Fill lands ABOVE the limit (exchange glitch). Nuclear option: the
    1,000 shares are sold at market immediately."""
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=100.1, ask=100.1)
    backend = StubBackend()
    backend.queue_place(lambda kw: filled_result(kw, price=100.6))  # > limit 100.3
    backend.queue_place(lambda kw: filled_result(kw, price=100.5, order_id="EXIT-1"))
    em = make_em(md)

    outcome = await em.execute(
        backend=backend, symbol="RELIANCE", side=OrderSide.BUY,
        quantity=1000, signal_entry=100.0, stop_loss=97.0,
    )

    assert outcome.code == OUTCOME_SLIPPAGE_BREACH
    assert outcome.exit_result is not None
    assert len(backend.orders) == 2
    exit_order = backend.orders[1]
    assert exit_order["side"] == OrderSide.SELL
    assert exit_order["order_type"] == OrderType.MARKET
    assert exit_order["quantity"] == 1000


# =========================================================================
# Category 4: Broker errors & rejections
# =========================================================================


@pytest.mark.asyncio
async def test_e09_broker_rejection_is_terminal() -> None:
    """Fyers -32 (insufficient margin): the attempt ends REJECTED with
    the broker's error preserved; nothing is cancelled or retried."""
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=100.1, ask=100.1)
    backend = StubBackend()
    backend.queue_place(
        lambda kw: rejected_result(kw, error="code -32: insufficient margin")
    )
    em = make_em(md)

    outcome = await em.execute(
        backend=backend, symbol="RELIANCE", side=OrderSide.BUY,
        quantity=1000, signal_entry=100.0, stop_loss=97.0,
    )

    assert outcome.code == OUTCOME_REJECTED
    assert outcome.attempt.state == EntryState.REJECTED
    assert "margin" in (outcome.message or "")
    assert len(backend.orders) == 1
    assert backend.cancelled == []


@pytest.mark.asyncio
async def test_e10_broker_timeout_never_retries() -> None:
    """place_order hangs past the broker timeout. The attempt ends
    BROKER_TIMEOUT and — critically — is never retried (the order MAY
    have reached the broker; a blind retry risks a double)."""
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=100.1, ask=100.1)
    backend = StubBackend()
    backend.place_delay = 0.8  # > BROKER_ORDER_TIMEOUT_SECONDS (0.4)
    backend.queue_place(lambda kw: filled_result(kw))
    em = make_em(md)

    outcome = await em.execute(
        backend=backend, symbol="RELIANCE", side=OrderSide.BUY,
        quantity=1000, signal_entry=100.0, stop_loss=97.0,
    )

    assert outcome.code == OUTCOME_BROKER_TIMEOUT
    assert len(backend.orders) == 1
    await asyncio.sleep(0.05)
    assert len(backend.orders) == 1  # still exactly one — no retry


@pytest.mark.asyncio
async def test_e11_ws_confirmation_wins_over_rest() -> None:
    """The order-WS fill event arrives first: the attempt confirms via
    WS and REST is never polled (the late REST view is redundant —
    dedup at the DB layer is covered separately below)."""
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=100.1, ask=100.1)
    backend = StubBackend()
    backend.queue_place(lambda kw: pending_result(kw))
    backend.queue_status(OrderStatus(   # only reached if WS were missed
        broker_order_id="ORD-1", state=OrderState.FILLED,
        filled_quantity=1000, average_price=100.2,
    ))
    em = make_em(md)

    task = asyncio.create_task(em.execute(
        backend=backend, symbol="RELIANCE", side=OrderSide.BUY,
        quantity=1000, signal_entry=100.0, stop_loss=97.0,
    ))
    await asyncio.sleep(0.03)  # inside the WS window, before the REST mark
    await event_bus.publish(
        CHANNEL_TRADES_FILLED,
        {"broker_order_id": "ORD-1", "price": 100.2, "status": "filled"},
    )
    outcome = await task

    assert outcome.code == OUTCOME_FULL_FILL
    assert outcome.attempt.fill_source == "ws"
    assert outcome.result.average_price == pytest.approx(100.2)
    assert backend.status_calls == 0   # REST never consulted


# =========================================================================
# Category 5: Concurrency & race conditions
# =========================================================================


@pytest.mark.asyncio
async def test_e12_second_signal_rejected_by_symbol_lock() -> None:
    """Two overlapping signals for RELIANCE. The second waits its 500ms
    (scaled down here), finds the first still routing, and rejects with
    SYMBOL_LOCKED. Exactly ONE order reaches the broker."""
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=100.1, ask=100.1)
    backend = StubBackend()
    backend.place_delay = 0.25            # longer than the 0.1s lock wait
    backend.queue_place(lambda kw: filled_result(kw))
    backend.queue_place(lambda kw: filled_result(kw))  # must never be used
    em = make_em(md)

    kwargs: dict[str, Any] = dict(
        backend=backend, symbol="RELIANCE", side=OrderSide.BUY,
        quantity=1000, signal_entry=100.0, stop_loss=97.0,
    )
    a, b = await asyncio.gather(em.execute(**kwargs), em.execute(**kwargs))

    codes = sorted([a.code, b.code])
    assert codes == sorted([OUTCOME_FULL_FILL, OUTCOME_SYMBOL_LOCKED])
    assert len(backend.orders) == 1       # the double entry never happened


@pytest.mark.asyncio
async def test_e13_retracement_watch_holds_no_lock() -> None:
    """Signal A sits in RETRACEMENT_WATCH; a fresh signal B for the same
    symbol arrives. The watch is passive — B trades normally while A
    keeps watching."""
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=102.0, ask=102.0)
    settings = make_settings(RETRACEMENT_WINDOW_SECONDS=5)
    mon = RetracementMonitor(market_data=md, settings_provider=lambda: settings)
    backend = StubBackend()
    backend.queue_place(lambda kw: filled_result(kw))
    em = make_em(md, settings)
    try:
        # Signal A: drift-blocked at 102 vs its 100 anchor, parked.
        mon.watch(signal_id=10, symbol="RELIANCE", side="BUY", signal_entry=100.0)
        assert mon.watching(10)
        # Signal B: fresh news, priced AT the current market.
        outcome = await em.execute(
            backend=backend, symbol="RELIANCE", side=OrderSide.BUY,
            quantity=500, signal_entry=102.0, stop_loss=99.0,
        )
        assert outcome.code == OUTCOME_FULL_FILL
        assert mon.watching(10)           # A is still parked, undisturbed
    finally:
        mon.stop()


# =========================================================================
# Category 6: Market crash during the fill window
# =========================================================================


@pytest.mark.asyncio
async def test_e14_flash_crash_through_stop_exits_immediately() -> None:
    """The fill confirms at ₹96.5 — already below the ₹97 stop. The
    machine exits at market instead of handing a dead position over."""
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=100.1, ask=100.1)
    backend = StubBackend()

    def crash_fill(kw: dict[str, Any]) -> OrderResult:
        md.set_quote_sync("RELIANCE", last_price=96.5, ask=96.5)
        return filled_result(kw, price=96.5)

    backend.queue_place(crash_fill)
    backend.queue_place(lambda kw: filled_result(kw, price=96.2, order_id="EXIT-1"))
    em = make_em(md)

    outcome = await em.execute(
        backend=backend, symbol="RELIANCE", side=OrderSide.BUY,
        quantity=1000, signal_entry=100.0, stop_loss=97.0,
    )

    assert outcome.code == OUTCOME_FLASH_CRASH_EXIT
    assert outcome.exit_result is not None
    exit_order = backend.orders[1]
    assert exit_order["side"] == OrderSide.SELL
    assert exit_order["order_type"] == OrderType.MARKET
    assert exit_order["quantity"] == 1000


@pytest.mark.asyncio
async def test_e15_partial_fill_then_crash_exits_the_filled_half() -> None:
    """500 of 1,000 fill, then the stock crashes through the stop before
    the rest can fill. The remainder is cancelled and ONLY the 500
    filled shares are flattened."""
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=100.1, ask=100.1)
    backend = StubBackend()
    backend.queue_place(lambda kw: pending_result(kw))
    backend.queue_place(lambda kw: filled_result(kw, price=96.4, order_id="EXIT-1"))
    backend.queue_status(OrderStatus(
        broker_order_id="ORD-1", state=OrderState.PENDING,
        filled_quantity=500, average_price=100.1,
    ))
    em = make_em(md)

    task = asyncio.create_task(em.execute(
        backend=backend, symbol="RELIANCE", side=OrderSide.BUY,
        quantity=1000, signal_entry=100.0, stop_loss=97.0,
    ))
    await asyncio.sleep(0.05)   # inside the fill window
    md.set_quote_sync("RELIANCE", last_price=96.5, ask=96.5)  # the crash
    outcome = await task

    assert outcome.code == OUTCOME_FLASH_CRASH_EXIT
    assert outcome.attempt.filled_quantity == 500
    assert backend.cancelled == ["ORD-1"]        # unfilled 500 cancelled
    exit_order = backend.orders[1]
    assert exit_order["quantity"] == 500          # only the filled half exits
    assert exit_order["side"] == OrderSide.SELL
    assert exit_order["order_type"] == OrderType.MARKET


# =========================================================================
# Integration: the full Manager pipeline + DB persistence
# =========================================================================


from app.db.models import (  # noqa: E402
    BrokerAccount,
    Position as PositionRow,
    RiskEvent,
    Signal,
    Strategy,
    Trade as TradeRow,
)
from app.execution.manager import CHANNEL_TRADE_EXECUTED, Manager  # noqa: E402
from app.risk.engine import RiskEngine  # noqa: E402


def _setup_signal(db_session, *, rationale_entry: float = 2500.0):
    acct = BrokerAccount(name="paper", broker="fyers", paper_mode=True, enabled=True)
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    strat = Strategy(name="s", enabled=True, config={"broker_account_id": acct.id})
    db_session.add(strat)
    db_session.commit()
    db_session.refresh(strat)
    sig = Signal(
        symbol="RELIANCE", action="BUY", confidence=0.9, status="pending",
        strategy_id=strat.id,
        rationale=(
            f"entry={rationale_entry:.2f} sl={rationale_entry * 0.94:.2f} "
            f"target={rationale_entry * 1.18:.2f} rr=3.00"
        ),
    )
    db_session.add(sig)
    db_session.commit()
    db_session.refresh(sig)
    return acct, strat, sig


@pytest.mark.asyncio
async def test_manager_drift_block_arms_retracement(db_session, isolated_db) -> None:
    """Full pipeline E-03: live quote 2.4% above the analysis entry →
    EXCESSIVE_DRIFT, no trade, a RiskEvent for the audit trail, and the
    signal parked in the retracement monitor (still `pending`)."""
    md = MarketDataBus()
    # Analysis priced the signal at 2500; the market has already run.
    md.set_quote_sync(
        "RELIANCE", last_price=2560.0, bid=2559.0, ask=2560.0,
        average_daily_volume_crore=50.0,
    )
    _acct, _strat, sig = _setup_signal(db_session, rationale_entry=2500.0)
    mgr = Manager(
        risk_engine=RiskEngine(market_data=md, portfolio_value=10_000_000.0),
        market_data=md,
    )
    try:
        out = await mgr.process_signal(sig.id)
        assert out is not None
        assert out["approved"] is False
        assert out["code"] == "EXCESSIVE_DRIFT"
        assert out["retracement_watch"] is True
        assert mgr.retracement_monitor.watching(sig.id)
        # No trade reached a backend.
        assert (
            db_session.query(TradeRow)
            .filter(TradeRow.symbol == "RELIANCE", TradeRow.status == "filled")
            .count() == 0
        )
        # Audit trail: an EXCESSIVE_DRIFT risk event, signal still pending.
        evt = (
            db_session.query(RiskEvent)
            .filter(RiskEvent.event_type == "EXCESSIVE_DRIFT")
            .one()
        )
        assert evt.context["signal_id"] == sig.id
        db_session.refresh(sig)
        assert sig.status == "pending"
    finally:
        mgr.retracement_monitor.stop()


@pytest.mark.asyncio
async def test_manager_partial_fill_hands_over_exact_quantity(
    db_session, isolated_db
) -> None:
    """Full pipeline E-06: a live-style backend partially fills. The
    trade row records the FILLED quantity and `trade.executed` hands the
    TradeManager exactly that many shares with the original stop."""
    md = MarketDataBus()
    md.set_quote_sync(
        "RELIANCE", last_price=2500.0, bid=2499.5, ask=2500.0,
        average_daily_volume_crore=50.0,
    )
    acct, _strat, sig = _setup_signal(db_session, rationale_entry=2500.0)
    backend = StubBackend()
    backend.queue_place(lambda kw: pending_result(kw, order_id="STUB-1"))
    backend.queue_status(OrderStatus(
        broker_order_id="STUB-1", state=OrderState.PENDING,
        filled_quantity=3, average_price=2500.5,
    ))
    fast = make_settings()
    mgr = Manager(
        risk_engine=RiskEngine(market_data=md, portfolio_value=10_000_000.0),
        market_data=md,
        backends={acct.id: backend},
        entry_manager=EntryManager(market_data=md, settings_provider=lambda: fast),
    )
    events = event_bus.subscribe(CHANNEL_TRADE_EXECUTED)
    try:
        out = await mgr.process_signal(sig.id)
        assert out is not None
        assert out["code"] == OUTCOME_PARTIAL_FILL
        assert out["qty"] == 3
        assert out["requested_qty"] > 3
        # The remainder was cancelled at the broker.
        assert backend.cancelled == ["STUB-1"]
        # Trade row: filled with the ACTUAL quantity.
        trade = (
            db_session.query(TradeRow)
            .filter(TradeRow.broker_order_id == "STUB-1")
            .one()
        )
        assert trade.status == "filled"
        assert trade.quantity == 3
        assert trade.price == pytest.approx(2500.5)
        # Position mirrored once, with the filled quantity.
        pos = db_session.query(PositionRow).filter_by(symbol="RELIANCE").one()
        assert pos.quantity == 3
        # The handover event carries the exact fill + original stop.
        evt = events.get_nowait()
        assert evt.payload["quantity"] == 3
        assert evt.payload["state"] == "FILLED"
        assert evt.payload["entry"] == pytest.approx(2500.5)
        assert evt.payload["stop_loss"] == pytest.approx(2500.0 * 0.94)
    finally:
        event_bus.unsubscribe(CHANNEL_TRADE_EXECUTED, events)
        mgr.retracement_monitor.stop()


@pytest.mark.asyncio
async def test_reconcile_dedupes_ws_and_postback(db_session, isolated_db) -> None:
    """E-11 at the DB layer: the same fill arriving via order-WS and
    postback applies exactly once, and a late cancel echo (IOC partials
    end 'cancelled' at Fyers) never downgrades a filled row."""
    from app.execution.order_reconcile import reconcile_order_update

    trade = TradeRow(
        symbol="RELIANCE", side="BUY", quantity=100, price=0.0,
        order_type="limit", status="placed", broker_order_id="XYZ1",
    )
    db_session.add(trade)
    db_session.commit()

    fill = {"id": "XYZ1", "status": 2, "symbol": "NSE:RELIANCE-EQ", "tradedPrice": 100.2}
    out1 = await reconcile_order_update(db_session, fill, source="fyers_order_ws")
    assert out1["matched"] is True and out1["status"] == "filled"
    pos = db_session.query(PositionRow).filter_by(symbol="RELIANCE").one()
    assert pos.quantity == 100

    # The duplicate (postback) confirmation is acknowledged, not re-applied.
    out2 = await reconcile_order_update(db_session, fill, source="fyers_postback")
    assert out2.get("deduped") is True
    db_session.refresh(pos)
    assert pos.quantity == 100  # NOT doubled

    # A late cancel echo never downgrades the fill.
    cancel = {"id": "XYZ1", "status": 1, "symbol": "NSE:RELIANCE-EQ"}
    out3 = await reconcile_order_update(db_session, cancel, source="fyers_postback")
    assert out3.get("deduped") is True
    db_session.refresh(trade)
    assert trade.status == "filled"
