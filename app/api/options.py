"""`/api/options/chain` — option chain for the Trade page.

When a real Fyers account is connected, this serves the LIVE chain from
Fyers' `/data/options-chain-v3` endpoint: per-strike CE/PE with last
price, bid/ask, and open interest, plus the expiry list. When Fyers
isn't connected (or the call fails), it falls back to the static
instrument master so the panel still renders a strike ladder (without
live prices). The response shape is identical either way; `source` tells
the UI which path produced it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import BrokerAccount
from app.db.session import get_db
from app.execution.fyers_live import (
    FyersAPIError,
    FyersAuthError,
    FyersBlockedError,
    _guess_lot_size,
    _leg_lot,
)
from app.logging_config import get_logger
from app.services.instrument_master import get_master

log = get_logger(__name__)

router = APIRouter(prefix="/api/options", tags=["options"])


# Map a human underlying (index short name) to the Fyers index symbol the
# option-chain endpoint expects.
_UNDERLYING_TO_SYMBOL: dict[str, str] = {
    "NIFTY": "NSE:NIFTY50-INDEX",
    "NIFTY50": "NSE:NIFTY50-INDEX",
    "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
    "NIFTYBANK": "NSE:NIFTYBANK-INDEX",
    "FINNIFTY": "NSE:FINNIFTY-INDEX",
    "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
    "NIFTYNXT50": "NSE:NIFTYNXT50-INDEX",
    "SENSEX": "BSE:SENSEX-INDEX",
    "BANKEX": "BSE:BANKEX-INDEX",
}


def _manager():
    """The shared ExecutionManager (same singleton orders.py uses)."""
    from app.main import app

    mgr = getattr(app.state, "execution_manager", None)
    if mgr is None:  # tests sometimes run without the lifespan
        from app.execution.manager import Manager
        from app.execution.market_data import MarketDataBus
        from app.risk.engine import RiskEngine

        return Manager(market_data=MarketDataBus(), risk_engine=RiskEngine())
    return mgr


def _connected_fyers_account(db: Session) -> Optional[BrokerAccount]:
    """First real (non-paper) Fyers account holding an OAuth token.

    Market-data path (option chain): keyed on "connected" (token present),
    NOT the `enabled` trading switch — turning Fyers trading off must not
    blank the chain. Order placement is gated separately."""
    return (
        db.execute(
            select(BrokerAccount)
            .where(
                BrokerAccount.broker == "fyers",
                BrokerAccount.paper_mode == False,  # noqa: E712
                BrokerAccount.access_token.is_not(None),
            )
            .order_by(BrokerAccount.id.asc())
        )
        .scalars()
        .first()
    )


def _resolve_symbol(underlying: str, symbol: Optional[str]) -> Optional[str]:
    if symbol and symbol.strip():
        return symbol.strip().upper()
    u = (underlying or "").strip().upper()
    if u in _UNDERLYING_TO_SYMBOL:
        return _UNDERLYING_TO_SYMBOL[u]
    if u.endswith("-INDEX") or ":" in u:
        return u
    # A stock (or an index the master knows) by its short name: Fyers'
    # chain takes the cash symbol, e.g. RELIANCE -> NSE:RELIANCE-EQ.
    for inst in get_master().search(u, limit=10):
        if inst.short_name == u and inst.instrument_type in ("EQ", "IND"):
            return inst.symbol
    return None


def _is_epoch(expiry: Optional[str]) -> bool:
    return bool(expiry) and expiry.strip().isdigit()  # type: ignore[union-attr]


def _master_expiry(expiry: Optional[str]) -> Optional[str]:
    """The master keys expiries by `YYYY-MM-DD`; the live chain by epoch.
    Translate a live epoch so a fallback keeps the expiry the user picked."""
    if not _is_epoch(expiry):
        return expiry
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.fromtimestamp(int(expiry), ist).strftime("%Y-%m-%d")  # type: ignore[arg-type]


def _ist_today() -> str:
    return datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d")


def _master_lot(symbol: str, underlying_lot: Optional[int], static_lot: Any) -> Optional[int]:
    """A static-ladder leg's lot, looked up the way the live chain does (the
    F&O scrip master's contract lot, else the underlying's), then the
    instrument master's when it's a real lot. Never a default of 1: the
    ticket and the scalper would send a 1-unit F&O order, which the exchange
    rejects; None makes them say the lot is unknown instead."""
    lot = _leg_lot(symbol, underlying_lot)
    if lot:
        return int(lot)
    try:
        static = int(static_lot or 0)
    except (TypeError, ValueError):
        static = 0
    return static if static > 1 else None


def _leg_from_master(leg: Optional[dict[str, Any]], underlying_lot: Optional[int]) -> Optional[dict[str, Any]]:
    if not leg:
        return None
    return {
        "symbol": leg.get("symbol", ""),
        "ltp": None,
        "bid": None,
        "ask": None,
        "oi": None,
        "volume": None,
        "ltpch": None,
        "lot_size": _master_lot(leg.get("symbol", ""), underlying_lot, leg.get("lot_size")),
        "tick_size": leg.get("tick_size") or 0.05,
    }


def _master_chain(underlying: str, expiry: Optional[str], reason: str) -> dict[str, Any]:
    """Static fallback from the instrument master — no live prices. Shaped
    identically to the live response so the UI doesn't branch."""
    master = get_master()
    chain = master.option_chain(underlying, expiry=_master_expiry(expiry))
    # A master file a few days old still lists expiries that have lapsed, and
    # picked the oldest as "nearest": a ladder of dead contracts. Drop them
    # and land on the first live expiry.
    today = _ist_today()
    live = [e for e in chain.get("expiries", []) if e >= today]
    if live and chain.get("selected_expiry") not in live:
        chain = master.option_chain(underlying, expiry=live[0])
    elif not live:
        chain = {**chain, "selected_expiry": None, "strikes": []}
    chain["expiries"] = live
    underlying_lot = _guess_lot_size(underlying)
    strikes = [
        {
            "strike": s["strike"],
            "ce": _leg_from_master(s.get("ce"), underlying_lot),
            "pe": _leg_from_master(s.get("pe"), underlying_lot),
        }
        for s in chain.get("strikes", [])
    ]
    return {
        "ok": True,
        "underlying": underlying,
        "symbol": "",
        "spot": chain.get("spot"),
        "expiries": [{"label": e, "ts": e} for e in chain.get("expiries", [])],
        "selected_expiry": chain.get("selected_expiry"),
        "strikes": strikes,
        "source": "master",
        "reason": reason,
    }


async def _lots_for(symbols: list[str]) -> dict[str, Optional[int]]:
    """Lot per symbol: 1 for cash, the contract's lot for NSE/BSE F&O (Fyers
    F&O scrip master), None when it isn't known — the order endpoint refuses
    such an order rather than guess. Fetches the master once if it's missing."""
    from app.algo import fno

    if any(fno.is_derivative(s) for s in symbols) and not fno.master_loaded():
        try:
            await fno.ensure_master()
        except Exception as e:  # noqa: BLE001
            log.warning("options.lot_master_refresh_failed", error=str(e)[:200])
    return {s: (fno.contract_lot(s) if fno.is_derivative(s) else 1) for s in symbols}


@router.get("/lot")
async def lot_size(
    symbol: str = Query(..., min_length=1, description="Fyers symbol, e.g. NSE:SBIN25OCTFUT"),
) -> dict[str, Any]:
    """Lot size for one symbol (`lot_size: null` when it isn't known)."""
    from app.algo import fno

    sym = symbol.strip().upper()
    lot = (await _lots_for([sym]))[sym]
    return {"ok": True, "symbol": sym, "lot_size": lot, "derivative": fno.is_derivative(sym),
            "underlying": fno.underlying_of(sym)}


@router.get("/lots")
async def lot_sizes(
    symbols: str = Query(..., min_length=1, description="Comma-separated Fyers symbols (max 200)"),
) -> dict[str, Any]:
    """Current lot sizes for many symbols at once — the Trade page refreshes
    saved watchlist entries with it, since a lot stored when an option was
    added goes stale when NSE revises lots."""
    syms = list(dict.fromkeys(s.strip().upper() for s in symbols.split(",") if s.strip()))[:200]
    return {"ok": True, "lots": await _lots_for(syms)}


@router.get("/chain")
async def options_chain(
    underlying: str = Query("", description="Index short name, e.g. NIFTY / BANKNIFTY"),
    symbol: Optional[str] = Query(
        None, description="Explicit Fyers index symbol, e.g. NSE:NIFTY50-INDEX"
    ),
    strikecount: int = Query(10, ge=1, le=50, description="Strikes on each side of ATM"),
    expiry: Optional[str] = Query(
        None, description="Expiry epoch from a prior response's expiries[].ts"
    ),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Live option chain (Fyers) with a static-master fallback."""
    idx_symbol = _resolve_symbol(underlying, symbol)
    if not idx_symbol:
        return _master_chain(
            underlying, expiry, "Unknown underlying — pick a known index (NIFTY, BANKNIFTY, …)."
        )

    acc = _connected_fyers_account(db)
    if acc is None:
        return _master_chain(
            underlying, expiry, "Connect a Fyers account for the live option chain."
        )

    backend = _manager()._manual_backend_for(acc)  # noqa: SLF001
    if backend is None or not hasattr(backend, "get_option_chain"):
        return _master_chain(underlying, expiry, "Fyers backend unavailable.")

    try:
        # Only an epoch is a Fyers expiry. A `YYYY-MM-DD` left over from a
        # master fallback made Fyers reject every poll, so the chain stayed
        # static after the account reconnected.
        chain = await backend.get_option_chain(
            idx_symbol, strikecount=strikecount, timestamp=expiry if _is_epoch(expiry) else ""
        )
    except FyersAuthError as e:
        # Daily token expiry is the common case — give an actionable hint.
        log.warning("options.chain.token_expired", symbol=idx_symbol, error=str(e))
        return _master_chain(
            underlying,
            expiry,
            "Fyers token expired — open Accounts and click Connect Fyers to "
            "re-authorise (Fyers tokens expire daily).",
        )
    except (FyersAPIError, FyersBlockedError) as e:
        log.warning("options.chain.fyers_failed", symbol=idx_symbol, error=str(e))
        return _master_chain(underlying, expiry, f"Fyers option chain unavailable: {e}")
    except Exception:  # noqa: BLE001
        log.exception("options.chain.unexpected", symbol=idx_symbol)
        return _master_chain(underlying, expiry, "Option chain fetch failed; showing static list.")

    expiries = chain.get("expiries", [])
    listed = [e["ts"] for e in expiries]
    selected = expiry if expiry in listed else (listed[0] if listed else None)
    chain.update(
        {
            "ok": True,
            "underlying": underlying or idx_symbol,
            "selected_expiry": selected,
            "source": "fyers",
            "reason": None,
        }
    )
    return chain
