"""Integration test: the very next signal after a TRADING_MODE
toggle routes to the right backend.

This is the verifier's "cosmetic toggle" fix: the toggle must be
honoured by the execution manager on the very next signal, not
just by the response body.

We exercise it via the Manager + a stub FyersLiveBackend. The
endpoint itself is exercised in test_trading_mode_api.py; here we
focus on the routing side-effect.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings, reset_settings_cache
from app.db.models import (
    AuditLog,
    BrokerAccount,
    Signal,
    Strategy,
)
from app.execution.base import (
    OrderResult,
    OrderSide,
    OrderState,
    OrderType,
    Position,
)
from app.execution.manager import Manager
from app.execution.market_data import MarketDataBus
from app.execution.paper import PaperBackend
from app.risk.engine import RiskEngine
from app.services.event_bus import event_bus


# -- Stub for the live backend -------------------------------------------


class _LiveStub:
    """A minimal FyersLiveBackend stub that records the place_order
    call and returns a FILLED OrderResult echoing the input symbol."""

    name = "fyers:live"
    broker_account_id = 99

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def place_order(self, *, signal, symbol, side, quantity,
                          order_type=OrderType.MARKET, limit_price=None, stop_price=None):
        self.calls.append({"method": "place_order", "symbol": symbol, "quantity": int(quantity)})
        return OrderResult(
            broker_order_id=f"STUB-{symbol}",
            state=OrderState.FILLED,
            symbol=symbol,
            side=side,
            quantity=int(quantity),
            order_type=order_type,
            filled_quantity=int(quantity),
            average_price=2500.0,
            raw={"stub": True},
        )

    async def cancel_order(self, broker_order_id: str) -> bool:
        return True

    async def get_positions(self) -> list[Position]:
        return []

    async def get_order_status(self, broker_order_id: str):
        from app.execution.base import OrderStatus
        return OrderStatus(broker_order_id=broker_order_id, state=OrderState.FILLED)


# -- Helpers --------------------------------------------------------------


def _make_signal(db_session, *, symbol: str, action: str, strategy_id) -> Signal:
    s = Signal(
        symbol=symbol,
        action=action,
        confidence=0.9,
        status="pending",
        strategy_id=strategy_id,
        rationale="entry=2500.00 sl=2475.00 target=2575.00 rr=3.00",
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


def _make_live_account(db_session, *, name: str = "live-acct-routing") -> BrokerAccount:
    a = BrokerAccount(
        name=name,
        broker="fyers",
        app_id="APP-LIVE-1",
        secret_key="SK",
        access_token="TK",
        paper_mode=False,
        enabled=True,
    )
    db_session.add(a)
    db_session.commit()
    db_session.refresh(a)
    return a


# -- The actual test ------------------------------------------------------


@pytest.mark.asyncio
async def test_toggle_then_next_signal_routes_to_live(
    db_session, isolated_db, client: TestClient
) -> None:
    """The verifier bug: POST /api/settings/trading-mode with mode=live
    was cosmetic — the in-memory Settings.TRADING_MODE stayed at
    'paper' so the next signal still routed to PaperBackend.

    After the fix:
      1. The toggle endpoint updates the process env and clears
         the lru_cache on get_settings.
      2. The very next signal routes to the live backend (mocked
         FyersLiveBackend here), not the paper backend.
    """
    from app.db.session import SessionLocal as SL

    # 1. Seed a strategy + a live broker account pointing at the
    # stub. The signal carries a sane risk profile so the engine
    # approves.
    with SL() as s:
        # Clear any leftover state from prior tests.
        s.query(AuditLog).filter_by(action="settings.trading_mode_changed").delete()
        s.query(Signal).delete()
        s.query(Strategy).delete()
        s.query(BrokerAccount).delete()
        s.commit()
        account = _make_live_account(s)
        strat = Strategy(
            name="toggle-test",
            enabled=True,
            config={"broker_account_id": account.id, "max_capital_risk_pct": 0.05},
        )
        s.add(strat)
        s.commit()
        s.refresh(strat)
        sig = _make_signal(s, symbol="RELIANCE", action="BUY", strategy_id=strat.id)
    s.commit()
    # Re-read in the test session so the in-memory objects match.
    db_session.expire_all()

    # 2. Make sure TRADING_MODE starts at "paper" (default for tests).
    reset_settings_cache()
    assert get_settings().TRADING_MODE == "paper"

    # 3. Build the manager — pre-seed the live stub for the account.
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.0)
    paper = PaperBackend(market_data=md, session_factory=db_session)
    risk = RiskEngine(market_data=md, portfolio_value=50_000_000.0)
    mgr = Manager(risk_engine=risk, market_data=md, paper_backend=paper)
    stub = _LiveStub()
    mgr.register_backend(account.id, stub)

    # 4. Flip the toggle to "live" via the HTTP endpoint.
    r = client.post(
        "/api/settings/trading-mode",
        json={"mode": "live", "confirm": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Defence-in-depth: the response already says effective=live
    # (this is the cosmetic-toggle fix the verifier called out).
    assert body["effective"] == "live", (
        f"verifier bug: effective={body.get('effective')!r} should be 'live'"
    )
    # And the live env var / Settings cache reflect the new mode.
    assert get_settings().TRADING_MODE == "live"
    assert get_settings().is_paper is False

    # 5. The very next signal must route to the LIVE backend, not
    # the paper backend. This is the verifier's main concern.
    outcome = await mgr.process_signal(sig.id)
    assert outcome is not None
    assert outcome["approved"] is True, outcome
    # The stub received the call (not paper's own pipeline).
    assert len(stub.calls) == 1, (
        f"live stub should have been called once; got {len(stub.calls)} calls"
    )
    assert stub.calls[0]["symbol"] == "RELIANCE"
    # The outcome reports the live backend.
    assert "live" in outcome["backend"].lower(), (
        f"backend should be the live one, got {outcome.get('backend')!r}"
    )

    # 6. Reset to paper for any subsequent tests.
    client.post(
        "/api/settings/trading-mode",
        json={"mode": "paper", "confirm": True},
    )
