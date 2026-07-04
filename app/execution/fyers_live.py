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

    place_order()     POST /orders/sync  (matches the official fyers-apiv3 SDK)
    cancel_order()    DELETE /orders?id=<id>
    get_positions()   GET /positions
    get_order_status  GET /orders?id=<id>
    get_quote()       GET /quotes?symbols=...

We use /orders/sync (not the plain /orders) because the plain
endpoint is fronted by a Cloudflare anti-bot layer that returns
HTML challenge pages to non-browser clients in production. The
/sync variant returns real Fyers JSON. See
https://pypi.org/project/fyers-apiv3/ for the SDK reference.

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
import re
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
    ProductType,
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


class FyersBlockedError(FyersAPIError):
    """The Fyers edge (or a CDN in front of it — Cloudflare, Akamai,
    etc.) refused the request as a bot/script. We surface a one-line
    operator-friendly message; the raw HTML body never reaches the
    UI or the trade error. Distinguished from `FyersAPIError` so
    the place-order flow can map it to a "rejected, fix creds /
    network" outcome rather than a transient retryable error.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        body: Optional[str] = None,
        reason: str = "blocked",
    ) -> None:
        super().__init__(message, status_code=status_code, body=body)
        self.reason = reason


def _looks_like_blocked_response(resp: httpx.Response) -> Optional[str]:
    """Return a one-line reason if `resp` looks like a bot block /
    challenge page rather than a real Fyers API response.

    Fyers sits behind Cloudflare in production. The default httpx
    `User-Agent: python-httpx/X.Y` is treated as a script and gets
    a 403 with a Cloudflare "Attention Required!" challenge HTML
    instead of the real API. We detect that and a few friends here
    so the operator sees a clear "your backend is being blocked"
    message instead of a 500-char wall of HTML.

    IMPORTANT: every Fyers response carries `Server: cloudflare`,
    even legitimate JSON ones (auth errors, rate limits, etc.). We
    must NOT key off that header alone — only flag a response as
    a challenge when the body / content-type also look like a
    challenge page. Otherwise every successful API call would be
    mis-classified as blocked. See `tests/test_fyers_live.py::
    test_cloudflare_blocked_html_raises_fyers_blocked_error` for
    the regression we care about.
    """
    ct = (resp.headers.get("content-type") or "").lower()
    # Cheap, low-cost markers first (no body read). The text checks
    # below are the only ones that need the body, and they read
    # at most the first 2 KB.
    body_lower = (resp.text or "")[:2000].lower()
    if "text/html" in ct and "json" not in ct:
        # Real Fyers endpoints always return JSON. HTML with a
        # 4xx/5xx almost certainly means we hit a CDN challenge /
        # login page.
        return "non_json_html"
    if "attention required" in body_lower:
        return "cloudflare_challenge"
    if "access denied" in body_lower and "json" not in ct:
        return "cdn_access_denied"
    if "cloudflare" in ct:
        # Cloudflare sometimes tags challenge responses with a
        # content-type like `text/html; charset=UTF-8` plus a
        # `__cf_bm` cookie header; we don't read cookies here, so
        # fall back to the explicit "cloudflare" content-type
        # signal. Plain JSON responses from Fyers are served with
        # `content-type: application/json`, so this never matches
        # a normal API call.
        return "cloudflare_challenge"
    # Plain JSON 4xx/5xx from Fyers is a real broker / auth /
    # validation error, not a CDN block — let the caller see the
    # response body in the error message.
    return None


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
        data_base_url: str = "https://api-t1.fyers.in/data",
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
        # Fyers v3 market-data endpoints (quotes / history / depth /
        # options-chain) live under `/data`, NOT `/api/v3`. Orders use
        # `_base_url`; data calls pass `base_url=self._data_base_url`.
        self._data_base_url = data_base_url.rstrip("/")
        self._timeout_s = float(timeout_s)
        self._max_retries = int(max_retries)
        self._semaphore = asyncio.Semaphore(concurrency)
        self._transport = transport
        self._client: Optional[httpx.AsyncClient] = None

    # -- lifecycle -------------------------------------------------------

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # Default httpx `User-Agent: python-httpx/X.Y` is on every
            # CDN bot-blocklist in existence. Fyers sits behind
            # Cloudflare; with the default UA, real-API requests come
            # back as 403 + Cloudflare "Attention Required!" HTML.
            # The UA below is a generic browser-style header — not
            # impersonating any specific browser — that the Fyers edge
            # accepts as a normal client.
            self._client = httpx.AsyncClient(
                transport=self._transport or httpx.AsyncHTTPTransport(),
                timeout=self._timeout_s,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (compatible; TradeBot/1.0; "
                        "+https://github.com/local/tradebot)"
                    ),
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "en-US,en;q=0.9",
                },
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

    @property
    def app_id(self) -> str:
        return self._app_id

    @property
    def ws_access_token(self) -> str:
        """The `<app_id>:<access_token>` string the Fyers WebSocket SDK
        (data + order sockets) expects as its `access_token` argument —
        the same format as the REST `Authorization` header."""
        return self._auth_header()

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
        base_url: Optional[str] = None,
    ) -> dict[str, Any]:
        url = f"{(base_url or self._base_url).rstrip('/')}/{path.lstrip('/')}"
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
            # Bot-block detection runs on every non-2xx (and even
            # 2xx-with-HTML, but those are vanishingly rare). If the
            # response is HTML or carries a Cloudflare challenge, we
            # raise a clean FyersBlockedError instead of dumping the
            # raw HTML body into the operator's error banner. Retry
            # won't help (the CDN has flagged the UA / IP), so this
            # is terminal.
            block_marker = _looks_like_blocked_response(resp)
            if block_marker and resp.status_code >= 400:
                log.warning(
                    "fyers.blocked",
                    method=method, path=path, status=resp.status_code,
                    marker=block_marker, server=resp.headers.get("server"),
                    content_type=resp.headers.get("content-type"),
                )
                raise FyersBlockedError(
                    "Fyers edge is blocking requests as a script — "
                    "Cloudflare / anti-bot challenge detected. "
                    "Check that the server's outbound IP isn't on a "
                    "blocklist and that the User-Agent is acceptable.",
                    status_code=resp.status_code,
                    body=_safe_body(resp),
                    reason=block_marker,
                )
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
        """POST /orders/sync. Payload follows Fyers' order schema.

        The /orders/sync endpoint (not the plain /orders) is the
        one that matches the official `fyers-apiv3` SDK's behaviour
        and the one that returns real Fyers JSON on the most
        common Fyers production environment (api-t1.fyers.in). The
        plain /orders endpoint is increasingly fronted by a CDN
        anti-bot layer that returns a 403 Cloudflare challenge to
        any non-browser client — including httpx. Using /sync
        avoids that path entirely. See also
        https://pypi.org/project/fyers-apiv3/ for the SDK reference.
        """
        return await self._request("POST", "/orders/sync", json_body=payload)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """DELETE /orders?id=<id>."""
        return await self._request("DELETE", "/orders", params={"id": order_id})

    async def get_positions(self) -> dict[str, Any]:
        """GET /positions."""
        return await self._request("GET", "/positions")

    async def get_funds(self) -> dict[str, Any]:
        """GET /funds — account fund limits.

        Returns the raw Fyers payload: `{"s": "ok", "fund_limit":
        [{"id": 1, "title": "Total Balance", "equityAmount": …}, …]}`.
        """
        return await self._request("GET", "/funds")

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        """GET /orders?id=<id>."""
        return await self._request("GET", "/orders", params={"id": order_id})

    async def get_quote(self, symbols: list[str]) -> dict[str, Any]:
        """GET /data/quotes?symbols=<comma-separated>.

        Quotes are a market-data endpoint → the `/data` host path, not
        `/api/v3` (which is orders only). Hitting `/api/v3/quotes`
        returns a 404/error, which is why the live quote feed was
        silently empty.
        """
        if not symbols:
            return {"s": "ok", "d": []}
        return await self._request(
            "GET", "/quotes", params={"symbols": ",".join(symbols)},
            base_url=self._data_base_url,
        )

    async def get_history(
        self,
        symbol: str,
        *,
        resolution: str = "5",
        range_from: str,
        range_to: str,
        date_format: int = 1,
        cont_flag: int = 1,
    ) -> dict[str, Any]:
        """GET /data/history — OHLCV candles for a symbol.

        `resolution` is the Fyers timeframe code ("5" = 5-minute, "15",
        "60", "D", …). `range_from`/`range_to` are `yyyy-mm-dd` when
        `date_format=1` (epoch seconds when 0). Returns the raw Fyers
        payload `{"candles": [[ts, o, h, l, c, v], ...]}`. Market-data
        endpoint → the `/data` host.
        """
        params = {
            "symbol": symbol,
            "resolution": str(resolution),
            "date_format": int(date_format),
            "range_from": str(range_from),
            "range_to": str(range_to),
            "cont_flag": str(cont_flag),
        }
        return await self._request(
            "GET", "/history", params=params, base_url=self._data_base_url
        )

    async def get_option_chain(
        self, symbol: str, *, strikecount: int = 10, timestamp: str = ""
    ) -> dict[str, Any]:
        """GET /data/options-chain-v3 — live option chain for an index /
        equity underlying.

        `strikecount` is the number of strikes on EACH side of ATM.
        `timestamp` selects a specific expiry (the epoch from the
        response's `expiryData`); blank = the nearest expiry.
        """
        params: dict[str, Any] = {"symbol": symbol, "strikecount": int(strikecount)}
        if timestamp:
            params["timestamp"] = timestamp
        return await self._request(
            "GET", "/options-chain-v3", params=params, base_url=self._data_base_url
        )


# ---- Helpers -------------------------------------------------------------


# Fyers app ids look like `LUVWQUOVWJ-200`, `XTK927LW7V-100`,
# `9TJPAJSKCX-100`: a run of alphanumerics, a hyphen, then digits.
_APP_ID_RE = re.compile(r"this app\s+([A-Za-z0-9]+-\d+)", re.IGNORECASE)


def _extract_rejected_app_id(message: str) -> Optional[str]:
    """Pull the app_id Fyers names in a code -50 rejection message
    (`...not allowed from this app <APP_ID>`). Returns None when the
    message doesn't carry one."""
    if not message:
        return None
    m = _APP_ID_RE.search(message)
    return m.group(1) if m else None


def _safe_body(resp: httpx.Response) -> str:
    try:
        text = resp.text
    except Exception:  # noqa: BLE001
        return "<unreadable>"
    if len(text) > 500:
        return text[:500] + "...<truncated>"
    return text


def _fyers_order_type(order_type: OrderType) -> int:
    """Fyers v3 order-type codes (Fyers API v3 Orders schema):

        1 = Limit order        (needs limitPrice)
        2 = Market order       (no price)
        3 = Stop order / SL-M  (needs stopPrice trigger; fills at market)
        4 = Stoplimit / SL-L   (needs stopPrice trigger AND limitPrice)

    NOTE: these were previously swapped (MARKET→1, LIMIT→2), which made
    every MARKET order go to Fyers as a *Limit* order with limitPrice=0 —
    rejected with "limitPrice: Must be greater than or equal to 0.0025".
    OrderType.STOP_LOSS is the UI's "SL-L" (stop-limit) → 4;
    OrderType.STOP_LOSS_MARKET is "SL-M" (stop-market) → 3.
    """
    return {
        OrderType.LIMIT: 1,
        OrderType.MARKET: 2,
        OrderType.STOP_LOSS_MARKET: 3,  # SL-M
        OrderType.STOP_LOSS: 4,         # SL-L
    }.get(order_type, 2)  # default to MARKET


def _fyers_side(side: OrderSide) -> int:
    return 1 if side == OrderSide.BUY else -1


def _order_type_from_int(code: int) -> OrderType:
    # Inverse of `_fyers_order_type` — Fyers v3 codes.
    return {
        1: OrderType.LIMIT,
        2: OrderType.MARKET,
        3: OrderType.STOP_LOSS_MARKET,  # SL-M
        4: OrderType.STOP_LOSS,         # SL-L
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


# ---- Option chain normalisation -----------------------------------------


# Known F&O lot sizes for the common index underlyings — a fallback for
# when the scrip master (which carries the authoritative per-symbol lot
# size) hasn't been downloaded. These change periodically; refresh from
# data/scrip_master/*.csv when available.
_INDEX_LOT_SIZES: dict[str, int] = {
    "NIFTYNXT50": 25,
    "MIDCPNIFTY": 120,
    "FINNIFTY": 65,
    "BANKNIFTY": 30,
    "NIFTY50": 75,
    "NIFTY": 75,
    "SENSEX": 20,
    "BANKEX": 30,
}


def _guess_lot_size(underlying_symbol: str) -> int:
    s = (underlying_symbol or "").upper()
    for key, lot in _INDEX_LOT_SIZES.items():
        if key in s:
            return lot
    return 1


def normalize_option_chain(
    data: dict[str, Any], *, underlying_symbol: str
) -> dict[str, Any]:
    """Translate Fyers' /data/options-chain-v3 response into the shape the
    Trade page renders: spot, an expiry list, and per-strike CE/PE legs
    with live ltp / bid / ask / oi.

    Fyers returns a FLAT `optionsChain` list: the first entry (option_type
    blank / strike_price <= 0) is the underlying spot, the rest are CE/PE
    rows. We group them by strike. `expiryData` is `[{date, expiry}]`
    where `expiry` is the epoch to pass back as `timestamp`.
    """
    d = data.get("data") if isinstance(data, dict) else None
    d = d or {}
    rows = d.get("optionsChain") or []
    expiry_rows = d.get("expiryData") or []
    expiries = [
        {"label": str(e.get("date") or ""), "ts": str(e.get("expiry") or "")}
        for e in expiry_rows
        if isinstance(e, dict) and e.get("expiry") is not None
    ]
    lot = _guess_lot_size(underlying_symbol)
    spot: Optional[float] = None
    by_strike: dict[float, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        otype = str(row.get("option_type") or "").upper()
        if otype not in ("CE", "PE"):
            sp = safe_float(row.get("ltp") or row.get("fp"))
            if sp > 0:
                spot = sp
            continue
        strike = safe_float(row.get("strike_price"))
        leg = {
            "symbol": str(row.get("symbol") or ""),
            "ltp": safe_float(row.get("ltp")) or None,
            "bid": safe_float(row.get("bid")) or None,
            "ask": safe_float(row.get("ask")) or None,
            "oi": int(safe_float(row.get("oi"))) or None,
            "volume": int(safe_float(row.get("volume"))) or None,
            "ltpch": safe_float(row.get("ltpch")) or None,
            "lot_size": lot,
            "tick_size": 0.05,
        }
        by_strike.setdefault(strike, {})[otype] = leg
    strikes = [
        {"strike": s, "ce": by_strike[s].get("CE"), "pe": by_strike[s].get("PE")}
        for s in sorted(by_strike.keys())
    ]
    return {
        "symbol": underlying_symbol,
        "spot": spot,
        "expiries": expiries,
        "strikes": strikes,
    }


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

    @property
    def app_id(self) -> str:
        return self._client.app_id

    @property
    def access_token(self) -> str:
        return self._client.access_token

    @property
    def ws_access_token(self) -> str:
        """`<app_id>:<access_token>` for the Fyers WebSocket SDK."""
        return self._client.ws_access_token

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
        product_type: ProductType = ProductType.INTRADAY,
        validity: str = "DAY",
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
        # Map our ProductType enum onto Fyers' productType strings.
        # Unknown values fall back to INTRADAY (the safe default).
        try:
            pt = product_type if isinstance(product_type, ProductType) else ProductType(product_type)
        except ValueError:
            pt = ProductType.INTRADAY
        fyers_product = {
            ProductType.INTRADAY: "INTRADAY",
            ProductType.DELIVERY: "CNC",
            ProductType.NORMAL: "NRML",
            ProductType.MARGIN: "MARGIN",
            ProductType.CO: "CO",
            ProductType.BO: "BO",
        }.get(pt, "INTRADAY")
        fyers_type = _fyers_order_type(order_type)
        # Per Fyers v3: Limit(1)/SL-L(4) need a positive limitPrice;
        # SL-M(3)/SL-L(4) need a positive stopPrice (trigger). A
        # Market(2) order must send both as 0. We zero out the prices
        # that don't apply so a stray value can't trip Fyers' validation,
        # and reject early (with a clear message) when a required price
        # is missing rather than letting Fyers 400.
        needs_limit = fyers_type in (1, 4)
        needs_stop = fyers_type in (3, 4)
        lp = float(limit_price) if (needs_limit and limit_price is not None) else 0.0
        sp = float(stop_price) if (needs_stop and stop_price is not None) else 0.0

        def _reject(msg: str) -> OrderResult:
            return OrderResult(
                broker_order_id="",
                state=OrderState.REJECTED,
                symbol=symbol,
                side=side,
                quantity=int(quantity),
                order_type=order_type,
                error=msg,
            )

        if needs_limit and lp <= 0:
            return _reject(f"{order_type.value} order requires a limit price > 0")
        if needs_stop and sp <= 0:
            return _reject(f"{order_type.value} order requires a stop/trigger price > 0")

        payload = {
            "symbol": symbol,
            "qty": int(quantity),
            "type": fyers_type,
            "side": _fyers_side(side),
            "productType": fyers_product,
            "limitPrice": lp,
            "stopPrice": sp,
            # IOC for speed-news auto entries (immediate-or-cancel bounds
            # slippage); DAY for everything else. Fyers v3 accepts both.
            "validity": "IOC" if str(validity).upper() == "IOC" else "DAY",
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
        except FyersBlockedError as e:
            # CDN-level block (Cloudflare, Akamai, etc.). Not
            # retryable — the operator needs to fix the network /
            # IP / UA situation. Surface a clean one-liner; the
            # full HTML body is in the log only.
            log.error(
                "fyers.place_order.blocked",
                symbol=symbol, status_code=e.status_code, reason=e.reason,
            )
            return OrderResult(
                broker_order_id="",
                state=OrderState.REJECTED,
                symbol=symbol,
                side=side,
                quantity=int(quantity),
                order_type=order_type,
                error=(
                    "Fyers edge blocked the request (anti-bot). "
                    "Check server IP allowlist / User-Agent. "
                    f"({e.reason})"
                ),
                raw={"status_code": e.status_code, "reason": e.reason},
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
            # Fyers code=-50 family: the order was refused at the
            # *app* level. Two message variants we've seen in the
            # wild, both code -50:
            #   "Order placement restricted. Algo orders are not
            #    allowed from this app <APP_ID>"
            #   "Order placement are not allowed from this app
            #    <APP_ID>"
            # These collapse to the same class of problem — the app
            # the request authenticated as is (a) not whitelisted for
            # this server's IP, (b) missing the Order Placement
            # permission, or (c) simply the WRONG app_id: the operator
            # re-created the app on https://myapi.fyers.in/dashboard/
            # but the configured/cached app_id still points at the old
            # one. The misleading "algo orders" wording (per Fyers
            # support docs) is just the IP-whitelist sub-case. We surface
            # one clear message that names the rejected app_id (so the
            # operator can eyeball it against their dashboard) and keep
            # `raw.reason = "ip_whitelist"` so the Trade page renders the
            # copyable public-IP hint.
            err_str = str(e)
            err_lower = err_str.lower()
            app_level_block = "not allowed from this app" in err_lower or (
                "order placement restricted" in err_lower
                and "algo orders" in err_lower
            )
            if app_level_block:
                configured_app_id = getattr(self._client, "_app_id", None)
                rejected_app_id = _extract_rejected_app_id(err_str) or configured_app_id
                log.error(
                    "fyers.place_order.app_level_block",
                    symbol=symbol,
                    # `app_id` lives on the FyersClient (the inner
                    # `_client`); the FyersLiveBackend doesn't keep
                    # a copy.
                    configured_app_id=configured_app_id,
                    rejected_app_id=rejected_app_id,
                    error=err_str,
                )
                app_clause = (
                    f" (app {rejected_app_id})" if rejected_app_id else ""
                )
                return OrderResult(
                    broker_order_id="",
                    state=OrderState.REJECTED,
                    symbol=symbol,
                    side=side,
                    quantity=int(quantity),
                    order_type=order_type,
                    error=(
                        f"Fyers rejected the order{app_clause}. On "
                        "https://myapi.fyers.in/dashboard/ confirm the app_id "
                        "matches .env (re-created apps need a fresh Connect "
                        "Fyers), this server's IP is on the static-IP "
                        "whitelist, and Order Placement permission is enabled."
                    ),
                    raw={
                        "status_code": e.status_code,
                        "reason": "ip_whitelist",
                        "rejected_app_id": rejected_app_id,
                        "configured_app_id": configured_app_id,
                    },
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

    async def get_funds(self) -> Optional[float]:
        """Total equity balance (₹) in the Fyers account, or None.

        Parses the `/funds` fund_limit rows: prefers the "Total Balance"
        row (id 1), falling back to a title match. Fail-safe — any error
        or a non-positive number returns None so the risk layer falls
        back to the PORTFOLIO_VALUE ledger rather than sizing off junk.
        """
        try:
            data = await self._client.get_funds()
        except FyersAPIError as e:
            log.warning("fyers.funds.failed", error=str(e))
            return None
        rows = data.get("fund_limit") or data.get("data") or []
        total: Optional[float] = None
        for entry in rows:
            try:
                is_total = int(entry.get("id") or 0) == 1 or (
                    "total balance" in str(entry.get("title") or "").lower()
                )
                if is_total:
                    total = safe_float(entry.get("equityAmount"))
                    break
            except Exception:  # noqa: BLE001
                continue
        if total is None or total <= 0:
            return None
        return float(total)

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
                if not isinstance(entry, dict):
                    continue
                # Fyers /data/quotes nests the live values under `v`:
                #   {"n": "NSE:SBIN-EQ", "s": "ok", "v": {"lp": .., "bid": ..}}
                # Fall back to a flat entry for tolerance across shapes.
                v = entry.get("v") if isinstance(entry.get("v"), dict) else entry
                sym = str(entry.get("n") or entry.get("symbol") or v.get("symbol") or "")
                if not sym:
                    continue
                lp = safe_float(
                    v.get("lp") or v.get("lastPrice") or v.get("last_price") or 0.0
                )
                if lp <= 0:
                    continue
                # Day change vs prev close. Keep a genuine 0.0 (flat),
                # only fall to None when the field is absent.
                ch_raw = v.get("ch")
                chp_raw = v.get("chp")
                pc_raw = v.get("prev_close_price") or v.get("c")
                out.append(
                    Quote(
                        symbol=sym,
                        last_price=lp,
                        bid=safe_float(v.get("bid")) or None,
                        ask=safe_float(v.get("ask")) or None,
                        volume=int(safe_float(v.get("volume") or v.get("vol") or 0)) or None,
                        change=safe_float(ch_raw) if ch_raw is not None else None,
                        change_pct=safe_float(chp_raw) if chp_raw is not None else None,
                        prev_close=(safe_float(pc_raw) or None) if pc_raw is not None else None,
                    )
                )
            except Exception:  # noqa: BLE001
                log.exception("fyers.quote.parse_error")
        return out

    # -- history (candles, for ATR) -------------------------------------

    async def get_history(
        self, symbol: str, *, resolution: str = "5", days: int = 5
    ) -> list[Any]:
        """OHLCV candles for `symbol` over the last `days` (for ATR).

        Returns the raw `[[ts, o, h, l, c, v], ...]` list (oldest→newest),
        or `[]` on any broker failure — the caller (volatility provider)
        treats `[]`/insufficient data as "no ATR" and falls back to the %
        stop. Never raises."""
        from datetime import date, timedelta

        today = date.today()
        start = today - timedelta(days=max(1, int(days)))
        try:
            data = await self._client.get_history(
                symbol,
                resolution=resolution,
                range_from=start.isoformat(),
                range_to=today.isoformat(),
            )
        except FyersAPIError as e:
            log.warning("fyers.history.failed", symbol=symbol, error=str(e))
            return []
        candles = data.get("candles") or data.get("data") or []
        return candles if isinstance(candles, list) else []

    # -- option chain (live, for the Trade page) ------------------------

    async def get_option_chain(
        self, symbol: str, *, strikecount: int = 10, timestamp: str = ""
    ) -> dict[str, Any]:
        """Live option chain for `symbol` (e.g. "NSE:NIFTY50-INDEX"),
        normalised for the UI. Raises FyersAPIError on broker failure so
        the caller can fall back to the static master chain."""
        data = await self._client.get_option_chain(
            symbol, strikecount=strikecount, timestamp=timestamp
        )
        return normalize_option_chain(data, underlying_symbol=symbol)
