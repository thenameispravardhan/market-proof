"""GET /health — liveness + dependency status."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import __version__
from app.config import Settings, get_settings
from app.db.session import get_db

router = APIRouter(tags=["health"])


@router.get("/health")
def health(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    db_ok = "ok"
    try:
        db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 — health is best-effort
        db_ok = "down"
    return {
        "status": "ok" if db_ok == "ok" else "degraded",
        "db": db_ok,
        "deepseek_configured": settings.deepseek_configured,
        "fyers_configured": settings.fyers_configured,
        "trading_mode": settings.TRADING_MODE,
        "version": __version__,
    }
