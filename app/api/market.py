"""`/api/market` — read-only market data for the dashboard status bar.

The status bar polls `GET /api/market/indices` every few seconds to
render NIFTY 50 / SENSEX / BANK NIFTY in (near) real time.

Index levels are *public* data, so we source them from Yahoo Finance's
free chart endpoint rather than Fyers. That means the ticker works in
paper mode and before any Fyers OAuth — no token required. (Fyers is
only needed to place orders, not to read an index level.)

  Yahoo symbols:
    ^NSEI     NIFTY 50
    ^BSESN    SENSEX
    ^NSEBANK  NIFTY Bank

The response is cached in-process for CACHE_TTL_S to avoid hammering
upstream. Errors never raise to the caller; the endpoint returns
`{ok: false, reason: ...}` so the UI can render an inline message.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import httpx
from fastapi import APIRouter

from app.logging_config import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["market"])


# key/name shown in the UI + the Yahoo Finance ticker symbol.
INDICES: list[dict[str, str]] = [
    {"key": "NIFTY",     "yahoo": "^NSEI",    "name": "NIFTY 50"},
    {"key": "SENSEX",    "yahoo": "^BSESN",   "name": "SENSEX"},
    {"key": "BANKNIFTY", "yahoo": "^NSEBANK", "name": "BANK NIFTY"},
]

CACHE_TTL_S: float = 5.0
_YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
# Yahoo rejects requests without a browser-like UA.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


_cache: dict[str, Any] = {"data": None, "ts": 0.0, "lock": asyncio.Lock()}


# ---- generic per-symbol quote (used by the Trade page) -------------------

_INDEX_YAHOO = {
    "NSE:NIFTY50-INDEX": "^NSEI",
    "NSE:NIFTYBANK-INDEX": "^NSEBANK",
    "BSE:SENSEX-INDEX": "^BSESN",
    "NSE:NIFTY-INDEX": "^NSEI",
    "NSE:BANKNIFTY-INDEX": "^NSEBANK",
}


def _to_yahoo_symbol(broker_symbol: str) -> Optional[str]:
    """Map a Fyers-style symbol to a Yahoo Finance ticker.

    Equities + indices are public on Yahoo and need no auth:
      NSE:RELIANCE-EQ  -> RELIANCE.NS
      BSE:TCS-EQ       -> TCS.BO
      NSE:NIFTY50-INDEX-> ^NSEI
    F&O / options return None (no public Yahoo symbol — needs Fyers).
    """
    if not broker_symbol:
        return None
    s = broker_symbol.strip().upper()
    if s in _INDEX_YAHOO:
        return _INDEX_YAHOO[s]
    if "-INDEX" in s:
        return _INDEX_YAHOO.get(s)
    exch, _, rest = s.partition(":")
    rest = rest or exch
    # Only plain cash equities map cleanly (suffix -EQ / -BE / -A ...).
    base, _, suffix = rest.rpartition("-")
    base = base or rest
    if suffix not in ("EQ", "BE", "A", "B", "") :
        return None  # likely a derivative
    if exch == "BSE":
        return f"{base}.BO"
    return f"{base}.NS"


async def fetch_quote(broker_symbol: str) -> Optional[dict[str, Any]]:
    """Live last-price for any equity/index symbol via Yahoo. Returns
    None for symbols Yahoo can't serve (F&O) or on failure."""
    ysym = _to_yahoo_symbol(broker_symbol)
    if not ysym:
        return None
    try:
        async with httpx.AsyncClient(http2=False) as client:
            r = await client.get(
                _YAHOO_CHART.format(symbol=ysym),
                params={"interval": "1d", "range": "1d"},
                headers=_HEADERS,
                timeout=8.0,
            )
            r.raise_for_status()
            meta = (((r.json() or {}).get("chart") or {}).get("result") or [{}])[0].get("meta") or {}
    except Exception as e:  # noqa: BLE001
        log.debug("market.quote_fetch_failed", symbol=broker_symbol, yahoo=ysym, error=str(e))
        return None
    price = meta.get("regularMarketPrice")
    if price is None:
        return None
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    price = float(price)
    out: dict[str, Any] = {
        "last_price": price,
        "prev_close": float(prev) if prev else None,
        "high": meta.get("regularMarketDayHigh"),
        "low": meta.get("regularMarketDayLow"),
        "volume": meta.get("regularMarketVolume"),
        "change": round(price - float(prev), 2) if prev else None,
        "change_pct": round((price - float(prev)) / float(prev) * 100.0, 2) if prev else None,
    }
    return out


async def _fetch_one(client: httpx.AsyncClient, row: dict[str, str]) -> dict[str, Any]:
    out = {
        "key": row["key"],
        "name": row["name"],
        "symbol": row["yahoo"],
        "last_price": None,
        "change": None,
        "change_pct": None,
    }
    try:
        r = await client.get(
            _YAHOO_CHART.format(symbol=row["yahoo"]),
            params={"interval": "1d", "range": "1d"},
            headers=_HEADERS,
            timeout=8.0,
        )
        r.raise_for_status()
        meta = (((r.json() or {}).get("chart") or {}).get("result") or [{}])[0].get("meta") or {}
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        if price is not None and prev:
            price = float(price)
            prev = float(prev)
            out["last_price"] = price
            out["change"] = round(price - prev, 2)
            out["change_pct"] = round((price - prev) / prev * 100.0, 2) if prev else 0.0
        elif price is not None:
            out["last_price"] = float(price)
            out["change"] = 0.0
            out["change_pct"] = 0.0
    except Exception as e:  # noqa: BLE001
        log.debug("market.index_fetch_failed", symbol=row["yahoo"], error=str(e))
    return out


async def _fetch_indices() -> dict[str, Any]:
    async with _cache["lock"]:
        now = time.monotonic()
        if _cache["data"] is not None and (now - _cache["ts"]) < CACHE_TTL_S:
            return _cache["data"]

        try:
            async with httpx.AsyncClient(http2=False) as client:
                results = await asyncio.gather(
                    *[_fetch_one(client, row) for row in INDICES]
                )
        except Exception as e:  # noqa: BLE001
            log.warning("market.indices.fetch_failed", error=str(e))
            payload = {
                "ok": False,
                "configured": True,
                "reason": f"market data unavailable: {e!s}"[:120],
                "indices": [],
                "fetched_at": None,
            }
            _cache["data"] = payload
            _cache["ts"] = now
            return payload

        got_any = any(r["last_price"] is not None for r in results)
        payload = {
            "ok": got_any,
            "configured": True,
            "reason": None if got_any else "no index data returned upstream",
            "indices": results,
            "fetched_at": time.time(),
        }
        _cache["data"] = payload
        _cache["ts"] = now
        return payload


@router.get("/api/market/indices")
async def market_indices() -> dict[str, Any]:
    """NIFTY 50 / SENSEX / BANK NIFTY last price, change, and %.

    Always 200. Sourced from public data, so it works without Fyers.
    """
    return await _fetch_indices()
