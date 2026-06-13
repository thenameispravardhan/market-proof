"""Tests for the execution manager.

Coverage:

  - Paper mode (TRADING_MODE=paper or account.paper_mode=True)
    routes to PaperBackend.
  - Live mode routes to FyersLiveBackend when the account has
    app_id + access_token.
  - Multi-account routing: different strategies on different
    accounts go to the right backend.
  - Risk engine blocks: signal is marked blocked, no order placed,
    `risk.blocked` event fires, `risk_events` row written.
  - Risk engine approves: order placed, trade row persisted,
    `trade.executed` event fires.
  - Settings hot-reload: `settings.updated` event causes the
    manager to drop FyersLiveBackend instances.
  - Loop end-to-end via the event bus: publish signals.new, manager
    processes, places order, fires trade.executed.
  - parse_rationale_levels helper.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import select

from app.analyzer.service import CHANNEL_NEW_SIGNAL
from app.config import get_settings
from app.db.models import (
    AuditLog,
    BrokerAccount,
    RiskEvent,
    Signal,
    Strategy,
    Trade as TradeRow,
)
from app.execution.base import (
    OrderResult,
    OrderSide,
    OrderState,
    OrderType,
    Position,
    TradingBackend,
)
from app.execution.manager import (
    CHANNEL_RISK_BLOCKED,
    CHANNEL_TRADE_EXECUTED,
    Manager,
    parse_rationale_levels,
)
from app.execution.market_data import MarketDataBus
from app.execution.paper import PaperBackend
from app.risk.engine import RiskEngine
from app.services.event_bus import event_bus


# -- Stub backend for tests that want a known response --------------------


class _StubBackend:
    """A minimal TradingBackend stub. Records all calls and
    returns a configurable OrderResult."""

    def __init__(self, name: str = "stub", result: OrderResult | None = None) -> None:
        self.name = name
        self.broker_account_id: int | None = None
        self.calls: list[dict[str, Any]] = []
        self._result = result  # may be None — we synthesise per call.

    async def place_order(self, *, signal, symbol, side, quantity,
                          order_type=OrderType.MARKET, limit_price=None, stop_price=None) -> OrderResult:
        self.calls.append({
            "method": "place_order",
            "symbol": symbol, "side": side, "quantity": int(quantity),
            "order_type": order_type,
        })
        if self._result is not None:
            return self._result
        # Synthesise a per-call result echoing the symbol.
        return OrderResult(
            broker_order_id=f"STUB-{symbol}",
            state=OrderState.FILLED,
            symbol=symbol,
            side=side,
            quantity=int(quantity),
            order_type=order_type,
            filled_quantity=int(quantity),
            average_price=1000.0,
            raw={"stub": True},
        )

    async def cancel_order(self, broker_order_id: str) -> bool:
        self.calls.append({"method": "cancel_order", "broker_order_id": broker_order_id})
        return True

    async def get_positions(self) -> list[Position]:
        return []

    async def get_order_status(self, broker_order_id: str) -> "OrderStatus":  # type: ignore[name-defined]
        from app.execution.base import OrderStatus
        return OrderStatus(broker_order_id=broker_order_id, state=OrderState.FILLED)


# -- parse_rationale_levels ----------------------------------------------


def test_parse_rationale_levels_extracts_numbers() -> None:
    rat = "Rule matched. entry=2500.50 sl=2475.00 target=2575.00 rr=3.00"
    levels = parse_rationale_levels(rat)
    assert levels["entry"] == 2500.50
    assert levels["stop_loss"] == 2475.00
    assert levels["target"] == 2575.00
    assert levels["rr"] == 3.00


def test_parse_rationale_levels_empty_when_no_match() -> None:
    assert parse_rationale_levels("no numbers here") == {}
    assert parse_rationale_levels(None) == {}
    assert parse_rationale_levels("") == {}


# -- Fixtures -------------------------------------------------------------


def _make_signal(
    db_session, *,
    symbol: str = "RELIANCE",
    action: str = "BUY",
    confidence: float = 0.9,
    strategy_id: int | None = None,
    rationale: str = "entry=2500.00 sl=2475.00 target=2575.00 rr=3.00",
) -> Signal:
    s = Signal(
        symbol=symbol,
        action=action,
        confidence=confidence,
        status="pending",
        strategy_id=strategy_id,
        rationale=rationale,
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


def _make_strategy(
    db_session, name: str = "test", config: dict | None = None
) -> Strategy:
    s = Strategy(name=name, enabled=True, config=config or {})
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


def _make_account(
    db_session,
    *,
    name: str = "main",
    paper_mode: bool = True,
    app_id: str | None = "APP",
    secret_key: str | None = "sk",
    access_token: str | None = "tk",
) -> BrokerAccount:
    a = BrokerAccount(
        name=name, broker="fyers", app_id=app_id, secret_key=secret_key,
        access_token=access_token, paper_mode=paper_mode, enabled=True,
    )
    db_session.add(a)
    db_session.commit()
    db_session.refresh(a)
    return a


# -- Paper mode routing --------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_account_blocks_signal(db_session, isolated_db):
    """The dashboard Paper/Fyers off switch flips account.enabled=False;
    the manager must block any signal routed to a disabled account."""
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.0)
    paper = PaperBackend(market_data=md, session_factory=lambda: db_session)
    paper.start()
    try:
        account = _make_account(db_session, name="off-acct", paper_mode=True)
        account.enabled = False
        strat = _make_strategy(
            db_session, "off-route", config={"broker_account_id": account.id}
        )
        sig = _make_signal(db_session, symbol="RELIANCE", action="BUY", strategy_id=strat.id)
        db_session.commit()

        risk = RiskEngine(market_data=md, portfolio_value=50_000_000.0)
        mgr = Manager(risk_engine=risk, market_data=md, paper_backend=paper)
        outcome = await mgr.process_signal(sig.id)
        assert outcome is not None
        assert outcome["approved"] is False
        assert outcome["code"] == "ACCOUNT_DISABLED"
        # No order was placed.
        rows = db_session.execute(
            select(TradeRow).where(TradeRow.symbol == "RELIANCE", TradeRow.status == "filled")
        ).scalars().all()
        assert len(rows) == 0
    finally:
        await paper.stop()


@pytest.mark.asyncio
async def test_paper_mode_routes_to_paper_backend(db_session, isolated_db):
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.0)
    paper = PaperBackend(market_data=md, session_factory=lambda: db_session)
    paper.start()
    try:
        strat = _make_strategy(
            db_session, "paper-route",
            config={"broker_account_id": None, "max_capital_risk_pct": 0.05},
        )
        # Account is paper by default.
        account = _make_account(db_session, name="paper-acct", paper_mode=True)
        sig = _make_signal(db_session, symbol="RELIANCE", action="BUY", strategy_id=strat.id)
        # Pre-seed the strategy's config to point at the account.
        strat.config = {"broker_account_id": account.id, "max_capital_risk_pct": 0.05}
        db_session.commit()

        risk = RiskEngine(
            market_data=md,
            portfolio_value=50_000_000.0,
        )
        mgr = Manager(risk_engine=risk, market_data=md, paper_backend=paper)
        outcome = await mgr.process_signal(sig.id)
        assert outcome is not None
        assert outcome["approved"] is True
        # The trade was placed (filled immediately since the quote
        # is cached).
        rows = db_session.execute(
            select(TradeRow).where(TradeRow.symbol == "RELIANCE")
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].symbol == "RELIANCE"
        assert rows[0].status == "filled"
    finally:
        await paper.stop()


# -- Live mode routing ----------------------------------------------------


@pytest.mark.asyncio
async def test_live_mode_routes_to_fyers_backend(db_session, isolated_db, monkeypatch):
    """In live mode with a real-looking Fyers account, the manager
    should construct a FyersLiveBackend (which fails to talk to
    the network in this test env but the test verifies routing
    via the stub registry)."""
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.0)
    strat = _make_strategy(
        db_session, "live-route",
        config={"max_capital_risk_pct": 0.05},
    )
    account = _make_account(
        db_session, name="live-acct", paper_mode=False,
        app_id="APP-LIVE-1", secret_key="SK", access_token="TK",
    )
    strat.config = {"broker_account_id": account.id, "max_capital_risk_pct": 0.05}
    db_session.commit()
    sig = _make_signal(db_session, symbol="RELIANCE", action="BUY", strategy_id=strat.id)

    # Stub the FyersLiveBackend.
    stub = _StubBackend(name="fyers:live-acct")
    risk = RiskEngine(market_data=md, portfolio_value=50_000_000.0)
    mgr = Manager(risk_engine=risk, market_data=md)
    # Force TRADING_MODE=paper so the global kill-switch doesn't
    # force the paper backend. We want the account to drive the
    # routing.
    monkeypatch.setenv("TRADING_MODE", "live")
    from app import config as app_config
    app_config.reset_settings_cache()
    mgr.register_backend(account.id, stub)
    try:
        outcome = await mgr.process_signal(sig.id)
        assert outcome is not None
        assert outcome["approved"] is True
        assert outcome["backend"] == "fyers:live-acct"
        # The stub was called.
        assert any(c["method"] == "place_order" for c in stub.calls)
        assert outcome["broker_order_id"] == "STUB-RELIANCE"
    finally:
        app_config.reset_settings_cache()


# -- Multi-account routing -----------------------------------------------


@pytest.mark.asyncio
async def test_multi_account_routes_to_right_backend(db_session, isolated_db, monkeypatch):
    """Two strategies, two accounts (one paper, one live). Each
    signal should go to the right backend."""
    # Clean up state from prior tests in the same module.
    # Use the SessionLocal (the manager's session factory uses
    # the same in-memory engine) to ensure rows are gone.
    from app.db.session import SessionLocal
    with SessionLocal() as s:
        s.query(TradeRow).delete()
        s.query(Signal).delete()
        s.query(Strategy).delete()
        s.query(BrokerAccount).delete()
        s.query(RiskEvent).delete()
        s.query(AuditLog).delete()
        s.commit()
    db_session.expire_all()
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.0)
    md.set_quote_sync("TCS", last_price=4000.0)

    # Account 1: paper, owns RELIANCE strategy.
    paper_acct = _make_account(db_session, name="paper-1", paper_mode=True)
    # Account 2: live, owns TCS strategy.
    live_acct = _make_account(
        db_session, name="live-1", paper_mode=False,
        app_id="APP-LIVE-1", secret_key="SK", access_token="TK",
    )

    s_reliance = _make_strategy(
        db_session, "reliance-strat",
        config={"max_capital_risk_pct": 0.05},
    )
    s_reliance.config = {"broker_account_id": paper_acct.id, "max_capital_risk_pct": 0.05}
    s_tcs = _make_strategy(
        db_session, "tcs-strat",
        config={"max_capital_risk_pct": 0.05},
    )
    s_tcs.config = {"broker_account_id": live_acct.id, "max_capital_risk_pct": 0.05}
    db_session.commit()

    sig_reliance = _make_signal(
        db_session, symbol="RELIANCE", action="BUY", strategy_id=s_reliance.id,
    )
    sig_tcs = _make_signal(
        db_session, symbol="TCS", action="BUY", strategy_id=s_tcs.id,
    )

    risk = RiskEngine(market_data=md, portfolio_value=50_000_000.0)
    mgr = Manager(risk_engine=risk, market_data=md)
    # Force live so the global kill-switch doesn't pre-empt.
    monkeypatch.setenv("TRADING_MODE", "live")
    from app import config as app_config
    app_config.reset_settings_cache()

    # Pre-seed a stub for the live account.
    stub_live = _StubBackend(name="fyers:live-1")
    mgr.register_backend(live_acct.id, stub_live)

    try:
        # Paper signal: goes to PaperBackend (default).
        out1 = await mgr.process_signal(sig_reliance.id)
        # Live signal: goes to stub.
        out2 = await mgr.process_signal(sig_tcs.id)
        assert out1["approved"] is True
        assert out2["approved"] is True
        # Paper placed a real trade.
        rows = db_session.execute(
            select(TradeRow).where(TradeRow.symbol == "RELIANCE")
        ).scalars().all()
        assert len(rows) == 1
        # Live was a stub — manager persists a row from the
        # OrderResult.
        live_rows = db_session.execute(
            select(TradeRow).where(TradeRow.symbol == "TCS")
        ).scalars().all()
        assert len(live_rows) == 1
        # Stub received the call.
        assert any(c["symbol"] == "TCS" for c in stub_live.calls)
    finally:
        app_config.reset_settings_cache()


# -- Risk engine blocks --------------------------------------------------


@pytest.mark.asyncio
async def test_risk_block_writes_risk_event_and_does_not_place_order(db_session, isolated_db):
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.0)
    strat = _make_strategy(
        db_session, "block-test",
        config={"symbol_blocklist": ["RELIANCE"]},
    )
    account = _make_account(db_session, name="blk-acct", paper_mode=True)
    strat.config = {"broker_account_id": account.id, "symbol_blocklist": ["RELIANCE"]}
    db_session.commit()
    sig = _make_signal(
        db_session, symbol="RELIANCE", action="BUY", strategy_id=strat.id,
    )

    paper = PaperBackend(market_data=md, session_factory=lambda: db_session)
    risk = RiskEngine(market_data=md, portfolio_value=50_000_000.0)
    mgr = Manager(risk_engine=risk, market_data=md, paper_backend=paper)
    # Subscribe to the risk.blocked channel.
    sub = event_bus.subscribe(CHANNEL_RISK_BLOCKED)
    try:
        outcome = await mgr.process_signal(sig.id)
        assert outcome is not None
        assert outcome["approved"] is False
        assert outcome["code"] == "RISK_SYMBOL_BLOCKLISTED"
        # risk_events row written.
        ev = db_session.execute(
            select(RiskEvent).where(RiskEvent.event_type == "RISK_SYMBOL_BLOCKLISTED")
        ).scalar_one()
        assert ev.message
        # signal.status = blocked.
        db_session.refresh(sig)
        assert sig.status == "blocked"
        # No trade row from a successful place.
        rows = db_session.execute(
            select(TradeRow).where(TradeRow.symbol == "RELIANCE")
        ).scalars().all()
        # There is a "blocked" trade row (rejected), but no filled
        # or pending row.
        assert all(r.status == "rejected" for r in rows)
        assert len(rows) >= 1
        # The risk.blocked event was published.
        evt = sub.get_nowait()
        assert evt.payload["signal_id"] == sig.id
        assert evt.payload["code"] == "RISK_SYMBOL_BLOCKLISTED"
    finally:
        event_bus.unsubscribe(CHANNEL_RISK_BLOCKED, sub)


# -- Approved signal: trade.executed event -------------------------------


@pytest.mark.asyncio
async def test_approved_signal_publishes_trade_executed(db_session, isolated_db):
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.0)
    strat = _make_strategy(
        db_session, "exec-test",
        config={"max_capital_risk_pct": 0.05},
    )
    account = _make_account(db_session, name="exec-acct", paper_mode=True)
    strat.config = {"broker_account_id": account.id, "max_capital_risk_pct": 0.05}
    db_session.commit()
    sig = _make_signal(
        db_session, symbol="RELIANCE", action="BUY", strategy_id=strat.id,
    )
    paper = PaperBackend(market_data=md, session_factory=lambda: db_session)
    risk = RiskEngine(market_data=md, portfolio_value=50_000_000.0)
    mgr = Manager(risk_engine=risk, market_data=md, paper_backend=paper)
    sub = event_bus.subscribe(CHANNEL_TRADE_EXECUTED)
    try:
        outcome = await mgr.process_signal(sig.id)
        assert outcome["approved"] is True
        evt = sub.get_nowait()
        assert evt.payload["signal_id"] == sig.id
        assert evt.payload["symbol"] == "RELIANCE"
        assert evt.payload["state"] == "FILLED"
    finally:
        event_bus.unsubscribe(CHANNEL_TRADE_EXECUTED, sub)


# -- Settings hot-reload -------------------------------------------------


@pytest.mark.asyncio
async def test_settings_updated_drops_fyers_backends(db_session, isolated_db, monkeypatch):
    md = MarketDataBus()
    risk = RiskEngine(market_data=md, portfolio_value=50_000_000.0)
    mgr = Manager(risk_engine=risk, market_data=md)
    # Pre-seed a FyersLiveBackend.
    from app.execution.fyers_live import FyersLiveBackend
    fe = FyersLiveBackend(
        app_id="APP", access_token="TK", broker_account_id=42, account_name="x",
    )
    mgr.register_backend(42, fe)
    assert 42 in mgr._backends  # type: ignore[attr-defined]
    # Publish settings.updated.
    await event_bus.publish("settings.updated", {"changed_keys": ["TRADING_MODE"]})
    # Drain.
    await mgr._on_settings_updated({"changed_keys": ["TRADING_MODE"]})
    assert 42 not in mgr._backends  # type: ignore[attr-defined]


# -- End-to-end via the bus ----------------------------------------------


@pytest.mark.asyncio
async def test_loop_processes_published_signal(db_session, isolated_db):
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.0)
    strat = _make_strategy(
        db_session, "loop-test",
        config={"max_capital_risk_pct": 0.05},
    )
    account = _make_account(db_session, name="loop-acct", paper_mode=True)
    strat.config = {"broker_account_id": account.id, "max_capital_risk_pct": 0.05}
    db_session.commit()
    sig = _make_signal(
        db_session, symbol="RELIANCE", action="BUY", strategy_id=strat.id,
    )

    paper = PaperBackend(market_data=md, session_factory=lambda: db_session)
    risk = RiskEngine(market_data=md, portfolio_value=50_000_000.0)
    mgr = Manager(risk_engine=risk, market_data=md, paper_backend=paper)
    task = mgr.start()
    sub = event_bus.subscribe(CHANNEL_TRADE_EXECUTED)
    try:
        await mgr.wait_until_ready(timeout=2.0)
        await event_bus.publish(
            CHANNEL_NEW_SIGNAL,
            {
                "signal_id": sig.id,
                "symbol": "RELIANCE",
                "action": "BUY",
                "approved": True,
            },
        )
        # Wait for the trade row.
        for _ in range(50):
            n = db_session.execute(select(TradeRow)).scalars().all()
            if n:
                break
            await asyncio.sleep(0.05)
        assert n, "trade row was not created"
        # Trade.executed event fired.
        evt = sub.get_nowait()
        assert evt.payload["symbol"] == "RELIANCE"
    finally:
        event_bus.unsubscribe(CHANNEL_TRADE_EXECUTED, sub)
        mgr.stop()
        await mgr.wait_until_stopped()
