"""Tests for the Fyers live-trading backend.

Coverage:

  - place_order() returns a PENDING OrderResult with the order id.
  - 5xx retried up to max_retries; final failure raises.
  - 401 raises FyersAuthError (caller rotates the token).
  - 429 sleeps then retries; eventual success returns the order id.
  - cancel_order() returns True on 2xx, False on 4xx.
  - get_positions() parses Fyers' `netPositions` array.
  - get_order_status() returns the right OrderState for the Fyers
    status string.
  - constructor requires app_id + access_token.
  - No token in any error message or log.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.execution.base import (
    OrderSide,
    OrderState,
    OrderType,
)
from app.execution.fyers_live import (
    FyersAPIError,
    FyersAuthError,
    FyersClient,
    FyersLiveBackend,
)


# -- Helpers --------------------------------------------------------------


def _ok_order_response(order_id: str = "FX12345") -> dict[str, Any]:
    return {"s": "ok", "code": 200, "id": order_id, "message": "Order placed successfully"}


def _ok_order_book(order_id: str, status: str = "2", filled_qty: int = 10, avg_price: float = 2500.0) -> dict[str, Any]:
    return {
        "s": "ok",
        "code": 200,
        "orderBook": [
            {
                "id": order_id,
                "symbol": "NSE:RELIANCE-EQ",
                "qty": 10,
                "tradedQty": filled_qty,
                "tradedPrice": avg_price,
                "status": status,
                "type": 1,
                "side": 1,
            }
        ],
    }


def _ok_positions() -> dict[str, Any]:
    return {
        "s": "ok",
        "code": 200,
        "netPositions": [
            {
                "symbol": "NSE:RELIANCE-EQ",
                "netQty": 10,
                "avgPrice": 2500.0,
                "ltp": 2600.0,
                "unrealizedProfit": 1000.0,
            },
            {
                "symbol": "NSE:TCS-EQ",
                "netQty": 0,
                "avgPrice": 0.0,
                "ltp": 3500.0,
            },
        ],
    }


def _make_client(handler, *, max_retries: int = 3) -> FyersClient:
    return FyersClient(
        app_id="APP123",
        access_token="TOK-SECRET",
        max_retries=max_retries,
        transport=httpx.MockTransport(handler),
    )


# -- Tests ----------------------------------------------------------------


def test_constructor_requires_app_id_and_token() -> None:
    with pytest.raises(ValueError):
        FyersClient(app_id="", access_token="x")
    with pytest.raises(ValueError):
        FyersClient(app_id="x", access_token="")


@pytest.mark.asyncio
async def test_place_order_happy_path() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "POST"
        assert req.url.path.endswith("/orders")
        # Verify Authorization header shape (app_id:token).
        assert req.headers.get("Authorization") == "APP123:TOK-SECRET"
        return httpx.Response(200, json=_ok_order_response("FX12345"))

    client = _make_client(handler)
    backend = FyersLiveBackend(
        app_id="APP123", access_token="TOK-SECRET",
        broker_account_id=1, account_name="main", client=client,
    )
    try:
        result = await backend.place_order(
            signal=None, symbol="NSE:RELIANCE-EQ",
            side=OrderSide.BUY, quantity=10,
        )
        assert result.broker_order_id == "FX12345"
        assert result.state == OrderState.PENDING
        assert result.symbol == "NSE:RELIANCE-EQ"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_5xx_retried_then_raises() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="server error")

    client = _make_client(handler, max_retries=2)
    with pytest.raises(FyersAPIError) as exc:
        await client.place_order({"symbol": "x", "qty": 1})
    # 1 initial + 2 retries = 3 attempts.
    assert calls["n"] == 3
    assert exc.value.status_code == 500
    assert exc.value.retryable is True
    await client.aclose()


@pytest.mark.asyncio
async def test_401_raises_fyers_auth_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="token invalid")

    client = _make_client(handler, max_retries=0)
    with pytest.raises(FyersAuthError) as exc:
        await client.place_order({"symbol": "x", "qty": 1})
    assert exc.value.status_code == 401
    await client.aclose()


@pytest.mark.asyncio
async def test_429_retries_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(429, text="rate limit")
        return httpx.Response(200, json=_ok_order_response("FX999"))

    # Use max_retries=1 and short backoff.
    client = FyersClient(
        app_id="APP123", access_token="TOK",
        max_retries=1,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.place_order({"symbol": "x", "qty": 1})
        assert result["id"] == "FX999"
        assert calls["n"] == 2
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_400_not_retried() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="bad request")

    client = _make_client(handler, max_retries=3)
    with pytest.raises(FyersAPIError) as exc:
        await client.place_order({"symbol": "x", "qty": 1})
    assert calls["n"] == 1  # no retry
    assert exc.value.status_code == 400
    assert exc.value.retryable is False
    await client.aclose()


@pytest.mark.asyncio
async def test_cancel_order_succeeds() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "DELETE"
        assert "id=FX12345" in str(req.url)
        return httpx.Response(200, json={"s": "ok", "code": 200})

    client = _make_client(handler)
    backend = FyersLiveBackend(
        app_id="APP123", access_token="TOK",
        broker_account_id=1, account_name="main", client=client,
    )
    try:
        ok = await backend.cancel_order("FX12345")
        assert ok is True
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_cancel_order_returns_false_on_4xx() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="unknown order")

    client = _make_client(handler, max_retries=0)
    backend = FyersLiveBackend(
        app_id="APP123", access_token="TOK",
        broker_account_id=1, account_name="main", client=client,
    )
    try:
        ok = await backend.cancel_order("FX-UNKNOWN")
        assert ok is False
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_get_positions_parses_net_positions() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/positions")
        return httpx.Response(200, json=_ok_positions())

    client = _make_client(handler)
    backend = FyersLiveBackend(
        app_id="APP123", access_token="TOK",
        broker_account_id=7, account_name="main", client=client,
    )
    try:
        positions = await backend.get_positions()
        # One non-zero position.
        nonzero = [p for p in positions if p.quantity != 0]
        assert len(nonzero) == 1
        assert nonzero[0].symbol == "NSE:RELIANCE-EQ"
        assert nonzero[0].quantity == 10
        assert nonzero[0].average_price == 2500.0
        assert nonzero[0].last_price == 2600.0
        assert nonzero[0].unrealized_pnl == 1000.0
        assert nonzero[0].broker_account_id == 7
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_get_order_status_filled() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_order_book("FX12345", status="2", filled_qty=10, avg_price=2500.0))

    client = _make_client(handler)
    backend = FyersLiveBackend(
        app_id="APP123", access_token="TOK",
        broker_account_id=1, account_name="main", client=client,
    )
    try:
        status = await backend.get_order_status("FX12345")
        assert status.state == OrderState.FILLED
        assert status.filled_quantity == 10
        assert status.average_price == 2500.0
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_get_order_status_cancelled() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        # Fyers status code 1 = Cancelled.
        return httpx.Response(200, json=_ok_order_book("FX12345", status="1", filled_qty=0, avg_price=0.0))

    client = _make_client(handler)
    backend = FyersLiveBackend(
        app_id="APP123", access_token="TOK",
        broker_account_id=1, account_name="main", client=client,
    )
    try:
        status = await backend.get_order_status("FX12345")
        assert status.state == OrderState.CANCELLED
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_5xx_returns_pending_order_result() -> None:
    """The backend wraps 5xx as PENDING (manager will time out
    the row). The transport-level error is still logged."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    client = _make_client(handler, max_retries=0)
    backend = FyersLiveBackend(
        app_id="APP123", access_token="TOK",
        broker_account_id=1, account_name="main", client=client,
    )
    try:
        result = await backend.place_order(
            signal=None, symbol="NSE:X",
            side=OrderSide.BUY, quantity=1,
        )
        assert result.state == OrderState.PENDING
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_no_token_in_error_message() -> None:
    """Defence-in-depth: the access_token must never appear in our
    formatted error message we propagate up to the caller. (The
    raw upstream response body is captured separately; we don't
    promise to redact that — the upstream is the source of truth.)"""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid: TOK-SECRET")

    client = _make_client(handler, max_retries=0)
    # Use the lower-level client (not the backend) so the auth
    # error propagates instead of being caught by cancel_order's
    # "return False on 4xx" path.
    with pytest.raises(FyersAuthError) as exc:
        await client.place_order({"symbol": "x", "qty": 1})
    # The formatted error message is a fixed string; we never
    # include the token in our own formatting.
    assert "TOK-SECRET" not in str(exc.value)
    await client.aclose()


@pytest.mark.asyncio
async def test_set_access_token_rotates() -> None:
    """The access_token can be rotated after the OAuth callback."""

    def handler(req: httpx.Request) -> httpx.Response:
        # The request must use the NEW token after rotation.
        assert req.headers.get("Authorization") == "APP123:TOK-NEW"
        return httpx.Response(200, json=_ok_order_response("FX-NEW"))

    client = _make_client(handler)
    client.set_access_token("TOK-NEW")
    backend = FyersLiveBackend(
        app_id="APP123", access_token="TOK-NEW",
        broker_account_id=1, account_name="main", client=client,
    )
    try:
        result = await backend.place_order(
            signal=None, symbol="NSE:X",
            side=OrderSide.BUY, quantity=1,
        )
        assert result.broker_order_id == "FX-NEW"
    finally:
        await client.aclose()
