"""`/api/search` — symbol search + option chain.

These power the Trade page's autocomplete and option-chain panel.

  GET /api/search/symbols?q=<query>&segment=EQ,FO&limit=20
      -> { hits: [...], count: int, ok: true }

  GET /api/search/option-chain?underlying=NIFTY&expiry=2024-12-26
      -> { underlying, expiries, selected_expiry, spot, strikes: [...] }

The endpoint is read-only and never fails. If the instrument master
is empty (CSV reload pending), the response is `{hits: []}` so the
UI degrades gracefully.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Query

from app.services.instrument_master import get_master

router = APIRouter(tags=["search"])


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
) -> dict[str, Any]:
    segs = [s.strip() for s in (segment or "").split(",") if s.strip()] or None
    master = get_master()
    hits = master.search(q, limit=limit, segments=segs)
    return {
        "ok": True,
        "count": len(hits),
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
