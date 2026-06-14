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
    FyersBlockedError,
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
        # We POST to /orders/sync, not /orders, to match the official
        # fyers-apiv3 SDK and to bypass the Cloudflare anti-bot layer
        # that fronts the plain /orders endpoint in production.
        assert req.url.path.endswith("/orders/sync"), (
            f"place_order must hit /orders/sync, got {req.url.path}"
        )
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
async def test_place_order_type_codes_match_fyers_v3() -> None:
    """Regression: MARKET must go to Fyers as type 2 (not 1). The codes
    were swapped (MARKET→1, LIMIT→2), so every MARKET order was sent as a
    Limit(1) with limitPrice=0 and Fyers rejected it with
    "limitPrice: Must be greater than or equal to 0.0025". Verify the full
    v3 mapping (1=Limit, 2=Market, 3=SL-M, 4=SL-L) and the per-type price
    fields the broker expects.
    """
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured.clear()
        captured.update(json.loads(req.content))
        return httpx.Response(200, json=_ok_order_response("FX-T"))

    client = _make_client(handler)
    backend = FyersLiveBackend(
        app_id="APP123", access_token="TOK",
        broker_account_id=1, account_name="main", client=client,
    )
    try:
        # MARKET -> type 2, both prices 0
        await backend.place_order(
            signal=None, symbol="NSE:SBIN-EQ", side=OrderSide.BUY, quantity=1,
        )
        assert captured["type"] == 2
        assert captured["limitPrice"] == 0.0
        assert captured["stopPrice"] == 0.0

        # LIMIT -> type 1, limitPrice carried
        await backend.place_order(
            signal=None, symbol="NSE:SBIN-EQ", side=OrderSide.BUY, quantity=1,
            order_type=OrderType.LIMIT, limit_price=612.5,
        )
        assert captured["type"] == 1
        assert captured["limitPrice"] == 612.5

        # SL-M -> type 3, stopPrice (trigger), limitPrice 0
        await backend.place_order(
            signal=None, symbol="NSE:SBIN-EQ", side=OrderSide.SELL, quantity=1,
            order_type=OrderType.STOP_LOSS_MARKET, stop_price=600.0,
        )
        assert captured["type"] == 3
        assert captured["stopPrice"] == 600.0
        assert captured["limitPrice"] == 0.0

        # SL-L (STOP_LOSS) -> type 4, both trigger + limit
        await backend.place_order(
            signal=None, symbol="NSE:SBIN-EQ", side=OrderSide.SELL, quantity=1,
            order_type=OrderType.STOP_LOSS, stop_price=600.0, limit_price=599.0,
        )
        assert captured["type"] == 4
        assert captured["stopPrice"] == 600.0
        assert captured["limitPrice"] == 599.0
    finally:
        await client.aclose()


def test_normalize_option_chain_parses_fyers_v3_response() -> None:
    """The /data/options-chain-v3 response is a FLAT list: the first row
    (option_type blank, strike_price <= 0) is the underlying spot, the
    rest are CE/PE rows. Verify we extract spot, group by strike, carry
    ltp/oi, sort strikes, and surface the expiry list."""
    from app.execution.fyers_live import normalize_option_chain

    raw = {
        "s": "ok",
        "code": 200,
        "data": {
            "expiryData": [
                {"date": "16-06-2026", "expiry": "1781604000"},
                {"date": "23-06-2026", "expiry": "1782208800"},
            ],
            "optionsChain": [
                {"symbol": "NSE:NIFTY50-INDEX", "strike_price": -1, "option_type": "", "ltp": 23622.9},
                {"symbol": "NSE:NIFTY2661623600CE", "strike_price": 23600, "option_type": "CE", "ltp": 180.0, "oi": 100},
                {"symbol": "NSE:NIFTY2661623500CE", "strike_price": 23500, "option_type": "CE",
                 "ltp": 242.45, "bid": 242.0, "ask": 243.0, "oi": 4755530},
                {"symbol": "NSE:NIFTY2661623500PE", "strike_price": 23500, "option_type": "PE", "ltp": 67.8, "oi": 8497970},
            ],
        },
    }
    out = normalize_option_chain(raw, underlying_symbol="NSE:NIFTY50-INDEX")
    assert out["spot"] == 23622.9
    assert out["expiries"][0] == {"label": "16-06-2026", "ts": "1781604000"}
    assert len(out["expiries"]) == 2
    by = {s["strike"]: s for s in out["strikes"]}
    assert by[23500.0]["ce"]["ltp"] == 242.45
    assert by[23500.0]["ce"]["oi"] == 4755530
    assert by[23500.0]["pe"]["ltp"] == 67.8
    # NIFTY lot size guessed from the underlying symbol.
    assert by[23500.0]["ce"]["lot_size"] == 75
    # Strikes returned in ascending order.
    assert [s["strike"] for s in out["strikes"]] == [23500.0, 23600.0]


@pytest.mark.asyncio
async def test_get_quote_uses_data_path_and_nested_v_shape() -> None:
    """Regression: Fyers /data/quotes nests the live values under `v`
    ({"n": sym, "v": {"lp": ..}}) and lives on the /data host path. The
    old code hit /api/v3/quotes and read lp flat off the entry, so the
    quote feed was silently empty."""
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        return httpx.Response(
            200,
            json={
                "s": "ok",
                "d": [
                    {"n": "NSE:SBIN-EQ", "s": "ok",
                     "v": {"lp": 612.5, "bid": 612.0, "ask": 613.0, "volume": 1000}},
                    {"n": "NSE:RELIANCE-EQ", "s": "ok", "v": {"lp": 0}},  # zero → skipped
                ],
            },
        )

    client = _make_client(handler)
    backend = FyersLiveBackend(
        app_id="APP123", access_token="TOK",
        broker_account_id=1, account_name="m", client=client,
    )
    try:
        quotes = await backend.get_quote(["NSE:SBIN-EQ", "NSE:RELIANCE-EQ"])
        assert "/data/quotes" in captured["url"], captured["url"]
        assert len(quotes) == 1  # the lp=0 entry is dropped
        q = quotes[0]
        assert q.symbol == "NSE:SBIN-EQ"
        assert q.last_price == 612.5
        assert q.bid == 612.0
        assert q.ask == 613.0
        assert q.volume == 1000
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_place_order_rejects_missing_required_price() -> None:
    """SL-M without a trigger, and SL-L without a limit, are rejected
    locally with a clear message — never sent to Fyers."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("must not reach Fyers")

    client = _make_client(handler)
    backend = FyersLiveBackend(
        app_id="APP123", access_token="TOK",
        broker_account_id=1, account_name="main", client=client,
    )
    try:
        r1 = await backend.place_order(
            signal=None, symbol="NSE:SBIN-EQ", side=OrderSide.SELL, quantity=1,
            order_type=OrderType.STOP_LOSS_MARKET,  # no stop_price
        )
        assert r1.state == OrderState.REJECTED
        assert "stop" in (r1.error or "").lower()

        r2 = await backend.place_order(
            signal=None, symbol="NSE:SBIN-EQ", side=OrderSide.SELL, quantity=1,
            order_type=OrderType.STOP_LOSS, stop_price=600.0,  # no limit_price
        )
        assert r2.state == OrderState.REJECTED
        assert "limit" in (r2.error or "").lower()
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
async def test_cloudflare_blocked_html_raises_fyers_blocked_error() -> None:
    """Fyers sits behind Cloudflare. The default httpx `User-Agent`
    gets blocked, and the response is a Cloudflare "Attention
    Required!" HTML page (HTTP 403, content-type text/html). We
    must surface a clean `FyersBlockedError` with a one-line
    operator-friendly message, never the raw HTML. This is the
    regression test for the "fyers 403: <!DOCTYPE html>..."
    error that leaked into the Trade page's banner."""
    cloudflare_html = (
        "<!DOCTYPE html><html><head>"
        "<title>Attention Required! | Cloudflare</title>"
        "</head><body>Please complete the captcha.</body></html>"
    )

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            text=cloudflare_html,
            headers={
                "Content-Type": "text/html; charset=UTF-8",
                "Server": "cloudflare",
            },
        )

    # max_retries=0 so we don't wait on the 5xx backoff path.
    client = _make_client(handler, max_retries=0)
    try:
        with pytest.raises(FyersBlockedError) as exc:
            await client.place_order({"symbol": "NSE:SBIN-EQ", "qty": 1})
        assert exc.value.status_code == 403
        # Operator-friendly message, no HTML.
        msg = str(exc.value)
        assert "<!DOCTYPE" not in msg
        assert "<html" not in msg
        assert "Fyers edge is blocking" in msg
        # The full HTML body is preserved on `body` for log forensics.
        assert "Attention Required" in (exc.value.body or "")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_real_fyers_json_4xx_with_cloudflare_server_is_not_a_block() -> None:
    """Regression: every Fyers response carries `Server: cloudflare`
    (Fyers is fronted by Cloudflare in production). An earlier
    version of `_looks_like_blocked_response` keyed off that header
    alone, which mis-classified EVERY Fyers response — including
    legitimate 4xx JSON errors like a 400 "Order placement
    restricted" or a 401 "Could not authenticate the user" — as a
    Cloudflare challenge. The Trade page would then show
    "Fyers edge is blocking the request (anti-bot)…" instead of
    the real broker error. The fix: only flag a response as a
    challenge when the body or content-type ALSO look like a
    challenge page (HTML, "Attention Required!", or content-type
    contains "cloudflare"). Plain JSON 4xx/5xx are passed through
    to the caller as `FyersAPIError` with the real message.
    """
    def handler(req: httpx.Request) -> httpx.Response:
        # Legitimate Fyers auth error — real JSON, `Server: cloudflare`
        # because Fyers is fronted by Cloudflare for every request.
        return httpx.Response(
            401,
            json={"code": -16, "message": "Could not authenticate the user", "s": "error"},
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Server": "cloudflare",
            },
        )

    client = _make_client(handler, max_retries=0)
    try:
        # A legitimate Fyers JSON 4xx MUST raise FyersAPIError, not
        # FyersBlockedError. The "Server: cloudflare" header alone
        # is not enough to flag this as a challenge.
        with pytest.raises(FyersAPIError) as exc:
            await client.place_order({"symbol": "NSE:SBIN-EQ", "qty": 1})
        # And it must NOT be the FyersBlockedError subclass.
        assert not isinstance(exc.value, FyersBlockedError), (
            "legitimate Fyers JSON 4xx must not be mis-classified as a CDN block"
        )
        assert exc.value.status_code == 401
        # The real broker error message is preserved in the exception
        # so the operator sees the actual reason (auth failure,
        # order restriction, etc.) instead of a generic anti-bot
        # banner.
        body = exc.value.body or ""
        assert "Could not authenticate" in body
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_fyers_live_backend_place_order_converts_block_to_rejected() -> None:
    """The `FyersLiveBackend.place_order` method must catch
    `FyersBlockedError` and convert it to a REJECTED `OrderResult`
    with a clean one-liner — never a long exception chain and
    never the raw Cloudflare HTML. This is the contract the
    Trade page's banner depends on."""
    cloudflare_html = (
        "<!DOCTYPE html><html><head>"
        "<title>Attention Required! | Cloudflare</title>"
        "</head><body>...</body></html>"
    )

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            text=cloudflare_html,
            headers={
                "Content-Type": "text/html; charset=UTF-8",
                "Server": "cloudflare",
            },
        )

    client = _make_client(handler, max_retries=0)
    backend = FyersLiveBackend(
        app_id="APP123", access_token="TOK",
        broker_account_id=1, account_name="main", client=client,
    )
    try:
        result = await backend.place_order(
            signal=None,
            symbol="NSE:SBIN-EQ",
            side=OrderSide.BUY,
            quantity=1,
            order_type=OrderType.MARKET,
        )
        assert result.state == OrderState.REJECTED
        assert result.broker_order_id == ""
        # The error string is short and contains a clear cause.
        # Crucially, no HTML makes it into the operator's UI.
        assert result.error is not None
        assert "<!DOCTYPE" not in result.error
        assert "<html" not in result.error
        assert "Fyers edge blocked" in result.error
        # The diagnostic reason is preserved in `raw` for the API
        # response shape.
        assert result.raw is not None
        assert result.raw.get("reason") in (
            "non_json_html",
            "cloudflare_challenge",
        )
    finally:
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
async def test_place_order_ip_whitelist_marker() -> None:
    """Fyers returns the misleading message "Algo orders are not
    allowed from this app" (code -50) when the server's IP isn't
    on the app's IP-whitelist. We detect that pattern and tag
    the OrderResult with `raw.reason = "ip_whitelist"` so the
    Trade page can render a copyable public-IP hint. The error
    string itself is rewritten to a one-liner that points the
    operator at https://myapi.fyers.in/dashboard/.
    """
    fyers_response = {
        "s": "error",
        "code": -50,
        "message": "Request rejected: Order placement restricted. Algo orders are not allowed from this app 9TJPAJSKCX-100",
    }

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=fyers_response)

    client = _make_client(handler, max_retries=0)
    backend = FyersLiveBackend(
        app_id="9TJPAJSKCX-100", access_token="TOK",
        broker_account_id=1, account_name="main", client=client,
    )
    try:
        result = await backend.place_order(
            signal=None, symbol="NSE:SBIN-EQ",
            side=OrderSide.BUY, quantity=1,
        )
        assert result.state == OrderState.REJECTED
        assert result.broker_order_id == ""
        # The rewritten error string points at the dashboard,
        # not at the misleading "algo orders" message.
        err = result.error or ""
        assert "<!DOCTYPE" not in err
        assert "ip" in err.lower() and "whitelist" in err.lower()
        assert "myapi.fyers.in" in err
        # The diagnostic marker is set on `raw.reason` for the
        # UI to render the IP-hint banner.
        assert result.raw is not None
        assert result.raw.get("reason") == "ip_whitelist"
        assert result.raw.get("status_code") == 400
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_place_order_app_not_allowed_variant() -> None:
    """The OTHER code -50 wording Fyers emits — "Order placement are
    not allowed from this app <APP_ID>" (no "algo orders" phrase) —
    must be caught too. It's the same app-level block (wrong app_id /
    IP not whitelisted / missing Order Placement permission). We tag
    `reason="ip_whitelist"` for the UI hint AND surface the rejected
    app_id so the operator can compare it against their dashboard.
    """
    fyers_response = {
        "s": "error",
        "code": -50,
        "message": "Request rejected: Order placement are not allowed from this app XTK927LW7V-100",
    }

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=fyers_response)

    # The injected client authenticates as "APP123" (see _make_client);
    # Fyers names a *different* app in the rejection — exactly the
    # mismatch the surfaced ids let the operator spot.
    client = _make_client(handler, max_retries=0)
    backend = FyersLiveBackend(
        app_id="APP123", access_token="TOK",
        broker_account_id=1, account_name="main", client=client,
    )
    try:
        result = await backend.place_order(
            signal=None, symbol="NSE:RELIANCE-EQ",
            side=OrderSide.BUY, quantity=1,
        )
        assert result.state == OrderState.REJECTED
        assert result.broker_order_id == ""
        err = result.error or ""
        assert "<!DOCTYPE" not in err
        assert "myapi.fyers.in" in err
        # The rejected app_id from the message is named so a mismatch
        # against the configured app is obvious.
        assert "XTK927LW7V-100" in err
        assert result.raw is not None
        assert result.raw.get("reason") == "ip_whitelist"
        assert result.raw.get("rejected_app_id") == "XTK927LW7V-100"
        assert result.raw.get("configured_app_id") == "APP123"
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
