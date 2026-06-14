"""GET /health — liveness + dependency status.

Also exposes `GET /api/version` for dashboard display.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import __version__
from app.config import Settings, get_settings
from app.db.session import get_db
from app.logging_config import get_logger

router = APIRouter(tags=["health"])
log = get_logger(__name__)


@router.get("/api/version")
def version() -> dict[str, str]:
    """Return the app version. Useful for dashboard display and CI checks."""
    return {"version": __version__}


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


# Public-IP lookup — used by the Trade page to surface the
# server's outbound IP in the place-order error banner when
# Fyers rejects with "Algo orders are not allowed from this
# app" (which, per Fyers support docs, almost always means
# the server's IP isn't whitelisted on the Fyers app's
# dashboard). The IP is cached for 5 minutes so we don't
# hammer ipify on every health check.
_public_ip_cache: dict[str, object] = {"ip": None, "fetched_at": 0.0}
_PUBLIC_IP_TTL_S = 300.0


async def _lookup_public_ip() -> str | None:
    """Best-effort outbound-IP lookup. We hit api.ipify.org (no
    auth, returns the calling IP as plain text) and cache the
    result. Falsy / network errors fall through to None — the
    UI then hides the IP hint instead of showing a broken
    state. We use a plain httpx get rather than a third-party
    SDK to keep this dependency-free.
    """
    import asyncio
    import time

    now = time.monotonic()
    if _public_ip_cache["ip"] and (now - float(_public_ip_cache["fetched_at"])) < _PUBLIC_IP_TTL_S:
        return str(_public_ip_cache["ip"])
    try:
        import httpx

        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get("https://api.ipify.org")
        ip = (r.text or "").strip()
        if ip:
            _public_ip_cache["ip"] = ip
            _public_ip_cache["fetched_at"] = now
            return ip
    except Exception as e:  # noqa: BLE001
        log.info("health.public_ip_lookup_failed", error=str(e))
    return None


@router.get("/api/server-info")
async def server_info() -> dict[str, object]:
    """Server identity info for the operator UI.

    Surfaces the bot's outbound public IP. The Trade page
    displays this verbatim when a place-order fails with
    Fyers' "Algo orders are not allowed from this app" error,
    so the operator can copy the IP into the Fyers app's
    IP-whitelist on https://myapi.fyers.in/dashboard/
    without having to SSH into the box to find it.

    The lookup is best-effort: if api.ipify.org is down
    or the server has no outbound HTTPS, `public_ip` is
    `null` and the UI hides the hint rather than show a
    broken state.
    """
    public_ip = await _lookup_public_ip()
    return {
        "public_ip": public_ip,
        "ip_source": "api.ipify.org (cached 5 min)",
    }
