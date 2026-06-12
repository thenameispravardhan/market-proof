"""Fyers live-trading backend.

A thin async httpx wrapper around the Fyers v3 REST API. We picked
`httpx` over the official `fyers-apiv3` SDK for these reasons:

  1. The official SDK is synchronous and would force us to wrap
     every call in `run_in_executor`. Our whole pipeline is
     `async` from `event_bus.subscribe` to `place_order` — going
     sync at the broker boundary would be a step backwards.
  2. The SDK adds zero functionality we need (just an XML/JSON
     wrapper around the same REST endpoints). The endpoints are
     stable and well documented at:
         https://myapi.fyers.in/docs/
         https://api-t1.fyers.in/api/v3/
  3. We have a single async client with one connection pool, so
     the "rate limit awareness" the spec asks for is trivial: a
     semaphore (default 5 in flight) + a small async sleep on 429.

The pinned httpx version is 0.28.1 (see requirements.txt). If
we ever swap to `fyers-apiv3`, the version pin is 3.1.6 — see
the docstring in `_SDK_CANDIDATES`.

API surface:

    place_order()     POST /orders  (or /api/v3/orders/sync)
    cancel_order()    DELETE /orders?id=<id>
    get_positions()   GET /positions
    get_order_status  GET /orders?id=<id>
    get_quote()       GET /quotes?symbols=...

`place_order` is fire-and-poll: Fyers' sync endpoint returns the
order id immediately, and the manager polls `get_order_status`
until the order moves out of PENDING (or the timeout expires).

Auth: every request carries `Authorization: <app_id>:<access_token>`.
The access_token is loaded from the BrokerAccount row at construction
time. The token is rotated via the `/api/fyers/callback` endpoint,
which updates the row; this backend caches the token at construction
and on `set_access_token` so the manager can re-inject the new token
without rebuilding the backend.

Failure handling per the spec:
  - 5xx (transport / server) -> retry with 1s, 2s, 4s backoff (max 3).
  - 429 (rate limit)         -> retry with longer backoff (5s).
  - 401 (token expired)      -> raise `FyersAuthError` (caller decides).
  - 4xx other                -> raise `FyersAPIError` immediately.
  - Network timeout          -> retry.
  - On exhausted retries     -> trade row stays `placed` (PENDING).
    The manager's poll loop will catch the timeout.

Rate limiting:
  The Fyers API has a documented limit of 10 requests/second on
  /quotes and 10 orders/second on /orders. We cap at 5 concurrent
  in-flight requests with an asyncio.Semaphore; on 429 we back
  off 5s and retry.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.execution.base import (
    OrderResult,
    OrderSide,
    OrderState,
    OrderStatus,
    OrderType,
    Position,
    safe_float,
)
from app.execution.market_data import Quote
from app.logging_config import get_logger

log = get_logger(__name__)


# SDK candidates we considered. The official `fyers-apiv3` SDK
# exists at pypi.org/project/fyers-apiv3 (latest 3.1.6 at writing)
# but it's synchronous and ties us to the SDK's release cadence.
# We prefer httpx for the reasons in the module docstring.
_SDK_CANDIDATES = {
    "httpx (chosen)": "0.28.1",
    "fyers-apiv3 (not chosen)": "3.1.6",
}


# ---- Errors -------------------------------------------------------------


class FyersAPIError(Exception):
    """Raised on Fyers API failure (4xx, transport, timeout, etc.).

    `status_code` is None for transport errors. The error message
    is sanitised — the access_token is never echoed.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        body: Optional[str] = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.retryable = retryable


class FyersAuthError(FyersAPIError):
    """Subclass of FyersAPIError raised on 401."""


# ---- Low-level client ----------------------------------------------------


@dataclass
class _Config:
    app_id: str
    secret_key: str          # used only for appIdHash refresh; not sent.
    access_token: str
    base_url: str = "https://api-t1.fyers.in/api/v3"
    timeout_s: float = 30.0
    max_retries: int = 3
    rate_limit_sleep_s: float = 5.0  # on 429
    transport: Optional[httpx.AsyncBaseTransport] = None


class FyersClient:
    """Low-level async client for the Fyers v3 REST API.

    Owns one `httpx.AsyncClient` + one `asyncio.Semaphore` for rate
    limiting. All methods are async; tests inject a custom transport
    via the `transport` constructor argument.
    """

    RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        *,
        app_id: str,
        access_token: str,
        base_url: str = "https://api-t1.fyers.in/api/v3",
        timeout_s: float = 30.0,
        max_retries: int = 3,
        concurrency: int = 5,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        if not app_id:
            raise ValueError("app_id is required")
        if not access_token:
            raise ValueError("access_token is required")
        self._app_id = app_id
        self._access_token = access_token
        self._base_url = base_url.rstrip("/")
        self._timeout_s = float(timeout_s)
        self._max_retries = int(max_retries)
        self._semaphore = asyncio.Semaphore(concurrency)
        self._transport = transport
        self._client: Optional[httpx.AsyncClient] = None

    # -- lifecycle -------------------------------------------------------

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=self._transport or httpx.AsyncHTTPTransport(),
                timeout=self._timeout_s,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def set_access_token(self, access_token: str) -> None:
        """Rotate the access_token. Caller is responsible for
        persisting the new value to the broker_accounts row."""
        if not access_token:
            raise ValueError("access_token is required")
        self._access_token = access_token

    @property
    def access_token(self) -> str:
        return self._access_token

    # -- auth header -----------------------------------------------------

    def _auth_header(self) -> str:
        # Fyers format: `<app_id>:<access_token>`.
        return f"{self._app_id}:{self._access_token}"

    # -- request core ----------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url}/{path.lstrip('/')}"
        headers = {
            "Authorization": self._auth_header(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        last_err: Optional[Exception] = None
        for attempt in range(self._max_retries + 1):
            try:
                async with self._semaphore:
                    client = await self._ensure_client()
                    resp = await client.request(
                        method, url, headers=headers, params=params, json=json_body
                    )
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_err = FyersAPIError(
                    f"transport error: {e!r}", retryable=True
                )
                if attempt >= self._max_retries:
                    log.warning(
                        "fyers.transport_exhausted",
                        method=method, path=path, error=str(e),
                    )
                    raise last_err from e
                await self._backoff(attempt, base=1.0)
                continue

            # 401 — token expired. Caller rotates and retries.
            if resp.status_code == 401:
                raise FyersAuthError(
                    "fyers 401: token invalid or expired",
                    status_code=401,
                    body=_safe_body(resp),
                )
            # 429 — explicit rate limit.
            if resp.status_code == 429:
                last_err = FyersAPIError(
                    "fyers 429: rate limited",
                    status_code=429, body=_safe_body(resp), retryable=True,
                )
                if attempt >= self._max_retries:
                    raise last_err
                log.info(
                    "fyers.rate_limited",
                    method=method, path=path, attempt=attempt + 1,
                )
                await asyncio.sleep(self._rate_limit_sleep_s())
                continue
            # 5xx — retryable.
            if resp.status_code in self.RETRYABLE_STATUS:
                last_err = FyersAPIError(
                    f"fyers {resp.status_code}",
                    status_code=resp.status_code, body=_safe_body(resp),
                    retryable=True,
                )
                if attempt >= self._max_retries:
                    raise last_err
                log.info(
                    "fyers.retry",
                    method=method, path=path, status=resp.status_code,
                    attempt=attempt + 1,
                )
                await self._backoff(attempt, base=1.0)
                continue
            # Other 4xx — terminal.
            if resp.status_code >= 400:
                raise FyersAPIError(
                    f"fyers {resp.status_code}: {_safe_body(resp)}",
                    status_code=resp.status_code,
                    body=_safe_body(resp),
                )
            # 2xx — parse JSON and return.
            try:
                data = resp.json()
            except Exception as e:  # noqa: BLE001
                raise FyersAPIError(
                    f"fyers returned non-JSON: {e!r}",
                    status_code=resp.status_code,
                    body=_safe_body(resp),
                ) from e
            return data if isinstance(data, dict) else {"data": data}

        # Unreachable.
        assert last_err is not None
        raise last_err

    def _rate_limit_sleep_s(self) -> float:
        # Operators can override via subclassing; default is 5s.
        return 5.0

    async def _backoff(self, attempt: int, base: float = 1.0) -> None:
        await asyncio.sleep(base * (2 ** attempt))

    # -- public API ------------------------------------------------------

    async def place_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /orders. Payload follows Fyers' order schema."""
        return await self._request("POST", "/orders", json_body=payload)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """DELETE /orders?id=<id>."""
        return await self._request("DELETE", "/orders", params={"id": order_id})

    async def get_positions(self) -> dict[str, Any]:
        """GET /positions."""
        return await self._request("GET", "/positions")

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        """GET /orders?id=<id>."""
        return await self._request("GET", "/orders", params={"id": order_id})

    async def get_quote(self, symbols: list[str]) -> dict[str, Any]:
        """GET /quotes?symbols=<comma-separated>."""
        if not symbols:
            return {"s": "ok", "d": []}
        return await self._request(
            "GET", "/quotes", params={"symbols": ",".join(symbols)}
        )


# ---- Helpers -------------------------------------------------------------


def _safe_body(resp: httpx.Response) -> str:
    try:
        text = resp.text
    except Exception:  # noqa: BLE001
        return "<unreadable>"
    if len(text) > 500:
        return text[:500] + "...<truncated>"
    return text


def _fyers_order_type(order_type: OrderType) -> int:
    """Fyers order-type codes. The exact mapping is documented at
    https://myapi.fyers.in/docs/#tag/Orders. We expose ints because
    Fyers' API is int-keyed; the backend converts to the matching
    OrderType on the way out.
    """
    return {
        OrderType.MARKET: 1,
        OrderType.LIMIT: 2,
        OrderType.STOP_LOSS: 3,
        OrderType.STOP_LOSS_MARKET: 4,
    }.get(order_type, 1)


def _fyers_side(side: OrderSide) -> int:
    return 1 if side == OrderSide.BUY else -1


def _order_type_from_int(code: int) -> OrderType:
    return {
        1: OrderType.MARKET,
        2: OrderType.LIMIT,
        3: OrderType.STOP_LOSS,
        4: OrderType.STOP_LOSS_MARKET,
    }.get(code, OrderType.MARKET)


def _state_from_str(status: str) -> OrderState:
    """Map a Fyers status field to our OrderState.

    Fyers' documented status codes (string digits):
        1 = Cancelled
        2 = Traded (filled)
        3 = (unused / partial)
        4 = Pending
        5 = Rejected
        6 = Expired
    We also accept the upper-cased English form for tolerance.
    """
    s = (status or "").strip()
    if s.isdigit():
        return {
            "1": OrderState.CANCELLED,
            "2": OrderState.FILLED,
            "3": OrderState.PENDING,
            "4": OrderState.PENDING,
            "5": OrderState.REJECTED,
            "6": OrderState.EXPIRED,
        }.get(s, OrderState.PENDING)
    upper = s.upper()
    return {
        "PENDING": OrderState.PENDING,
        "PLACED": OrderState.PENDING,
        "TRADED": OrderState.FILLED,
        "FILLED": OrderState.FILLED,
        "CANCELLED": OrderState.CANCELLED,
        "CANCELED": OrderState.CANCELLED,
        "REJECTED": OrderState.REJECTED,
        "EXPIRED": OrderState.EXPIRED,
    }.get(upper, OrderState.PENDING)


# ---- The backend --------------------------------------------------------


class FyersLiveBackend:
    """TradingBackend implementation for the Fyers v3 API.

    Wraps a specific `broker_accounts` row. The access_token is
    loaded at construction; `set_access_token` rotates it after
    the OAuth callback updates the DB.

    `name` and `broker_account_id` are exposed as class attributes
    (the protocol uses them). `name` is set in the constructor so
    each backend instance has a unique name (e.g. "fyers#1").
    """

    broker_account_id: Optional[int] = None

    def __init__(
        self,
        *,
        app_id: str,
        access_token: str,
        broker_account_id: int,
        account_name: str = "fyers",
        client: Optional[FyersClient] = None,
    ) -> None:
        if not app_id:
            raise ValueError("app_id is required for FyersLiveBackend")
        if not access_token:
            raise ValueError("access_token is required for FyersLiveBackend")
        self.name = f"fyers:{account_name}"
        self.broker_account_id = broker_account_id
        self._client = client or FyersClient(app_id=app_id, access_token=access_token)

    async def aclose(self) -> None:
        await self._client.aclose()

    def set_access_token(self, access_token: str) -> None:
        self._client.set_access_token(access_token)

    # -- TradingBackend protocol -----------------------------------------

    async def place_order(
        self,
        *,
        signal: Any,
        symbol: str,
        side: OrderSide,
        quantity: int,
        order_type: OrderType = OrderType.MARKET,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
    ) -> OrderResult:
        if int(quantity) <= 0:
            return OrderResult(
                broker_order_id="",
                state=OrderState.REJECTED,
                symbol=symbol,
                side=side,
                quantity=int(quantity),
                order_type=order_type,
                error="quantity must be > 0",
            )
        payload = {
            "symbol": symbol,
            "qty": int(quantity),
            "type": _fyers_order_type(order_type),
            "side": _fyers_side(side),
            "productType": "INTRADAY",
            "limitPrice": float(limit_price) if limit_price is not None else 0.0,
            "stopPrice": float(stop_price) if stop_price is not None else 0.0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
        }
        try:
            data = await self._client.place_order(payload)
        except FyersAuthError as e:
            log.error("fyers.place_order.auth_error", symbol=symbol, error=str(e))
            return OrderResult(
                broker_order_id="",
                state=OrderState.REJECTED,
                symbol=symbol,
                side=side,
                quantity=int(quantity),
                order_type=order_type,
                error=str(e),
                raw={"status_code": e.status_code},
            )
        except FyersAPIError as e:
            # Retryable failures stay PENDING (manager will time out
            # the row). Non-retryable 4xx are REJECTED.
            if e.retryable:
                log.warning("fyers.place_order.retryable", symbol=symbol, error=str(e))
                return OrderResult(
                    broker_order_id="",
                    state=OrderState.PENDING,
                    symbol=symbol,
                    side=side,
                    quantity=int(quantity),
                    order_type=order_type,
                    error=str(e),
                    raw={"status_code": e.status_code},
                )
            log.error("fyers.place_order.failed", symbol=symbol, error=str(e))
            return OrderResult(
                broker_order_id="",
                state=OrderState.REJECTED,
                symbol=symbol,
                side=side,
                quantity=int(quantity),
                order_type=order_type,
                error=str(e),
                raw={"status_code": e.status_code},
            )
        order_id = str(data.get("id") or data.get("orderNumber") or "")
        if not order_id:
            # Fyers returned 2xx but no order id. Treat as PENDING —
            # the manager's poll loop will resolve it.
            return OrderResult(
                broker_order_id="",
                state=OrderState.PENDING,
                symbol=symbol,
                side=side,
                quantity=int(quantity),
                order_type=order_type,
                error="fyers returned no order id",
                raw=data,
            )
        return OrderResult(
            broker_order_id=order_id,
            state=OrderState.PENDING,
            symbol=symbol,
            side=side,
            quantity=int(quantity),
            order_type=order_type,
            limit_price=limit_price,
            stop_price=stop_price,
            raw=data,
        )

    async def cancel_order(self, broker_order_id: str) -> bool:
        if not broker_order_id:
            return False
        try:
            await self._client.cancel_order(broker_order_id)
            return True
        except FyersAPIError as e:
            log.warning("fyers.cancel.failed",
                        broker_order_id=broker_order_id, error=str(e))
            return False

    async def get_positions(self) -> list[Position]:
        try:
            data = await self._client.get_positions()
        except FyersAPIError as e:
            log.warning("fyers.positions.failed", error=str(e))
            return []
        positions_data = data.get("netPositions") or data.get("data") or []
        out: list[Position] = []
        for entry in positions_data:
            try:
                sym = str(entry.get("symbol") or "")
                if not sym:
                    continue
                qty = int(entry.get("netQty") or entry.get("qty") or 0)
                avg = safe_float(entry.get("avgPrice") or entry.get("buyAvg") or 0.0)
                last = safe_float(entry.get("ltp") or entry.get("lastPrice"))
                unrealized = safe_float(entry.get("unrealizedProfit"))
                out.append(
                    Position(
                        symbol=sym,
                        quantity=qty,
                        average_price=avg,
                        last_price=last if last > 0 else None,
                        unrealized_pnl=unrealized if unrealized != 0 else None,
                        broker_account_id=self.broker_account_id,
                    )
                )
            except Exception:  # noqa: BLE001
                log.exception("fyers.positions.parse_error")
        return out

    async def get_order_status(self, broker_order_id: str) -> OrderStatus:
        if not broker_order_id:
            return OrderStatus(
                broker_order_id="",
                state=OrderState.REJECTED,
                error="empty broker_order_id",
            )
        try:
            data = await self._client.get_order_status(broker_order_id)
        except FyersAuthError as e:
            return OrderStatus(
                broker_order_id=broker_order_id,
                state=OrderState.REJECTED,
                error=str(e),
            )
        except FyersAPIError as e:
            return OrderStatus(
                broker_order_id=broker_order_id,
                state=OrderState.PENDING,
                error=str(e),
            )
        order_list = data.get("orderBook") or data.get("data") or []
        if not order_list:
            return OrderStatus(
                broker_order_id=broker_order_id,
                state=OrderState.PENDING,
                raw=data,
            )
        # Fyers returns an array; pick the one matching our id.
        match = None
        for entry in order_list:
            if str(entry.get("id") or "") == broker_order_id:
                match = entry
                break
        if match is None:
            return OrderStatus(
                broker_order_id=broker_order_id,
                state=OrderState.PENDING,
                raw=data,
            )
        status = str(match.get("status") or "PENDING")
        filled_qty = int(match.get("tradedQty") or 0)
        avg_price = safe_float(match.get("tradedPrice"))
        return OrderStatus(
            broker_order_id=broker_order_id,
            state=_state_from_str(status),
            filled_quantity=filled_qty,
            average_price=avg_price if avg_price > 0 else None,
            raw=match,
        )

    # -- quote feed (used by the manager for live PnL) ------------------

    async def get_quote(self, symbols: list[str]) -> list[Quote]:
        if not symbols:
            return []
        try:
            data = await self._client.get_quote(symbols)
        except FyersAPIError as e:
            log.warning("fyers.quote.failed", error=str(e))
            return []
        dlist = data.get("d") or data.get("data") or []
        out: list[Quote] = []
        for entry in dlist:
            try:
                sym = str(entry.get("symbol") or entry.get("n") or "")
                if not sym:
                    continue
                lp = safe_float(entry.get("lp") or entry.get("lastPrice") or 0.0)
                if lp <= 0:
                    continue
                out.append(
                    Quote(
                        symbol=sym,
                        last_price=lp,
                        bid=safe_float(entry.get("bid")) or None,
                        ask=safe_float(entry.get("ask")) or None,
                        volume=int(entry.get("vol") or 0) or None,
                    )
                )
            except Exception:  # noqa: BLE001
                log.exception("fyers.quote.parse_error")
        return out
