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
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api import market
from app.db.models import Trade
from app.db import session as db_session
from app.db.session import get_db
from app.execution.order_reconcile import _unwrap_fyers, reconcile_order_update, spawn, wait_for_trade
from app.services.event_bus import event_bus

router = APIRouter(prefix="/api/fyers", tags=["fyers"])

# Flood guards. The route is unauthenticated by design (Fyers sends no
# credentials), so a script replaying one of our order ids could otherwise
# turn every POST into a Fyers REST read and burn the API budget that live
# orders need. Updates for one order inside the cooldown collapse into a
# single deferred re-read (a real pending -> traded pair inside 2s still
# ends with the final state), and background waits for not-yet-committed
# ids are capped.
COOLDOWN_S = 2.0
MAX_BACKGROUND_WAITS = 50
_last_sync: dict[str, float] = {}
_deferred: set[str] = set()
_waiting: set[str] = set()


def _reset_guards() -> None:
    """Tests only."""
    _last_sync.clear()
    _deferred.clear()
    _waiting.clear()


async def _sync_throttled(order_id: str) -> dict[str, Any]:
    now = time.monotonic()
    last = _last_sync.get(order_id)
    if last is not None and now - last < COOLDOWN_S:
        if order_id not in _deferred:
            _deferred.add(order_id)
            spawn(_deferred_sync(order_id, COOLDOWN_S - (now - last)))
        return {"ok": True, "matched": True, "throttled": True, "order_id": order_id}
    _last_sync[order_id] = now
    if len(_last_sync) > 5000:              # bounded memory under a flood of ids
        cutoff = now - COOLDOWN_S
        for k in [k for k, t in _last_sync.items() if t < cutoff]:
            _last_sync.pop(k, None)
    return await _sync_from_fyers(order_id)


async def _deferred_sync(order_id: str, delay: float) -> None:
    try:
        await asyncio.sleep(max(0.0, delay))
        _last_sync[order_id] = time.monotonic()
        await _sync_from_fyers(order_id)
    finally:
        _deferred.discard(order_id)


async def _sync_from_fyers(order_id: str) -> dict[str, Any]:
    """Reconcile one of OUR orders from what Fyers reports (not the payload)."""
    backend = market._fyers_backend()  # noqa: SLF001
    if backend is None or not hasattr(backend, "get_order_status"):
        return {"ok": False, "reason": "Fyers not connected", "order_id": order_id}
    st = await backend.get_order_status(order_id)
    truth = st.raw if isinstance(st.raw, dict) and str(st.raw.get("id") or "") == order_id else None
    if truth is None:
        return {"ok": False, "reason": st.error or "order not found at Fyers", "order_id": order_id}
    with db_session.SessionLocal() as db:
        return await reconcile_order_update(db, truth, source="fyers_postback")


async def _sync_when_row_lands(order_id: str) -> None:
    try:
        if await wait_for_trade(order_id):
            await _sync_throttled(order_id)
    finally:
        _waiting.discard(order_id)


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
    await event_bus.publish("broker", {"order_id": order_id, "source": "fyers_postback"})
    row = db.query(Trade.status).filter(Trade.broker_order_id == order_id).first()
    if row is not None:
        if row[0] in ("filled", "rejected", "cancelled"):
            # The order WebSocket already settled it: no extra Fyers call.
            return {"ok": True, "matched": True, "deduped": True, "order_id": order_id, "status": row[0]}
        return await _sync_throttled(order_id)
    # Not ours (Fyers app / web order) or Fyers beat our own commit: answer
    # Fyers NOW — it re-sends a webhook that takes ~2s — and look again in
    # the background. Unknown ids still never cost a Fyers call.
    if order_id in _waiting or len(_waiting) >= MAX_BACKGROUND_WAITS:
        return {"ok": True, "matched": False, "order_id": order_id, "throttled": True}
    _waiting.add(order_id)
    spawn(_sync_when_row_lands(order_id))
    return {"ok": True, "matched": False, "order_id": order_id}
