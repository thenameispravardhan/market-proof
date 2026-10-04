"""Fyers order postback (webhook) — a second channel for order updates.

Fyers POSTs order updates (pending / traded / rejected / cancelled) to the
webhook URL set in the API dashboard:

    https://<domain>/api/fyers/postback

The payload is treated as a NUDGE, never as truth: we take the order id,
and only if it is one of OUR orders (a trades row with that
broker_order_id) do we fetch the order's real state from the Fyers REST
API and reconcile from that — the same deduped path as the order
WebSocket, which never creates a signal. So the URL needs no secret
(Fyers doesn't send the dashboard "Secret" anyway): a forged POST can at
most make us re-read one of our own orders from Fyers.

Caddy exempts only the exact path /api/fyers/postback from basic auth.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api import market
from app.db.models import Trade
from app.db.session import get_db
from app.execution.order_reconcile import UNMATCHED_RETRY_S, _unwrap_fyers, reconcile_order_update

router = APIRouter(prefix="/api/fyers", tags=["fyers"])


@router.post("/postback")
async def fyers_postback(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="invalid json")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    order_id = str(_unwrap_fyers(payload).get("id") or "").strip()
    if not order_id:
        return {"ok": False, "reason": "no order id"}
    # Only our own orders cost a Fyers call; manual / unknown ids stop here.
    # Fyers can call before the placing request has committed our row, so
    # an unknown id gets one more look after a short wait.
    mine = lambda: db.query(Trade.id).filter(Trade.broker_order_id == order_id).first() is not None  # noqa: E731
    if not mine():
        await asyncio.sleep(UNMATCHED_RETRY_S)
        db.rollback()  # end the read snapshot so the re-check sees a just-committed row
        if not mine():
            return {"ok": True, "matched": False, "order_id": order_id}
    backend = market._fyers_backend()  # noqa: SLF001
    if backend is None or not hasattr(backend, "get_order_status"):
        return {"ok": False, "reason": "Fyers not connected", "order_id": order_id}
    # ponytail: one REST read per update of our own orders; add a per-id cooldown if Fyers ever floods
    st = await backend.get_order_status(order_id)
    truth = st.raw if isinstance(st.raw, dict) and str(st.raw.get("id") or "") == order_id else None
    if truth is None:
        return {"ok": False, "reason": st.error or "order not found at Fyers", "order_id": order_id}
    return await reconcile_order_update(db, truth, source="fyers_postback")
