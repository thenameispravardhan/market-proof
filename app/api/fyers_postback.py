"""Fyers order postback (webhook) — a second channel for order updates.

Fyers POSTs order updates (pending / traded / rejected / cancelled) to the
webhook URL set in the API dashboard. They go through the same reconciler
as the order WebSocket (match by broker_order_id, never create a signal,
deduped), so the two channels converge on the same row.

Auth: Fyers does not send the dashboard's "Secret" (their support: it
"is currently not having a use"), so the shared secret rides in the URL as
?token=<FYERS_POSTBACK_SECRET>. No secret configured = endpoint closed.
Caddy exempts only the exact path /api/fyers/postback from basic auth;
/api/fyers/postback/url stays behind it.

  POST /api/fyers/postback?token=..   the receiver Fyers calls
  GET  /api/fyers/postback/url        the URL to paste into the dashboard
"""
from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import get_db
from app.execution.order_reconcile import reconcile_order_update

router = APIRouter(prefix="/api/fyers", tags=["fyers"])

PATH = "/api/fyers/postback"


def _secret() -> str:
    return (get_settings().FYERS_POSTBACK_SECRET or "").strip()


@router.post("/postback")
async def fyers_postback(request: Request, token: str = Query(default=""), db: Session = Depends(get_db)) -> dict[str, Any]:
    secret = _secret()
    if not secret:
        raise HTTPException(status_code=503, detail="postback not configured (set FYERS_POSTBACK_SECRET)")
    if not secrets.compare_digest(token.encode(), secret.encode()):
        raise HTTPException(status_code=401, detail="bad token")
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="invalid json")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    return await reconcile_order_update(db, payload, source="fyers_postback")


@router.get("/postback/url")
def postback_url(request: Request) -> dict[str, Any]:
    secret = _secret()
    host = request.headers.get("host", "localhost:8000")
    scheme = "http" if host.startswith(("localhost", "127.0.0.1")) else "https"
    return {
        "configured": bool(secret),
        "url": f"{scheme}://{host}{PATH}?token={secret}" if secret else None,
        "dashboard_secret": "Any value without spaces — Fyers does not use it yet.",
        "order_updates": ["Pending", "Rejected", "Cancelled", "Traded"],
    }
