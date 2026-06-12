"""`/api/webhooks` — CRUD for registered webhooks (plugin /
extension fan-out) + the inbound endpoint
`/api/webhooks/in/{webhook_id}`.

`webhooks.direction` is `in` (the receiver endpoint targets a
specific row) or `out` (the dispatcher subscribes to the event
bus and POSTs to `url` on matching events).
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.init import init_db
from app.db.models import Webhook
from app.db.session import get_db
from app.logging_config import get_logger
from app.webhooks.receiver import InboundError, process_inbound

log = get_logger(__name__)

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

init_db()

VALID_DIRECTIONS = {"in", "out"}


def _mask_webhook(w: Webhook) -> dict[str, Any]:
    out = {
        "id": w.id,
        "name": w.name,
        "direction": w.direction,
        "event_filter": w.event_filter,
        "url": w.url,
        "enabled": w.enabled,
        "created_at": w.created_at.isoformat() if w.created_at else None,
    }
    if w.secret:
        out["secret"] = "***"
    else:
        out["secret"] = None
    return out


# -------------------------------------------------------------------------
# Pydantic schemas
# -------------------------------------------------------------------------


class WebhookCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    direction: str = Field(...)
    event_filter: Optional[str] = Field(default="*")
    url: str = Field(...)
    secret: Optional[str] = None
    enabled: bool = True

    @field_validator("direction")
    @classmethod
    def _dir_check(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in VALID_DIRECTIONS:
            raise ValueError(f"direction must be one of {sorted(VALID_DIRECTIONS)}")
        return v


class WebhookUpdate(BaseModel):
    name: Optional[str] = None
    event_filter: Optional[str] = None
    url: Optional[str] = None
    secret: Optional[str] = None
    enabled: Optional[bool] = None


# -------------------------------------------------------------------------
# CRUD
# -------------------------------------------------------------------------


@router.get("")
def list_webhooks(db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.execute(select(Webhook).order_by(Webhook.id.asc())).scalars().all()
    return {"webhooks": [_mask_webhook(w) for w in rows]}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_webhook(body: WebhookCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    w = Webhook(
        name=body.name,
        direction=body.direction,
        event_filter=body.event_filter or "*",
        url=body.url,
        secret=body.secret,
        enabled=body.enabled,
    )
    db.add(w)
    db.commit()
    db.refresh(w)
    return _mask_webhook(w)


@router.put("/{webhook_id}")
def update_webhook(
    webhook_id: int,
    body: WebhookUpdate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    w = db.get(Webhook, webhook_id)
    if w is None:
        raise HTTPException(404, detail=f"webhook {webhook_id} not found")
    if body.name is not None:
        w.name = body.name
    if body.event_filter is not None:
        w.event_filter = body.event_filter
    if body.url is not None:
        w.url = body.url
    if body.secret is not None:
        # empty string clears the secret.
        w.secret = body.secret or None
    if body.enabled is not None:
        w.enabled = body.enabled
    db.commit()
    db.refresh(w)
    return _mask_webhook(w)


@router.delete("/{webhook_id}")
def delete_webhook(webhook_id: int, db: Session = Depends(get_db)) -> Response:
    w = db.get(Webhook, webhook_id)
    if w is None:
        raise HTTPException(404, detail=f"webhook {webhook_id} not found")
    db.delete(w)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{webhook_id}/deliveries")
def list_deliveries(
    webhook_id: int,
    limit: int = 50,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return recent webhook_deliveries for a webhook."""
    from sqlalchemy import select as _select
    from app.db.models import WebhookDelivery

    w = db.get(Webhook, webhook_id)
    if w is None:
        raise HTTPException(404, detail=f"webhook {webhook_id} not found")
    rows = db.execute(
        _select(WebhookDelivery)
        .where(WebhookDelivery.webhook_id == webhook_id)
        .order_by(WebhookDelivery.attempted_at.desc())
        .limit(limit)
    ).scalars().all()
    return {
        "webhook_id": webhook_id,
        "count": len(rows),
        "deliveries": [
            {
                "id": d.id,
                "direction": d.direction,
                "event_type": d.event_type,
                "payload": d.payload,
                "status_code": d.status_code,
                "response_body": d.response_body,
                "error": d.error,
                "attempted_at": d.attempted_at.isoformat() if d.attempted_at else None,
            }
            for d in rows
        ],
    }


# -------------------------------------------------------------------------
# Inbound
# -------------------------------------------------------------------------


@router.post("/in/{webhook_id}")
async def inbound_webhook(
    webhook_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Accept a signed JSON payload, create an `analyses` + `signals`
    row, and publish on `signals.new` so T4's risk engine picks it
    up.

    Error mapping:
      - 401: bad signature (webhook has secret + sig header is wrong)
      - 400: invalid json, missing fields, stale timestamp
      - 404: webhook_id not found or not direction='in'
      - 503: webhook is disabled
    """
    w = db.get(Webhook, webhook_id)
    if w is None:
        raise HTTPException(404, detail=f"webhook {webhook_id} not found")
    if w.direction != "in":
        raise HTTPException(
            400,
            detail=f"webhook {webhook_id} is not configured for inbound (direction={w.direction!r})",
        )
    if not w.enabled:
        raise HTTPException(503, detail=f"webhook {webhook_id} is disabled")

    body = await request.body()
    # Normalise headers to a plain dict (case-insensitive lookup).
    headers = {k: v for k, v in request.headers.items()}
    try:
        result = process_inbound(db, w, body, headers)
    except InboundError as e:
        raise HTTPException(e.status_code, detail=str(e)) from e
    return result
