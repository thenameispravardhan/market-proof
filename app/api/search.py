"""`/api/search` — symbol search + option chain.

These power the Trade page's autocomplete and option-chain panel.

  GET /api/search/symbols?q=<query>&segment=EQ,FO&limit=20&offset=0
                         &exchange=NSE,NFO&types=EQ,ETF
      -> { hits: [...], count: int, offset: int, has_more: bool, ok: true }

      `exchange` takes "sources" (NSE, BSE, MCX, NFO, BFO, CDS, BCD) and
      `types` takes EQ, ETF, FUT, OPT, IND — see
      `instrument_master.search_filter` for the exact rules. Filters run
      inside the ranked search before the page is cut, so consecutive
      `offset` pages never overlap or skip.

  GET /api/search/option-chain?underlying=NIFTY&expiry=2024-12-26
      -> { underlying, expiries, selected_expiry, spot, strikes: [...] }

The endpoint is read-only and never fails. If the instrument master
is empty (CSV reload pending), the response is `{hits: []}` so the
UI degrades gracefully.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Query

from app.services.instrument_master import get_master, search_filter

router = APIRouter(tags=["search"])


def _csv(value: Optional[str]) -> list[str]:
    """Split a comma-separated query param into trimmed, non-empty parts."""
    return [s.strip() for s in (value or "").split(",") if s.strip()]


@router.post("/api/search/refresh")
async def refresh_instruments() -> dict[str, Any]:
    """Download the full NSE + BSE cash scrip master from Fyers and reload
    the in-process instrument index, so every listed NSE/BSE stock becomes
    searchable (not just the built-in seed). Safe to call repeatedly."""
    from app.services.instrument_master import download_masters

    result = await download_masters()
    return {"ok": True, **result}


@router.get("/api/search/symbols")
def search_symbols(
    q: str = Query("", description="Free-text query (short name, ticker, etc.)"),
    segment: Optional[str] = Query(
        None,
        description="Comma-separated segments to filter on: EQ, FO, COM, CD, INDEX",
    ),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, description="Skip this many ranked hits (paging)"),
    exchange: Optional[str] = Query(
        None,
        description="Comma-separated sources: NSE, BSE, MCX, NFO, BFO, CDS, BCD",
    ),
    types: Optional[str] = Query(
        None,
        description="Comma-separated instrument types: EQ, ETF, FUT, OPT, IND",
    ),
) -> dict[str, Any]:
    segs = _csv(segment) or None
    where = search_filter(sources=_csv(exchange), types=_csv(types))
    master = get_master()
    # One extra hit past the page tells us whether another page exists.
    found = master.search(q, limit=offset + limit + 1, segments=segs, where=where)
    hits = found[offset:offset + limit]
    return {
        "ok": True,
        "count": len(hits),
        "offset": offset,
        "has_more": len(found) > offset + limit,
        "hits": [h.to_dict() for h in hits],
    }


@router.get("/api/search/option-chain")
def option_chain(
    underlying: str = Query(..., min_length=1, max_length=32),
    expiry: Optional[str] = Query(None, max_length=16),
) -> dict[str, Any]:
    master = get_master()
    chain = master.option_chain(underlying, expiry=expiry)
    # Try to attach a spot price from the live market data bus.
    # Index underlyings use the Fyers symbol; for the seed the
    # names are NIFTY, BANKNIFTY, SENSEX.
    spot: Optional[float] = None
    try:
        from app.execution.market_data import MarketDataBus  # type: ignore
        # The bus isn't directly accessible here without the manager;
        # the endpoint just returns the chain. The Trade page polls
        # the Fyers /quote endpoint to get spot.
    except Exception:  # noqa: BLE001
        pass
    chain["spot"] = spot
    chain["ok"] = True
    return chain
