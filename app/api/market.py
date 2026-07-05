"""`/api/market` — read-only market data for the dashboard status bar.

The status bar polls `GET /api/market/indices` every few seconds to
render NIFTY 50 / SENSEX / BANK NIFTY in (near) real time.

Every price in the app comes from the connected real Fyers account —
there is NO public-feed fallback. Fyers serves equities, indices,
futures and options off the same `/data/quotes` endpoint, so a single
source covers the index ticker, the Trade-page quote, and the paper /
P&L quote feed. The trade-off: the ticker is blank until a Fyers
account is connected (the status bar then shows a "connect" prompt).

  Fyers index symbols:
    NSE:NIFTY50-INDEX    NIFTY 50
    BSE:SENSEX-INDEX     SENSEX
    NSE:NIFTYBANK-INDEX  NIFTY Bank

The response is cached in-process for CACHE_TTL_S to avoid hammering
the broker. Errors never raise to the caller; the endpoint returns
`{ok: false, reason: ...}` so the UI can render an inline message.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from fastapi import APIRouter, Query

from app.execution.market_data import Quote
from app.logging_config import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["market"])

# Fyers timeframe codes the chart is allowed to request. Anything else
# is rejected before it reaches the broker ("D" = daily, numbers are
# minutes).
CHART_RESOLUTIONS: frozenset[str] = frozenset(
    {"1", "2", "3", "5", "10", "15", "20", "30", "45", "60", "120", "180", "240", "D"}
)


# key/name shown in the UI + the Fyers quote symbol.
INDICES: list[dict[str, str]] = [
    {"key": "NIFTY",     "symbol": "NSE:NIFTY50-INDEX",   "name": "NIFTY 50"},
    {"key": "SENSEX",    "symbol": "BSE:SENSEX-INDEX",    "name": "SENSEX"},
    {"key": "BANKNIFTY", "symbol": "NSE:NIFTYBANK-INDEX", "name": "BANK NIFTY"},
]

CACHE_TTL_S: float = 5.0

_cache: dict[str, Any] = {"data": None, "ts": 0.0, "lock": asyncio.Lock()}


# ---- Fyers access --------------------------------------------------------


def _manager():
    """The shared ExecutionManager (built in the FastAPI lifespan). A
    throwaway instance is returned when the lifespan hasn't run (tests),
    which is harmless because `_fyers_backend` only calls it once it has
    found a connected account — and tests have none."""
    from app.main import app

    mgr = getattr(app.state, "execution_manager", None)
    if mgr is None:
        from app.execution.manager import Manager
        from app.execution.market_data import MarketDataBus
        from app.risk.engine import RiskEngine

        return Manager(market_data=MarketDataBus(), risk_engine=RiskEngine())
    return mgr


def _fyers_backend() -> Optional[Any]:
    """The live backend for the CONNECTED real Fyers account, or None
    when no such account exists (so the caller degrades gracefully).

    "Connected" means a real (non-paper) Fyers account that holds an OAuth
    access token. This is a MARKET-DATA path (index ticker, quotes, history,
    and the realtime WebSocket feed), so it deliberately does NOT require the
    account's `enabled` flag — that flag is the *trading* on/off switch.
    Turning trading off must keep prices flowing and the OAuth login intact;
    only order routing is gated on `enabled` (manager / orders API).

    Never raises — a DB or manager hiccup returns None so the index
    endpoint keeps its "always 200" contract."""
    try:
        from sqlalchemy import select

        from app.db.models import BrokerAccount
        from app.db.session import SessionLocal

        with SessionLocal() as db:
            acc = (
                db.execute(
                    select(BrokerAccount).where(
                        BrokerAccount.broker == "fyers",
                        BrokerAccount.paper_mode == False,  # noqa: E712
                        BrokerAccount.access_token.is_not(None),
                    ).order_by(BrokerAccount.id.asc())
                )
                .scalars()
                .first()
            )
            if acc is None:
                return None
            backend = _manager()._manual_backend_for(acc)  # noqa: SLF001
            if backend is None or not hasattr(backend, "get_quote"):
                return None
            return backend
    except Exception as e:  # noqa: BLE001
        log.debug("market.fyers_backend_lookup_failed", error=str(e))
        return None


async def fyers_quotes(symbols: list[str]) -> dict[str, Quote]:
    """Live quotes for `symbols` from the connected Fyers account, keyed
    by upper-cased symbol. Empty dict when no account is connected or the
    broker call fails — there is no public-feed fallback."""
    if not symbols:
        return {}
    backend = _fyers_backend()
    if backend is None:
        return {}
    try:
        quotes = await backend.get_quote(list(symbols))
    except Exception as e:  # noqa: BLE001
        log.debug("market.fyers_quote_failed", symbols=symbols, error=str(e))
        return {}
    return {q.symbol.upper(): q for q in quotes}


async def fetch_quote(broker_symbol: str) -> Optional[dict[str, Any]]:
    """Live quote for any Fyers symbol (equity, index, future, option).
    Returns None when Fyers isn't connected or the symbol can't be served."""
    if not broker_symbol:
        return None
    sym = broker_symbol.strip().upper()
    quotes = await fyers_quotes([sym])
    # Fyers echoes the requested symbol; fall back to the lone result.
    q = quotes.get(sym) or (next(iter(quotes.values())) if len(quotes) == 1 else None)
    if q is None or not q.last_price:
        return None
    return {
        "last_price": q.last_price,
        "bid": q.bid,
        "ask": q.ask,
        "volume": q.volume,
        "change": q.change,
        "change_pct": q.change_pct,
        "prev_close": q.prev_close,
    }


async def fetch_history(
    broker_symbol: str, resolution: str = "5", days: int = 5
) -> list[Any]:
    """OHLCV candles for any Fyers symbol (for ATR). Returns [] when Fyers
    isn't connected or the call fails — the volatility provider treats an
    empty/short series as "no ATR" and falls back to the % stop."""
    if not broker_symbol:
        return []
    backend = _fyers_backend()
    if backend is None or not hasattr(backend, "get_history"):
        return []
    try:
        return await backend.get_history(
            broker_symbol.strip().upper(), resolution=resolution, days=days
        )
    except Exception as e:  # noqa: BLE001
        log.debug("market.fyers_history_failed", symbol=broker_symbol, error=str(e))
        return []


async def fetch_funds() -> Optional[float]:
    """Total equity balance (₹) of the connected Fyers account, or None
    when Fyers isn't connected or the call fails. LIVE-mode position
    sizing anchors to this so sizes follow the real account funds."""
    backend = _fyers_backend()
    if backend is None or not hasattr(backend, "get_funds"):
        return None
    try:
        return await backend.get_funds()
    except Exception as e:  # noqa: BLE001
        log.debug("market.fyers_funds_failed", error=str(e))
        return None


# ---- index ticker --------------------------------------------------------


async def _fetch_indices() -> dict[str, Any]:
    async with _cache["lock"]:
        now = time.monotonic()
        if _cache["data"] is not None and (now - _cache["ts"]) < CACHE_TTL_S:
            return _cache["data"]

        backend = _fyers_backend()
        # Shell rows so the UI always has the three index slots to render.
        rows: list[dict[str, Any]] = [
            {
                "key": idx["key"],
                "name": idx["name"],
                "symbol": idx["symbol"],
                "last_price": None,
                "change": None,
                "change_pct": None,
            }
            for idx in INDICES
        ]

        if backend is None:
            payload = {
                "ok": False,
                "configured": False,
                "reason": "connect a Fyers account for live index data",
                "indices": rows,
                "fetched_at": None,
            }
            _cache["data"] = payload
            _cache["ts"] = now
            return payload

        try:
            quotes = await fyers_quotes([idx["symbol"] for idx in INDICES])
        except Exception as e:  # noqa: BLE001
            log.warning("market.indices.fetch_failed", error=str(e))
            payload = {
                "ok": False,
                "configured": True,
                "reason": f"market data unavailable: {e!s}"[:120],
                "indices": rows,
                "fetched_at": None,
            }
            _cache["data"] = payload
            _cache["ts"] = now
            return payload

        for row in rows:
            q = quotes.get(row["symbol"].upper())
            if q is not None and q.last_price:
                row["last_price"] = q.last_price
                row["change"] = q.change
                row["change_pct"] = q.change_pct

        got_any = any(r["last_price"] is not None for r in rows)
        payload = {
            "ok": got_any,
            "configured": True,
            "reason": None if got_any else "no index data returned by Fyers",
            "indices": rows,
            "fetched_at": time.time() if got_any else None,
        }
        _cache["data"] = payload
        _cache["ts"] = now
        return payload


# ---- chart history -------------------------------------------------------


@router.get("/api/market/history")
async def market_history(
    symbol: str,
    resolution: str = "5",
    from_ts: int = Query(..., alias="from"),
    to_ts: int = Query(..., alias="to"),
) -> dict[str, Any]:
    """OHLCV candles for the Trade-page chart.

    `from`/`to` are epoch seconds (inclusive). `resolution` is a Fyers
    timeframe code ("1", "5", "15", "60", "D", …). Candles come from the
    connected real Fyers account — there is no public-feed fallback, so
    the response degrades to `{ok: false, reason}` when Fyers isn't
    connected. Always 200 so the chart can render an inline message.
    """
    sym = (symbol or "").strip().upper()
    res = (resolution or "").strip().upper()
    if not sym:
        return {"ok": False, "reason": "symbol is required", "candles": []}
    if res not in CHART_RESOLUTIONS:
        return {
            "ok": False,
            "reason": f"unsupported resolution {resolution!r}",
            "candles": [],
        }
    if to_ts <= from_ts:
        return {"ok": False, "reason": "empty time range", "candles": []}

    backend = _fyers_backend()
    if backend is None or not hasattr(backend, "get_history_range"):
        return {
            "ok": False,
            "reason": "connect a Fyers account for chart data",
            "candles": [],
        }
    try:
        candles = await backend.get_history_range(
            sym, resolution=res, from_ts=from_ts, to_ts=to_ts
        )
    except Exception as e:  # noqa: BLE001
        log.warning("market.history.failed", symbol=sym, error=str(e))
        return {"ok": False, "reason": f"history unavailable: {e!s}"[:120], "candles": []}
    if candles is None:
        # Broker call failed (e.g. cold start / transient) — distinct
        # from an empty range so the chart can retry instead of showing
        # a misleading "no data".
        return {
            "ok": False,
            "reason": "broker history call failed — retrying may help",
            "candles": [],
        }
    return {
        "ok": True,
        "symbol": sym,
        "resolution": res,
        "candles": candles,
        "reason": None,
    }


@router.get("/api/market/indices")
async def market_indices() -> dict[str, Any]:
    """NIFTY 50 / SENSEX / BANK NIFTY last price, change, and %.

    Always 200. Sourced from the connected Fyers account; returns
    `configured: false` when no Fyers account is connected so the UI
    can prompt the operator to connect one.
    """
    return await _fetch_indices()
