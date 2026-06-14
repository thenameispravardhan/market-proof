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

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import BrokerAccount
from app.db.session import get_db
from app.execution.fyers_live import FyersAPIError, FyersBlockedError
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
    """First enabled, real (non-paper) Fyers account holding a token."""
    return (
        db.execute(
            select(BrokerAccount)
            .where(
                BrokerAccount.broker == "fyers",
                BrokerAccount.paper_mode == False,  # noqa: E712
                BrokerAccount.enabled == True,  # noqa: E712
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
    return None


def _leg_from_master(leg: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
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
        "lot_size": leg.get("lot_size", 1),
        "tick_size": leg.get("tick_size", 0.05),
    }


def _master_chain(underlying: str, expiry: Optional[str], reason: str) -> dict[str, Any]:
    """Static fallback from the instrument master — no live prices. Shaped
    identically to the live response so the UI doesn't branch."""
    chain = get_master().option_chain(underlying, expiry=expiry)
    strikes = [
        {
            "strike": s["strike"],
            "ce": _leg_from_master(s.get("ce")),
            "pe": _leg_from_master(s.get("pe")),
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
        chain = await backend.get_option_chain(
            idx_symbol, strikecount=strikecount, timestamp=expiry or ""
        )
    except (FyersAPIError, FyersBlockedError) as e:
        log.warning("options.chain.fyers_failed", symbol=idx_symbol, error=str(e))
        return _master_chain(underlying, expiry, f"Fyers option chain unavailable: {e}")
    except Exception:  # noqa: BLE001
        log.exception("options.chain.unexpected", symbol=idx_symbol)
        return _master_chain(underlying, expiry, "Option chain fetch failed; showing static list.")

    expiries = chain.get("expiries", [])
    selected = expiry or (expiries[0]["ts"] if expiries else None)
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
