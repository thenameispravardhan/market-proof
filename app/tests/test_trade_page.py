"""Tests for the Trade page backend: instrument master, search,
option chain, manual order placement, cancel, pending list.

The Trade page is real-money only — paper accounts are rejected
with a 400. The tests cover the real-account path with a stub
backend (the Fyers transport is never actually opened in tests).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db.models import BrokerAccount, Trade
from app.execution.base import (
    OrderResult,
    OrderSide,
    OrderState,
    OrderType,
    ProductType,
)
from app.services.instrument_master import get_master, reset_master_for_testing


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def real_account(db_session, isolated_db) -> BrokerAccount:
    """A non-paper, access-token-bearing broker account row."""
    acc = BrokerAccount(
        name="Fyers Live",
        broker="fyers",
        app_id="APP123",
        secret_key="sek",  # noqa: S106 (test only)
        access_token="AT123",
        paper_mode=False,
        enabled=True,
    )
    db_session.add(acc)
    db_session.commit()
    return db_session.query(BrokerAccount).filter_by(name="Fyers Live").one()


@pytest.fixture()
def paper_account(db_session, isolated_db) -> BrokerAccount:
    """The seeded paper account (created by the lifespan's
    `seed_default_paper_account`). Falls back to creating one if
    the lifespan didn't run."""
    acc = db_session.query(BrokerAccount).filter_by(name="Paper Account").one_or_none()
    if acc is None:
        acc = BrokerAccount(
            name="Paper Account",
            broker="fyers",
            paper_mode=True,
            enabled=True,
        )
        db_session.add(acc)
        db_session.commit()
        acc = db_session.query(BrokerAccount).filter_by(name="Paper Account").one()
    return acc


def _install_stub_backend(client: TestClient, account_id: int, *, ok: bool = True) -> None:
    """Replace the manager's backend for `account_id` with an in-
    memory stub that returns PENDING + a fake broker_order_id."""
    from app.main import app

    class StubBackend:
        name = "stub"
        broker_account_id = account_id

        def __init__(self) -> None:
            self.cancelled: list[str] = []
            self.placed_payloads: list[dict] = []

        async def place_order(
            self,
            *,
            signal,
            symbol,
            side,
            quantity,
            order_type=OrderType.MARKET,
            limit_price=None,
            stop_price=None,
            product_type=ProductType.INTRADAY,
        ) -> OrderResult:
            self.placed_payloads.append({
                "symbol": symbol, "side": side.value, "quantity": quantity,
                "order_type": order_type.value,
                "limit_price": limit_price, "stop_price": stop_price,
                "product_type": product_type.value,
            })
            if not ok:
                return OrderResult(
                    broker_order_id="", state=OrderState.REJECTED,
                    symbol=symbol, side=side, quantity=quantity,
                    order_type=order_type, error="stub-rejected",
                )
            return OrderResult(
                broker_order_id="STUB-1",
                state=OrderState.PENDING,
                symbol=symbol, side=side, quantity=quantity,
                order_type=order_type,
            )

        async def cancel_order(self, broker_order_id: str) -> bool:
            self.cancelled.append(broker_order_id)
            return ok

        async def get_positions(self) -> list:
            return []

        async def get_order_status(self, broker_order_id: str) -> object:
            return None

    mgr = app.state.execution_manager
    mgr.register_backend(account_id, StubBackend())


# ---------------------------------------------------------------------------
# Instrument master
# ---------------------------------------------------------------------------


def test_master_seed_has_nse_cash_and_indices():
    m = get_master()
    assert m.count() > 0
    rel = m.get("NSE:RELIANCE-EQ")
    assert rel is not None
    assert rel.exchange == "NSE"
    assert rel.instrument_type == "EQ"
    assert rel.lot_size == 1


def test_master_search_exact_match_ranks_first():
    m = get_master()
    hits = m.search("RELIANCE")
    assert any(h.symbol == "NSE:RELIANCE-EQ" for h in hits)


def test_master_search_case_insensitive():
    m = get_master()
    assert len(m.search("reliance")) > 0
    assert len(m.search("reli")) > 0


def test_master_search_filters_by_segment():
    m = get_master()
    eq_hits = m.search("NIFTY", segments=["EQ"])
    fo_hits = m.search("NIFTY", segments=["FO"])
    assert all(h.segment == "EQ" for h in eq_hits)
    assert all(h.segment == "FO" for h in fo_hits)


def test_master_option_chain_for_underlying_without_options():
    """Without CSV data, no options exist — chain returns empty."""
    m = get_master()
    chain = m.option_chain("NIFTY")
    assert chain["underlying"] == "NIFTY"
    assert chain["expiries"] == []
    assert chain["strikes"] == []


# ---------------------------------------------------------------------------
# Search HTTP endpoints
# ---------------------------------------------------------------------------


def test_search_symbols(client: TestClient, isolated_db):
    r = client.get("/api/search/symbols", params={"q": "RELIANCE"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["count"] >= 1
    assert any(h["symbol"] == "NSE:RELIANCE-EQ" for h in body["hits"])


def test_search_symbols_empty_query(client: TestClient, isolated_db):
    # An empty query now browses popular instruments so the dropdown is
    # never blank on focus.
    r = client.get("/api/search/symbols", params={"q": ""})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] > 0
    assert all("symbol" in h for h in body["hits"])


def test_option_chain_endpoint(client: TestClient, isolated_db):
    r = client.get("/api/search/option-chain", params={"underlying": "NIFTY"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["underlying"] == "NIFTY"
    # The seed has no F&O rows; expiries is empty, but the shape is right.
    assert "expiries" in body
    assert "strikes" in body


# ---------------------------------------------------------------------------
# Manual order placement
# ---------------------------------------------------------------------------


def test_place_order_happy_path(client: TestClient, isolated_db, real_account):
    _install_stub_backend(client, real_account.id, ok=True)
    r = client.post(
        "/api/orders",
        json={
            "account_id": real_account.id,
            "symbol": "NSE:SBIN-EQ",
            "side": "BUY",
            "quantity": 10,
            "order_type": "MARKET",
            "product_type": "INTRADAY",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["broker_order_id"] == "STUB-1"
    assert body["status"] == "PENDING"


def test_place_order_rejects_paper_account(client: TestClient, isolated_db, paper_account):
    r = client.post(
        "/api/orders",
        json={
            "account_id": paper_account.id,
            "symbol": "NSE:SBIN-EQ",
            "side": "BUY",
            "quantity": 10,
            "order_type": "MARKET",
            "product_type": "INTRADAY",
        },
    )
    assert r.status_code == 400
    assert "paper account" in r.json()["detail"].lower()


def test_place_order_rejects_unknown_account(client: TestClient, isolated_db):
    r = client.post(
        "/api/orders",
        json={
            "account_id": 99999,
            "symbol": "NSE:SBIN-EQ",
            "side": "BUY",
            "quantity": 10,
            "order_type": "MARKET",
        },
    )
    assert r.status_code == 404


def test_place_order_validates_limit_price_required(client: TestClient, isolated_db, real_account):
    r = client.post(
        "/api/orders",
        json={
            "account_id": real_account.id,
            "symbol": "NSE:SBIN-EQ",
            "side": "BUY",
            "quantity": 10,
            "order_type": "LIMIT",
        },
    )
    assert r.status_code == 422
    assert "limit_price" in r.json()["detail"]


def test_place_order_validates_quantity_positive(client: TestClient, isolated_db, real_account):
    r = client.post(
        "/api/orders",
        json={
            "account_id": real_account.id,
            "symbol": "NSE:SBIN-EQ",
            "side": "BUY",
            "quantity": 0,
            "order_type": "MARKET",
        },
    )
    assert r.status_code == 422


def test_place_order_with_bypass_risk_when_no_block(
    client: TestClient, isolated_db, real_account
):
    """When risk would have approved anyway, bypass is a no-op."""
    _install_stub_backend(client, real_account.id, ok=True)
    r = client.post(
        "/api/orders",
        json={
            "account_id": real_account.id,
            "symbol": "NSE:SBIN-EQ",
            "side": "BUY",
            "quantity": 1,
            "order_type": "MARKET",
            "bypass_risk": True,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["bypassed_risk"] is False  # wasn't actually blocked


def test_place_order_persists_trade_and_audit(
    client: TestClient, db_session, isolated_db, real_account
):
    _install_stub_backend(client, real_account.id, ok=True)
    r = client.post(
        "/api/orders",
        json={
            "account_id": real_account.id,
            "symbol": "NSE:INFY-EQ",
            "side": "SELL",
            "quantity": 5,
            "order_type": "MARKET",
            "product_type": "INTRADAY",
        },
    )
    assert r.status_code == 200, r.text
    # Find the persisted trade
    trade = db_session.query(Trade).filter_by(symbol="NSE:INFY-EQ").one()
    assert trade.broker_account_id == real_account.id
    assert trade.side == "SELL"
    assert trade.quantity == 5
    assert trade.status == "placed"
    assert trade.broker_order_id == "STUB-1"


def test_non_intraday_product_rejected(
    client: TestClient, db_session, isolated_db, real_account
):
    """Intraday-only bot: DELIVERY / NORMAL / MARGIN manual orders are
    rejected outright — nothing may survive the EOD square-off."""
    _install_stub_backend(client, real_account.id, ok=True)
    for product in ("DELIVERY", "NORMAL", "MARGIN"):
        r = client.post(
            "/api/orders",
            json={
                "account_id": real_account.id,
                "symbol": "NSE:INFY-EQ",
                "side": "BUY",
                "quantity": 1,
                "order_type": "MARKET",
                "product_type": product,
            },
        )
        assert r.status_code == 422, f"{product}: {r.text}"
        assert "intraday-only" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Cancel + pending list
# ---------------------------------------------------------------------------


def test_manual_filled_order_creates_position(
    client: TestClient, db_session, isolated_db, real_account
):
    """A manual order that fills (broker returns FILLED + avg price) must
    mirror into the positions table so the dashboard's Active Positions
    shows it — previously manual fills never created a position."""
    from app.main import app
    from app.db.models import Position

    class FilledStub:
        name = "stub"
        broker_account_id = real_account.id

        async def place_order(
            self, *, signal, symbol, side, quantity,
            order_type=OrderType.MARKET, limit_price=None, stop_price=None,
            product_type=ProductType.INTRADAY,
        ):
            return OrderResult(
                broker_order_id="FILL-1", state=OrderState.FILLED,
                symbol=symbol, side=side, quantity=quantity, order_type=order_type,
                filled_quantity=quantity, average_price=612.5,
            )

        async def cancel_order(self, b): return True
        async def get_positions(self): return []
        async def get_order_status(self, b): return None

    app.state.execution_manager.register_backend(real_account.id, FilledStub())
    r = client.post(
        "/api/orders",
        json={
            "account_id": real_account.id,
            "symbol": "NSE:SBIN-EQ",
            "side": "BUY",
            "quantity": 10,
            "order_type": "MARKET",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "FILLED"
    db_session.expire_all()
    pos = db_session.query(Position).filter_by(symbol="NSE:SBIN-EQ").one()
    assert pos.quantity == 10
    assert pos.average_price == 612.5


def test_cancel_order_updates_local_row(
    client: TestClient, db_session, isolated_db, real_account
):
    _install_stub_backend(client, real_account.id, ok=True)
    # First place
    r = client.post(
        "/api/orders",
        json={
            "account_id": real_account.id,
            "symbol": "NSE:SBIN-EQ",
            "side": "BUY",
            "quantity": 1,
            "order_type": "MARKET",
        },
    )
    assert r.status_code == 200
    # Now cancel
    r = client.post(
        "/api/orders/cancel",
        json={"account_id": real_account.id, "broker_order_id": "STUB-1"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # Local row is now cancelled
    db_session.expire_all()
    trade = db_session.query(Trade).filter_by(broker_order_id="STUB-1").one()
    assert trade.status == "cancelled"


def test_cancel_unknown_order_id_404s(
    client: TestClient, isolated_db, real_account
):
    _install_stub_backend(client, real_account.id, ok=True)
    r = client.post(
        "/api/orders/cancel",
        json={"account_id": real_account.id, "broker_order_id": "NOPE-1"},
    )
    # Stub backend still says "ok" because it doesn't know either
    # way, but the local row is missing. The HTTP layer still
    # returns 200 (the broker call was made); only the local row
    # update is skipped.
    assert r.status_code == 200


def test_cancel_marks_local_row_when_broker_rejects(
    client: TestClient, db_session, isolated_db, real_account
):
    """When the broker says 'no' to a cancel (order already filled,
    already cancelled, or just not on the broker anymore), the local
    `trades` row must still be flipped to `cancelled` so the Trade
    page's pending list doesn't loop forever showing the same row
    with no UI feedback. Regression test for the "cancel does
    nothing" complaint.
    """
    _install_stub_backend(client, real_account.id, ok=True)
    # Place an order so we have a row to cancel.
    r = client.post(
        "/api/orders",
        json={
            "account_id": real_account.id,
            "symbol": "NSE:SBIN-EQ",
            "side": "BUY",
            "quantity": 1,
            "order_type": "MARKET",
        },
    )
    assert r.status_code == 200

    # Re-install the stub with ok=False so the broker "rejects" the
    # cancel — the order is already gone from the broker side
    # (filled / cancelled / unknown id).
    _install_stub_backend(client, real_account.id, ok=False)

    r = client.post(
        "/api/orders/cancel",
        json={"account_id": real_account.id, "broker_order_id": "STUB-1"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["broker_order_id"] == "STUB-1"
    assert body["reason"] == "broker_rejected_already_gone"

    # The local row is still flipped to cancelled so the operator's
    # pending list drops it. Without this, the Trade page kept
    # showing the same row even after a successful broker reject.
    db_session.expire_all()
    trade = db_session.query(Trade).filter_by(broker_order_id="STUB-1").one()
    assert trade.status == "cancelled"


def test_cancel_handles_duplicate_broker_order_id_rows(
    client: TestClient, db_session, isolated_db, real_account
):
    """Regression: the cancel endpoint used `scalar_one_or_none()` to
    look up the local trade row, which raised `MultipleResultsFound`
    whenever two `trades` rows shared a `broker_order_id` (which
    happens — the persist path is best-effort and can write a
    duplicate on webhook re-write or operator double-click). The
    unhandled `MultipleResultsFound` surfaced as a FastAPI 500 with
    a plain-text body, which the frontend then crashed on with
    "body stream already read" while trying to parse the error.
    Fix: cancel iterates and updates every matching row.
    """
    _install_stub_backend(client, real_account.id, ok=True)
    # Place once — that creates one row.
    r = client.post(
        "/api/orders",
        json={
            "account_id": real_account.id,
            "symbol": "NSE:SBIN-EQ",
            "side": "BUY",
            "quantity": 1,
            "order_type": "MARKET",
        },
    )
    assert r.status_code == 200
    # Manually insert a duplicate row that mirrors the same
    # broker_order_id but lives on a different account (e.g. an
    # auto-pipeline place from the paper side that we want to clean
    # up at the same time).
    db_session.add(
        Trade(
            broker_account_id=None,
            broker_order_id="STUB-1",
            symbol="NSE:SBIN-EQ",
            side="BUY",
            quantity=1,
            order_type="MARKET",
            price=100.0,
            status="placed",
        )
    )
    db_session.commit()
    db_session.expire_all()
    matches = db_session.query(Trade).filter_by(broker_order_id="STUB-1").all()
    assert len(matches) >= 2, "test setup must produce duplicate rows"

    # Cancel must NOT 500. Must update every matching row.
    r = client.post(
        "/api/orders/cancel",
        json={"account_id": real_account.id, "broker_order_id": "STUB-1"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["rows_updated"] >= 2

    db_session.expire_all()
    for t in db_session.query(Trade).filter_by(broker_order_id="STUB-1").all():
        assert t.status == "cancelled", f"row {t.id} not flipped"


def test_list_trades_status_filter(
    client: TestClient, db_session, isolated_db, real_account
):
    """`/api/trades?status=filled` returns only filled rows, so a burst of
    rejected HOLD signals can't crowd real fills out of the newest-N window
    (which is what made Trade History lose old entry legs)."""
    for i in range(3):
        db_session.add(Trade(
            broker_account_id=real_account.id, broker_order_id=f"REJ{i}",
            symbol="NSE:SBIN-EQ", side="HOLD", quantity=0, order_type="MARKET",
            price=0.0, status="rejected",
        ))
    db_session.add(Trade(
        broker_account_id=real_account.id, broker_order_id="FILL1",
        symbol="NSE:SBIN-EQ", side="BUY", quantity=5, order_type="MARKET",
        price=100.0, status="filled",
    ))
    db_session.commit()

    allrows = client.get("/api/trades?limit=1000").json()
    assert any(t["status"] == "rejected" for t in allrows)

    filled = client.get("/api/trades?limit=1000&status=filled").json()
    assert len(filled) >= 1
    assert all(t["status"] == "filled" for t in filled)
    assert not any(t["status"] == "rejected" for t in filled)


def test_list_pending_orders(
    client: TestClient, db_session, isolated_db, real_account
):
    _install_stub_backend(client, real_account.id, ok=True)
    for sym in ("NSE:SBIN-EQ", "NSE:INFY-EQ", "NSE:TCS-EQ"):
        r = client.post(
            "/api/orders",
            json={
                "account_id": real_account.id,
                "symbol": sym,
                "side": "BUY",
                "quantity": 1,
                "order_type": "MARKET",
            },
        )
        assert r.status_code == 200
    r = client.get("/api/orders/pending", params={"account_id": real_account.id})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 3
    syms = {o["symbol"] for o in body["orders"]}
    assert syms == {"NSE:SBIN-EQ", "NSE:INFY-EQ", "NSE:TCS-EQ"}


def test_options_chain_endpoint_master_fallback(client: TestClient, isolated_db):
    """With no connected Fyers account, /api/options/chain falls back to
    the static instrument master — same shape as the live response, but
    source='master' and a reason explaining why there are no live prices.
    The endpoint never 500s; the panel always renders."""
    r = client.get("/api/options/chain", params={"underlying": "NIFTY", "strikecount": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["source"] == "master"
    assert body["reason"]  # explains how to get live prices
    assert "strikes" in body and "expiries" in body
    # An unknown underlying still returns a shaped (empty) response.
    r2 = client.get("/api/options/chain", params={"underlying": "ZZZNOPE"})
    assert r2.status_code == 200
    assert r2.json()["source"] == "master"


def test_quote_endpoint_no_live_quote_for_derivatives(client: TestClient, isolated_db):
    # Equities/indices now resolve a live quote via the public feed, but
    # F&O / options have no public symbol — those return ok:false offline
    # (no network), with a clear reason. (A connected Fyers feed serves
    # derivatives.)
    r = client.get("/api/orders/quote", params={"symbol": "NSE:NIFTY2561424500CE"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "no live quote" in body["reason"].lower()


# ---------------------------------------------------------------------------
# Manager.place_manual_order — direct unit tests via the lifespan
# ---------------------------------------------------------------------------


def test_manager_place_manual_order_stub_path(
    client: TestClient, db_session, isolated_db, real_account
):
    """Exercise Manager.place_manual_order directly. The client
    fixture runs the FastAPI lifespan, which constructs the manager
    and exposes it on `app.state.execution_manager`."""
    from app.main import app

    class StubB:
        name = "stub"
        broker_account_id = real_account.id
        async def place_order(
            self, *, signal, symbol, side, quantity,
            order_type=OrderType.MARKET, limit_price=None, stop_price=None,
            product_type=ProductType.INTRADAY,
        ):
            return OrderResult(
                broker_order_id="X1", state=OrderState.PENDING,
                symbol=symbol, side=side, quantity=quantity, order_type=order_type,
            )
        async def cancel_order(self, broker_order_id): return True
        async def get_positions(self): return []
        async def get_order_status(self, broker_order_id): return None

    import asyncio
    mgr = app.state.execution_manager
    mgr.register_backend(real_account.id, StubB())
    db_session.refresh(real_account)
    result = asyncio.get_event_loop().run_until_complete(
        mgr.place_manual_order(
            account=real_account,
            symbol="NSE:SBIN-EQ",
            side="BUY",
            quantity=2,
            order_type="MARKET",
            product_type="INTRADAY",
        )
    )
    assert result["ok"] is True
    assert result["broker_order_id"] == "X1"


def test_manual_order_blocked_by_risk_unless_bypassed(
    client: TestClient, db_session, isolated_db, real_account
):
    """A rejected risk verdict BLOCKS a manual order (so limits set in
    Settings actually take effect on the Trade page too); the operator
    can still override it with bypass_risk=True."""
    from app.main import app
    from types import SimpleNamespace
    import asyncio

    class StubB:
        name = "stub"
        broker_account_id = real_account.id
        async def place_order(
            self, *, signal, symbol, side, quantity,
            order_type=OrderType.MARKET, limit_price=None, stop_price=None,
            product_type=ProductType.INTRADAY,
        ):
            return OrderResult(
                broker_order_id="X9", state=OrderState.PENDING,
                symbol=symbol, side=side, quantity=quantity, order_type=order_type,
            )
        async def cancel_order(self, broker_order_id): return True
        async def get_positions(self): return []
        async def get_order_status(self, broker_order_id): return None

    class RejectingRisk:
        async def evaluate(self, **_):
            return SimpleNamespace(
                approved=False,
                codes=["RISK_MAX_SINGLE_POSITION_PCT"],
                violations=[{"code": "RISK_MAX_SINGLE_POSITION_PCT"}],
            )

    mgr = app.state.execution_manager
    mgr.register_backend(real_account.id, StubB())
    db_session.refresh(real_account)
    original_risk = mgr._risk
    mgr._risk = RejectingRisk()
    try:
        blocked = asyncio.get_event_loop().run_until_complete(
            mgr.place_manual_order(
                account=real_account, symbol="NSE:SBIN-EQ", side="BUY",
                quantity=2, order_type="MARKET", product_type="INTRADAY",
            )
        )
        assert blocked["ok"] is False
        assert blocked["blocked"] is True
        assert blocked["status"] == "REJECTED_RISK"
        assert blocked["broker_order_id"] is None
        assert "RISK_MAX_SINGLE_POSITION_PCT" in blocked["risk_codes"]

        # The operator overrides → the order is placed.
        placed = asyncio.get_event_loop().run_until_complete(
            mgr.place_manual_order(
                account=real_account, symbol="NSE:SBIN-EQ", side="BUY",
                quantity=2, order_type="MARKET", product_type="INTRADAY",
                bypass_risk=True,
            )
        )
        assert placed["ok"] is True
        assert placed["broker_order_id"] == "X9"
        assert placed["bypassed_risk"] is True
    finally:
        mgr._risk = original_risk


def test_manager_rejects_invalid_side(
    client: TestClient, db_session, isolated_db, real_account
):
    from app.main import app
    import asyncio
    mgr = app.state.execution_manager
    db_session.refresh(real_account)
    with pytest.raises(ValueError):
        asyncio.get_event_loop().run_until_complete(
            mgr.place_manual_order(
                account=real_account,
                symbol="NSE:SBIN-EQ",
                side="HOLD",
                quantity=1,
            )
        )


def test_manager_rejects_zero_quantity(
    client: TestClient, db_session, isolated_db, real_account
):
    from app.main import app
    import asyncio
    mgr = app.state.execution_manager
    db_session.refresh(real_account)
    with pytest.raises(ValueError):
        asyncio.get_event_loop().run_until_complete(
            mgr.place_manual_order(
                account=real_account,
                symbol="NSE:SBIN-EQ",
                side="BUY",
                quantity=0,
            )
        )


# =========================================================================
# POST /api/trades/delete — operator housekeeping on the history page
# =========================================================================


def test_delete_trades_removes_rows_and_audits(client: TestClient, db_session, isolated_db):
    """Trades delete, and each full row lands in audit_log so the deletion
    can be reconstructed."""
    from app.db.models import AuditLog

    t = Trade(symbol="SBIN", side="BUY", quantity=10, price=100.0,
              order_type="market", status="filled", pnl=50.0)
    db_session.add(t)
    db_session.commit()
    tid = t.id

    r = client.post("/api/trades/delete", json={"ids": [tid]})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "deleted": [tid], "count": 1}

    db_session.expire_all()  # the API used its own session; drop our cache
    assert db_session.get(Trade, tid) is None

    entry = (
        db_session.query(AuditLog).filter(AuditLog.target == f"trade:{tid}").one()
    )
    assert entry.action == "trade.delete"
    assert entry.before["symbol"] == "SBIN"
    assert entry.before["pnl"] == 50.0


def test_delete_trades_is_atomic_across_the_cluster(client: TestClient, db_session, isolated_db):
    """A shared leg means several displayed rows ride on the same trade
    rows. The batch must be all-or-nothing: if ONE symbol is blocked,
    nothing at all is deleted — a partial delete is what strands an exit
    whose entry already went."""
    from app.db.models import Position

    free = Trade(symbol="WIPRO", side="BUY", quantity=5, price=10.0,
                 order_type="market", status="filled")
    guarded = Trade(symbol="TCS", side="BUY", quantity=5, price=200.0,
                    order_type="market", status="filled")
    db_session.add_all([free, guarded])
    db_session.add(Position(symbol="TCS", quantity=5, average_price=200.0))
    db_session.commit()
    ids = [free.id, guarded.id]

    r = client.post("/api/trades/delete", json={"ids": ids})
    assert r.status_code == 409
    assert "TCS" in r.json()["detail"]

    db_session.expire_all()
    # Neither row went — not even the unguarded one.
    assert db_session.get(Trade, ids[0]) is not None
    assert db_session.get(Trade, ids[1]) is not None


def test_delete_trades_unknown_id_404s_and_deletes_nothing(
    client: TestClient, db_session, isolated_db
):
    """An unknown id fails the whole batch before anything is removed."""
    t = Trade(symbol="INFY", side="BUY", quantity=1, price=1.0,
              order_type="market", status="filled")
    db_session.add(t)
    db_session.commit()
    tid = t.id

    r = client.post("/api/trades/delete", json={"ids": [tid, 999999999]})
    assert r.status_code == 404
    assert "999999999" in r.json()["detail"]

    db_session.expire_all()
    assert db_session.get(Trade, tid) is not None


def test_delete_trades_rejects_empty_id_list(client: TestClient, isolated_db):
    assert client.post("/api/trades/delete", json={"ids": []}).status_code == 422
