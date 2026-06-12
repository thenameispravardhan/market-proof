"""`/api/notifications/channels` — CRUD + test endpoint for the
operator-curated notification channels (Telegram, Discord, email,
generic webhook).

Channel kinds and their `config`:
  - telegram: {bot_token, chat_id}
  - discord:  {webhook_url}
  - email:    {smtp_host, smtp_port, username, password,
               from_addr, to_addrs[]}
  - webhook:  {url, secret?}

`events_filter` is a CSV of canonical event types
(signal, trade, risk_halt, error) or '*'.

Secrets in `config` are masked in every response (defence in
depth, in addition to the structlog redactor and the
`redact_config` helper in `app.notifications.base`).
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.init import init_db
from app.db.models import NotificationChannel
from app.db.session import get_db
from app.logging_config import get_logger
from app.notifications.base import NotificationResult, redact_config
from app.notifications.discord import DiscordNotifier
from app.notifications.email import EmailNotifier
from app.notifications.manager import send_test_message
from app.notifications.telegram import TelegramNotifier
from app.notifications.webhook import WebhookNotifier

log = get_logger(__name__)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

# Make sure the table exists.
init_db()

VALID_KINDS = {"telegram", "discord", "email", "webhook"}
# Per-kind config keys we ALWAYS mask in responses.
_MASKED_KEYS: dict[str, frozenset[str]] = {
    "telegram": frozenset({"bot_token"}),
    "discord": frozenset(),  # webhook_url is not a secret
    "email": frozenset({"password"}),
    "webhook": frozenset({"secret"}),
}


def _mask_config(kind: str, config: dict[str, Any]) -> dict[str, Any]:
    masked = redact_config(config or {})
    keys = _MASKED_KEYS.get(kind, frozenset())
    for k in keys:
        if k in masked and masked[k] not in (None, ""):
            masked[k] = "***"
    return masked


# -------------------------------------------------------------------------
# Pydantic schemas
# -------------------------------------------------------------------------


class ChannelCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    kind: str = Field(...)
    config: dict[str, Any] = Field(default_factory=dict)
    events_filter: Optional[str] = Field(default="*")
    enabled: bool = True

    @field_validator("kind")
    @classmethod
    def _kind_check(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in VALID_KINDS:
            raise ValueError(f"kind must be one of {sorted(VALID_KINDS)}")
        return v

    @field_validator("events_filter")
    @classmethod
    def _events_check(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return "*"
        v = v.strip()
        if not v:
            return "*"
        return v


class ChannelUpdate(BaseModel):
    name: Optional[str] = None
    config: Optional[dict[str, Any]] = None
    events_filter: Optional[str] = None
    enabled: Optional[bool] = None


def _serialize(c: NotificationChannel) -> dict[str, Any]:
    return {
        "id": c.id,
        "name": c.name,
        "kind": c.kind,
        "config": _mask_config(c.kind, c.config or {}),
        "events_filter": c.events_filter,
        "enabled": c.enabled,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def _validate_config_for_kind(kind: str, config: dict[str, Any]) -> None:
    """Per-kind required fields. Empty / missing → 422."""
    if kind == "telegram":
        if not (config.get("bot_token") or "").strip():
            raise HTTPException(422, detail="telegram: bot_token is required")
        if not (config.get("chat_id") or "").strip():
            raise HTTPException(422, detail="telegram: chat_id is required")
    elif kind == "discord":
        if not (config.get("webhook_url") or "").strip():
            raise HTTPException(422, detail="discord: webhook_url is required")
    elif kind == "email":
        for k in ("smtp_host", "from_addr"):
            if not (config.get(k) or "").strip():
                raise HTTPException(422, detail=f"email: {k} is required")
        if not config.get("to_addrs"):
            raise HTTPException(422, detail="email: to_addrs must be a non-empty list")
    elif kind == "webhook":
        if not (config.get("url") or "").strip():
            raise HTTPException(422, detail="webhook: url is required")


# -------------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------------


@router.get("/channels")
def list_channels(db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.execute(select(NotificationChannel).order_by(NotificationChannel.id.asc())).scalars().all()
    return {"channels": [_serialize(c) for c in rows]}


@router.post("/channels", status_code=status.HTTP_201_CREATED)
def create_channel(
    body: ChannelCreate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    # Unique name.
    existing = db.execute(
        select(NotificationChannel).where(NotificationChannel.name == body.name)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(409, detail=f"channel with name {body.name!r} already exists")
    _validate_config_for_kind(body.kind, body.config)
    c = NotificationChannel(
        name=body.name,
        kind=body.kind,
        config=body.config,
        events_filter=body.events_filter or "*",
        enabled=body.enabled,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return _serialize(c)


@router.put("/channels/{channel_id}")
def update_channel(
    channel_id: int,
    body: ChannelUpdate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    c = db.get(NotificationChannel, channel_id)
    if c is None:
        raise HTTPException(404, detail=f"channel {channel_id} not found")
    if body.name is not None:
        c.name = body.name
    if body.config is not None:
        _validate_config_for_kind(c.kind, body.config)
        c.config = body.config
    if body.events_filter is not None:
        c.events_filter = body.events_filter
    if body.enabled is not None:
        c.enabled = body.enabled
    db.commit()
    db.refresh(c)
    return _serialize(c)


@router.delete("/channels/{channel_id}")
def delete_channel(
    channel_id: int,
    db: Session = Depends(get_db),
) -> Response:
    c = db.get(NotificationChannel, channel_id)
    if c is None:
        raise HTTPException(404, detail=f"channel {channel_id} not found")
    db.delete(c)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/channels/{channel_id}/test")
async def test_channel(
    channel_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    c = db.get(NotificationChannel, channel_id)
    if c is None:
        raise HTTPException(404, detail=f"channel {channel_id} not found")
    notifier = _notifier_for_kind(c.kind)
    try:
        result: NotificationResult = await send_test_message(
            session_factory=lambda: db,  # use the request's session
            channel=c,
            notifier=notifier,
        )
    finally:
        await _maybe_close(notifier)
    return {
        "ok": result.ok,
        "status_code": result.status_code,
        "error": (result.error or "")[:500] if not result.ok else None,
    }


def _notifier_for_kind(kind: str):
    if kind == "telegram":
        return TelegramNotifier()
    if kind == "discord":
        return DiscordNotifier()
    if kind == "email":
        return EmailNotifier()
    if kind == "webhook":
        return WebhookNotifier()
    raise HTTPException(422, detail=f"unknown kind: {kind!r}")


async def _maybe_close(notifier: Any) -> None:
    aclose = getattr(notifier, "aclose", None)
    if callable(aclose):
        try:
            await aclose()
        except Exception:  # noqa: BLE001
            pass
