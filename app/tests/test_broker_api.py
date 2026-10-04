"""Tests for /api/broker/* — profile, funds, holdings, GTT book and GTT
cancel — plus the FyersClient / FyersLiveBackend calls behind them.

Fyers is never contacted. Reads run either against a stub backend or
against a real FyersLiveBackend whose `FyersClient._request` is swapped
for a recorder (so the HTTP method, path and JSON body are asserted);
`_no_network` turns any accidental real request into a test failure.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import broker
from app.db.models import AuditLog, BrokerAccount
from app.execution.fyers_live import (
    FyersAPIError,
    FyersBlockedError,
    FyersClient,
    FyersLiveBackend,
)


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Any real outbound request fails the test (MockTransport clients pass)."""
    attempts: list[str] = []
    real_send = httpx.AsyncClient.send

    async def _guarded(self, request, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if isinstance(getattr(self, "_transport", None), httpx.MockTransport):
            return await real_send(self, request, *args, **kwargs)
        attempts.append(str(request.url))
        raise RuntimeError(f"network disabled in tests: {request.url}")

    monkeypatch.setattr(httpx.AsyncClient, "send", _guarded)
    yield
    assert not attempts, f"test tried to reach the network: {attempts}"


class _Recorder:
    """Stands in for `FyersClient._request`: records every call and answers
    from a `{(method, path): payload | Exception}` table."""

    def __init__(self, responses: dict[tuple[str, str], Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, method, path, *, params=None, json_body=None, base_url=None):  # noqa: ANN001
        self.calls.append({"method": method, "path": path, "params": params, "json": json_body})
        resp = self.responses.get((method, path), {"s": "ok"})
        if isinstance(resp, Exception):
            raise resp
        return resp


def _live_backend(monkeypatch, responses: dict[tuple[str, str], Any], account_id: int = 1):
    client = FyersClient(app_id="APP-100", access_token="TOK")
    rec = _Recorder(responses)
    monkeypatch.setattr(client, "_request", rec)
    backend = FyersLiveBackend(
        app_id="APP-100", access_token="TOK", broker_account_id=account_id, client=client,
    )
    return backend, rec


def _connect(monkeypatch, backend) -> None:
    monkeypatch.setattr(broker, "_fyers_backend", lambda: backend)


@pytest.fixture()
def real_account(db_session, isolated_db) -> BrokerAccount:
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
    return acc


def _register(account_id: int, backend) -> None:
    from app.main import app

    app.state.execution_manager.register_backend(account_id, backend)


# ---------------------------------------------------------------------------
# Not connected / broker failure — always 200
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path, empty",
    [
        ("/api/broker/profile", {"client_id": None, "name": None, "email": None}),
        ("/api/broker/funds", {"rows": []}),
        ("/api/broker/holdings", {"holdings": []}),
        ("/api/broker/gtt", {"orders": []}),
    ],
)
def test_reads_degrade_without_fyers(client: TestClient, monkeypatch, path: str, empty: dict) -> None:
    _connect(monkeypatch, None)
    r = client.get(path)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["reason"].startswith("connect a Fyers account")
    for k, v in empty.items():
        assert body[k] == v


def test_funds_and_holdings_shells_keep_their_keys(client: TestClient, monkeypatch) -> None:
    _connect(monkeypatch, None)
    funds = client.get("/api/broker/funds").json()
    assert set(funds["summary"]) == {
        "total_balance", "utilized", "clear_balance", "realized_pnl", "collateral",
        "fund_transfer", "receivables", "adhoc_limit", "limit_start", "available",
    }
    assert all(v is None for v in funds["summary"].values())
    holdings = client.get("/api/broker/holdings").json()
    assert set(holdings["overall"]) == {"count", "investment", "current_value", "pnl", "pnl_pct", "day_pnl"}


@pytest.mark.parametrize(
    "path, method",
    [
        ("/api/broker/profile", "/profile"),
        ("/api/broker/funds", "/funds"),
        ("/api/broker/holdings", "/holdings"),
        ("/api/broker/gtt", "/gtt/orders"),
    ],
)
def test_reads_report_broker_failures(client: TestClient, monkeypatch, path: str, method: str) -> None:
    be, _ = _live_backend(monkeypatch, {("GET", method): FyersAPIError("fyers 500", status_code=500)})
    _connect(monkeypatch, be)
    r = client.get(path)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "unavailable" in body["reason"] and "fyers 500" in body["reason"]


def test_reads_treat_an_s_error_payload_as_a_failure(client: TestClient, monkeypatch) -> None:
    """Fyers can answer 200 with `{"s": "error"}` — never shown as data."""
    be, _ = _live_backend(
        monkeypatch, {("GET", "/holdings"): {"s": "error", "code": -15, "message": "token expired"}}
    )
    _connect(monkeypatch, be)
    body = client.get("/api/broker/holdings").json()
    assert body["ok"] is False and "token expired" in body["reason"]


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


def test_profile(client: TestClient, monkeypatch) -> None:
    be, rec = _live_backend(monkeypatch, {("GET", "/profile"): {
        "s": "ok", "code": 200,
        "data": {
            "fy_id": "XA00001", "name": "A TRADER", "display_name": "AT",
            "email_id": "a@example.com", "PAN": "ABCDE1234F", "mobile_number": "9999999999",
        },
    }})
    _connect(monkeypatch, be)
    body = client.get("/api/broker/profile").json()
    # exactly these keys — PAN / mobile never leave the server
    assert body == {"ok": True, "client_id": "XA00001", "name": "A TRADER", "email": "a@example.com"}
    assert [(c["method"], c["path"]) for c in rec.calls] == [("GET", "/profile")]


def test_profile_falls_back_to_display_name(client: TestClient, monkeypatch) -> None:
    class _Stub:
        async def get_profile(self) -> dict:
            return {"fy_id": "XA00002", "name": None, "display_name": "Trader Two"}

    _connect(monkeypatch, _Stub())
    body = client.get("/api/broker/profile").json()
    assert body == {"ok": True, "client_id": "XA00002", "name": "Trader Two", "email": None}


# ---------------------------------------------------------------------------
# Funds
# ---------------------------------------------------------------------------


_FUND_LIMIT = [
    {"id": 1, "title": "Total Balance", "equityAmount": 125000.5, "commodityAmount": 0},
    {"id": 2, "title": "Utilized Amount", "equityAmount": 20000, "commodityAmount": 0},
    {"id": 3, "title": "Clear Balance", "equityAmount": 105000.5, "commodityAmount": 0},
    {"id": 4, "title": "Realized Profit and Loss", "equityAmount": -350.25, "commodityAmount": 0},
    {"id": 5, "title": "Collaterals", "equityAmount": 0, "commodityAmount": 0},
    {"id": 6, "title": "Fund Transfer", "equityAmount": 5000, "commodityAmount": 0},
    {"id": 7, "title": "Receivables", "equityAmount": 0, "commodityAmount": 0},
    {"id": 8, "title": "Adhoc Limit", "equityAmount": 0, "commodityAmount": 0},
    {"id": 9, "title": "Limit at start of the day", "equityAmount": 120000.5, "commodityAmount": 1500},
    {"id": 10, "title": "Available Balance", "equityAmount": 104650.25, "commodityAmount": 1500},
]


def test_funds_rows_and_summary(client: TestClient, monkeypatch) -> None:
    be, rec = _live_backend(monkeypatch, {("GET", "/funds"): {"s": "ok", "code": 200, "fund_limit": _FUND_LIMIT}})
    _connect(monkeypatch, be)
    body = client.get("/api/broker/funds").json()
    assert body["ok"] is True
    assert [(c["method"], c["path"]) for c in rec.calls] == [("GET", "/funds")]
    assert body["rows"][0] == {"id": 1, "title": "Total Balance", "equity": 125000.5, "commodity": 0.0}
    assert body["rows"][9] == {"id": 10, "title": "Available Balance", "equity": 104650.25, "commodity": 1500.0}
    assert len(body["rows"]) == 10
    assert body["summary"] == {
        "total_balance": 125000.5,
        "utilized": 20000.0,
        "clear_balance": 105000.5,
        "realized_pnl": -350.25,
        "collateral": 0.0,
        "fund_transfer": 5000.0,
        "receivables": 0.0,
        "adhoc_limit": 0.0,
        "limit_start": 120000.5,
        "available": 104650.25,
    }


def test_funds_summary_falls_back_to_titles(client: TestClient, monkeypatch) -> None:
    """Rows without ids still land in the summary by title; absent rows
    stay None (never a made-up 0)."""

    class _Stub:
        async def get_fund_rows(self) -> list:
            return [
                {"title": "Available Balance", "equityAmount": "999.5", "commodityAmount": None},
                {"title": "Utilized Amount", "equityAmount": 10},
                "junk",
            ]

    _connect(monkeypatch, _Stub())
    body = client.get("/api/broker/funds").json()
    assert body["ok"] is True
    assert body["summary"]["available"] == 999.5
    assert body["summary"]["utilized"] == 10.0
    assert body["summary"]["total_balance"] is None
    assert body["rows"][0] == {"id": 0, "title": "Available Balance", "equity": 999.5, "commodity": None}
    assert len(body["rows"]) == 2


@pytest.mark.asyncio
async def test_get_funds_contract_is_unchanged(monkeypatch) -> None:
    """The risk layer's `get_funds` stays a fail-safe float; the new rows
    method is separate."""
    be, _ = _live_backend(monkeypatch, {("GET", "/funds"): {"s": "ok", "fund_limit": _FUND_LIMIT}})
    assert await be.get_funds() == 125000.5
    assert len(await be.get_fund_rows()) == 10


# ---------------------------------------------------------------------------
# Holdings
# ---------------------------------------------------------------------------


_HOLDINGS = {
    "s": "ok", "code": 200,
    "overall": {
        "count_total": 2, "total_investment": 3029.5, "total_current_value": 3088.5,
        "total_pl": 59.0, "pnl_perc": 1.95,
    },
    "holdings": [
        {
            "holdingType": "HLD", "quantity": 10, "costPrice": 2.95, "marketVal": 26.0,
            "remainingQuantity": 10, "pl": -3.5, "ltp": 2.6, "id": 0, "fyToken": "101000000014366",
            "exchange": 10, "symbol": "NSE:IDEA-EQ", "segment": 10, "isin": "INE669E01016",
            "qty_t1": 0, "remainingPledgeQuantity": 2, "collateralQuantity": 1,
        },
        {   # no marketVal / pl: computed from ltp and cost
            "holdingType": "T1", "quantity": 5, "costPrice": 600, "ltp": 612.5,
            "symbol": "NSE:SBIN-EQ", "isin": "INE062A01020", "qty_t1": 5,
        },
    ],
}

_QUOTES = {"s": "ok", "d": [
    {"n": "NSE:IDEA-EQ", "s": "ok", "v": {"lp": 2.6, "ch": -0.1, "chp": -3.7, "prev_close_price": 2.7}},
    {"n": "NSE:SBIN-EQ", "s": "ok", "v": {"lp": 612.5, "ch": 2.5, "chp": 0.41}},  # no prev close
]}


def test_holdings_with_day_pnl_from_quotes(client: TestClient, monkeypatch) -> None:
    be, rec = _live_backend(monkeypatch, {("GET", "/holdings"): _HOLDINGS, ("GET", "/quotes"): _QUOTES})
    _connect(monkeypatch, be)
    body = client.get("/api/broker/holdings").json()
    assert body["ok"] is True
    assert [(c["method"], c["path"]) for c in rec.calls] == [("GET", "/holdings"), ("GET", "/quotes")]
    assert rec.calls[1]["params"] == {"symbols": "NSE:IDEA-EQ,NSE:SBIN-EQ"}

    idea, sbin = body["holdings"]
    assert idea == {
        "symbol": "NSE:IDEA-EQ", "isin": "INE669E01016", "qty": 10, "t1_qty": 0,
        "remaining_qty": 10, "pledged_qty": 2, "collateral_qty": 1,
        "avg_price": 2.95, "ltp": 2.6, "market_value": 26.0, "cost_value": 29.5,
        "pnl": -3.5, "pnl_pct": -11.86, "holding_type": "HLD",
        "prev_close": 2.7, "day_pnl": -1.0,
    }
    # computed fallbacks; prev close derived from lp - ch
    assert sbin["market_value"] == 3062.5 and sbin["cost_value"] == 3000.0
    assert sbin["pnl"] == 62.5 and sbin["pnl_pct"] == 2.08
    assert sbin["t1_qty"] == 5 and sbin["holding_type"] == "T1"
    assert sbin["prev_close"] == 610.0 and sbin["day_pnl"] == 12.5

    assert body["overall"] == {
        "count": 2, "investment": 3029.5, "current_value": 3088.5,
        "pnl": 59.0, "pnl_pct": 1.95, "day_pnl": 11.5,
    }


def test_holdings_survive_a_quote_failure(client: TestClient, monkeypatch) -> None:
    class _Stub:
        async def get_holdings(self) -> dict:
            return {"holdings": _HOLDINGS["holdings"], "overall": {}}

        async def get_quote(self, symbols: list[str]) -> list:
            raise RuntimeError("quotes down")

    _connect(monkeypatch, _Stub())
    body = client.get("/api/broker/holdings").json()
    assert body["ok"] is True
    assert [h["day_pnl"] for h in body["holdings"]] == [None, None]
    assert [h["prev_close"] for h in body["holdings"]] == [None, None]
    # no `overall` from the broker: summed from the rows
    assert body["overall"] == {
        "count": 2, "investment": 3029.5, "current_value": 3088.5,
        "pnl": 59.0, "pnl_pct": 1.95, "day_pnl": None,
    }


def test_holdings_quotes_are_batched_by_50(client: TestClient, monkeypatch) -> None:
    rows = [{"symbol": f"NSE:S{i}-EQ", "quantity": 1, "costPrice": 10, "ltp": 11} for i in range(120)]
    batches: list[int] = []

    class _Stub:
        async def get_holdings(self) -> dict:
            return {"holdings": rows}

        async def get_quote(self, symbols: list[str]) -> list:
            batches.append(len(symbols))
            return []

    _connect(monkeypatch, _Stub())
    body = client.get("/api/broker/holdings").json()
    assert body["ok"] is True and len(body["holdings"]) == 120
    assert sorted(batches) == [20, 50, 50]


def test_holdings_tolerate_a_data_wrapper(client: TestClient, monkeypatch) -> None:
    be, _ = _live_backend(monkeypatch, {("GET", "/holdings"): {"s": "ok", "data": {
        "holdings": _HOLDINGS["holdings"][:1], "overall": {"count_total": 1},
    }}})
    _connect(monkeypatch, be)
    body = client.get("/api/broker/holdings").json()
    assert body["ok"] is True
    assert [h["symbol"] for h in body["holdings"]] == ["NSE:IDEA-EQ"]
    assert body["overall"]["count"] == 1


# ---------------------------------------------------------------------------
# GTT order book
# ---------------------------------------------------------------------------


def test_gtt_book_parses_order_info_legs(client: TestClient, monkeypatch) -> None:
    be, rec = _live_backend(monkeypatch, {("GET", "/gtt/orders"): {"s": "ok", "orderBook": [
        {
            "id": "25010700000001", "symbol": "NSE:SBIN-EQ", "side": 1, "productType": "CNC",
            "gttStatus": "active", "orderDateTime": "07-Jan-2025 10:00:00",
            "orderInfo": {"leg1": {"price": 800, "triggerPrice": 801, "qty": 3}},
        },
        {
            "id": 25010700000002, "symbol": "NSE:TCS-EQ", "side": -1, "productType": "MARGIN",
            "status": 2,
            "orderInfo": {
                "leg1": {"price": 4200, "triggerPrice": 4199.5, "qty": 1},
                "leg2": {"price": 3700, "triggerPrice": 3701, "qty": 1},
            },
        },
    ]}})
    _connect(monkeypatch, be)
    body = client.get("/api/broker/gtt").json()
    assert body["ok"] is True
    assert [(c["method"], c["path"]) for c in rec.calls] == [("GET", "/gtt/orders")]
    single, oco = body["orders"]
    assert single == {
        "id": "25010700000001", "symbol": "NSE:SBIN-EQ", "side": "BUY", "gtt_type": "Single",
        "product": "CNC", "status": "ACTIVE", "qty": 3, "trigger": 801.0, "limit": 800.0,
        "trigger2": None, "limit2": None, "created": "07-Jan-2025 10:00:00",
    }
    assert oco["id"] == "25010700000002" and oco["side"] == "SELL" and oco["gtt_type"] == "OCO"
    assert (oco["trigger"], oco["limit"], oco["trigger2"], oco["limit2"]) == (4199.5, 4200.0, 3701.0, 3700.0)
    assert oco["status"] == "2"  # numeric GTT codes pass through unmapped
    assert oco["created"] is None


def test_gtt_book_reads_flat_oms_rows(client: TestClient, monkeypatch) -> None:
    """The raw order book may use flat OMS keys instead of orderInfo legs;
    a zeroed second leg is still a Single."""
    be, _ = _live_backend(monkeypatch, {("GET", "/gtt/orders"): {"s": "ok", "data": {"orderBook": [
        {
            "id": "G3", "symbol": "NSE:IDEA-EQ", "tran_side": -1, "product_type": "MTF",
            "ord_status": "1", "price_limit": 12, "price_trigger": 12.5, "qty": 100,
            "price2_limit": 0, "price2_trigger": 0, "qty2": 0, "create_time": "27-Jan-2025 11:32:33",
        },
        {
            "id": "G4", "symbol": "NSE:INFY-EQ", "tran_side": 1, "product_type": "CNC",
            "price_limit": 1500, "price_trigger": 1501, "qty": 2,
            "price2_limit": 1300, "price2_trigger": 1301, "qty2": 2,
        },
    ]}}})
    _connect(monkeypatch, be)
    body = client.get("/api/broker/gtt").json()
    flat, flat_oco = body["orders"]
    assert flat == {
        "id": "G3", "symbol": "NSE:IDEA-EQ", "side": "SELL", "gtt_type": "Single", "product": "MTF",
        "status": "1", "qty": 100, "trigger": 12.5, "limit": 12.0, "trigger2": None,
        "limit2": None, "created": "27-Jan-2025 11:32:33",
    }
    assert flat_oco["gtt_type"] == "OCO" and flat_oco["side"] == "BUY"
    assert (flat_oco["trigger2"], flat_oco["limit2"]) == (1301.0, 1300.0)


def test_gtt_book_accepts_gtt_orders_key(client: TestClient, monkeypatch) -> None:
    be, _ = _live_backend(monkeypatch, {("GET", "/gtt/orders"): {"s": "ok", "gttOrders": [
        {"id": "G9", "symbol": "NSE:SBIN-EQ", "side": "SELL", "orderInfo": {"leg1": {"price": 1, "qty": 1}}},
    ]}})
    _connect(monkeypatch, be)
    orders = client.get("/api/broker/gtt").json()["orders"]
    assert [(o["id"], o["side"], o["status"]) for o in orders] == [("G9", "SELL", None)]


# ---------------------------------------------------------------------------
# GTT cancel
# ---------------------------------------------------------------------------


def _gtt_audits(db_session) -> list[AuditLog]:
    db_session.expire_all()
    return db_session.query(AuditLog).filter_by(action="gtt.manual_cancelled").all()


def test_gtt_cancel_deletes_via_gtt_orders_sync(
    client: TestClient, db_session, isolated_db, real_account, monkeypatch
) -> None:
    be, rec = _live_backend(
        monkeypatch, {("DELETE", "/gtt/orders/sync"): {"s": "ok", "code": 200, "message": "cancelled"}},
        account_id=real_account.id,
    )
    _register(real_account.id, be)
    r = client.post("/api/broker/gtt/cancel", json={"account_id": real_account.id, "id": "25010700000001"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "id": "25010700000001"}
    assert rec.calls == [{
        "method": "DELETE", "path": "/gtt/orders/sync", "params": None,
        "json": {"id": "25010700000001"},
    }]
    audits = _gtt_audits(db_session)
    assert len(audits) == 1
    assert audits[0].target == f"account:{real_account.id}"
    assert audits[0].after == {"gtt_id": "25010700000001", "broker_ok": True}


def test_gtt_cancel_broker_refusal_is_ok_false_and_audited(
    client: TestClient, db_session, isolated_db, real_account, monkeypatch
) -> None:
    be, _ = _live_backend(
        monkeypatch,
        {("DELETE", "/gtt/orders/sync"): FyersAPIError("fyers 400: order not found", status_code=400)},
        account_id=real_account.id,
    )
    _register(real_account.id, be)
    r = client.post("/api/broker/gtt/cancel", json={"account_id": real_account.id, "id": "NOPE"})
    assert r.status_code == 200
    assert r.json() == {"ok": False, "id": "NOPE"}
    assert [a.after["broker_ok"] for a in _gtt_audits(db_session)] == [False]


def test_gtt_cancel_s_error_payload_is_ok_false(
    client: TestClient, isolated_db, real_account, monkeypatch
) -> None:
    be, _ = _live_backend(
        monkeypatch, {("DELETE", "/gtt/orders/sync"): {"s": "error", "message": "already triggered"}},
        account_id=real_account.id,
    )
    _register(real_account.id, be)
    r = client.post("/api/broker/gtt/cancel", json={"account_id": real_account.id, "id": "G1"})
    assert r.json() == {"ok": False, "id": "G1"}


def test_gtt_cancel_blocked_edge_is_503(
    client: TestClient, db_session, isolated_db, real_account, monkeypatch
) -> None:
    be, _ = _live_backend(
        monkeypatch,
        {("DELETE", "/gtt/orders/sync"): FyersBlockedError("blocked", status_code=403, reason="cloudflare_challenge")},
        account_id=real_account.id,
    )
    _register(real_account.id, be)
    r = client.post("/api/broker/gtt/cancel", json={"account_id": real_account.id, "id": "G1"})
    assert r.status_code == 503
    assert "blocking" in r.json()["detail"]
    assert _gtt_audits(db_session) == []


def test_gtt_cancel_other_broker_error_is_502(client: TestClient, isolated_db, real_account) -> None:
    class _Boom:
        async def cancel_gtt(self, gtt_id: str) -> bool:
            raise RuntimeError("socket closed")

    _register(real_account.id, _Boom())
    r = client.post("/api/broker/gtt/cancel", json={"account_id": real_account.id, "id": "G1"})
    assert r.status_code == 502
    assert "socket closed" in r.json()["detail"]


def test_gtt_cancel_needs_a_gtt_capable_backend(client: TestClient, isolated_db, real_account) -> None:
    class _NoGtt:
        async def cancel_order(self, broker_order_id: str) -> bool:
            return True

    _register(real_account.id, _NoGtt())
    r = client.post("/api/broker/gtt/cancel", json={"account_id": real_account.id, "id": "G1"})
    assert r.status_code == 400
    assert "GTT" in r.json()["detail"]


def test_gtt_cancel_validates_body_and_account(client: TestClient, db_session, isolated_db, real_account) -> None:
    assert client.post("/api/broker/gtt/cancel", json={"account_id": real_account.id}).status_code == 422
    assert client.post("/api/broker/gtt/cancel", json={"account_id": real_account.id, "id": "  "}).status_code == 422
    assert client.post("/api/broker/gtt/cancel", json={"account_id": 99999, "id": "G1"}).status_code == 404
    paper = BrokerAccount(name="Paper GTT", broker="fyers", paper_mode=True, enabled=True)
    db_session.add(paper)
    db_session.commit()
    r = client.post("/api/broker/gtt/cancel", json={"account_id": paper.id, "id": "G1"})
    assert r.status_code == 400 and "paper account" in r.json()["detail"]


# ---------------------------------------------------------------------------
# FyersClient on the wire (MockTransport — real _request, no network)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_sends_gtt_cancel_as_delete_with_json_body() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"s": "ok", "code": 200})

    client = FyersClient(app_id="APP123", access_token="TOK", transport=httpx.MockTransport(handler))
    try:
        assert await FyersLiveBackend(
            app_id="APP123", access_token="TOK", broker_account_id=1, client=client,
        ).cancel_gtt("G42") is True
        await client.get_profile()
        await client.get_holdings()
        await client.get_gtt_orders()
    finally:
        await client.aclose()
    assert [(r.method, r.url.path) for r in seen] == [
        ("DELETE", "/api/v3/gtt/orders/sync"),
        ("GET", "/api/v3/profile"),
        ("GET", "/api/v3/holdings"),
        ("GET", "/api/v3/gtt/orders"),
    ]
    assert seen[0].content.replace(b" ", b"") == b'{"id":"G42"}'
