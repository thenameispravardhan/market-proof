"""`/api/broker-accounts` — CRUD for broker accounts.

secret_key and access_token are NEVER returned in responses.

The enable switch on a REAL (non-paper) account doubles as the live-
trading master switch: enabling it flips TRADING_MODE to `live`,
disabling flips back to `paper`. One Dashboard toggle = real trading
on/off — the operator controls everything from the frontend.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import reset_settings_cache
from app.db.infra_models import AppSetting
from app.db.init import init_db
from app.db.models import AuditLog, BrokerAccount
from app.db.session import get_db
from app.logging_config import get_logger
from app.services.event_bus import event_bus

log = get_logger(__name__)

router = APIRouter(prefix="/api/broker-accounts", tags=["broker_accounts"])
init_db()


# -------------------------------------------------------------------------
# Pydantic schemas
# -------------------------------------------------------------------------


class BrokerAccountCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    broker: str = Field(default="fyers", min_length=1, max_length=32)
    app_id: Optional[str] = None
    secret_key: Optional[str] = None
    access_token: Optional[str] = None
    redirect_uri: Optional[str] = None
    paper_mode: bool = True
    enabled: bool = True


class BrokerAccountUpdate(BaseModel):
    name: Optional[str] = None
    broker: Optional[str] = None
    app_id: Optional[str] = None
    secret_key: Optional[str] = None
    access_token: Optional[str] = None
    redirect_uri: Optional[str] = None
    paper_mode: Optional[bool] = None
    enabled: Optional[bool] = None


# -------------------------------------------------------------------------
# Helpers — never expose secret_key or access_token
# -------------------------------------------------------------------------


def _ser(a: BrokerAccount, *, reveal_secrets: bool = False) -> dict[str, Any]:
    """Serialise a BrokerAccount. Secrets are ALWAYS masked unless reveal_secrets=True.

    The public API always passes reveal_secrets=False.
    """
    out: dict[str, Any] = {
        "id": a.id,
        "name": a.name,
        "broker": a.broker,
        "app_id": a.app_id,
        "redirect_uri": a.redirect_uri,
        "paper_mode": a.paper_mode,
        "enabled": a.enabled,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }
    if reveal_secrets:
        out["secret_key"] = a.secret_key
        out["access_token"] = a.access_token
    else:
        out["secret_key"] = "***" if a.secret_key else None
        out["access_token"] = "***" if a.access_token else None
    return out


def _ser_audit(a: BrokerAccount) -> dict[str, Any]:
    """Audit-safe serialiser — no secrets."""
    return _ser(a, reveal_secrets=False)


def _get_or_404(account_id: int, db: Session) -> BrokerAccount:
    a = db.get(BrokerAccount, account_id)
    if a is None:
        raise HTTPException(404, detail=f"broker account {account_id} not found")
    return a


def _audit(
    db: Session,
    *,
    action: str,
    target: str,
    before: Optional[dict],
    after: Optional[dict],
) -> None:
    db.add(
        AuditLog(actor="api", action=action, target=target, before=before, after=after)
    )


# -------------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------------


@router.get("")
def list_accounts(db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.execute(select(BrokerAccount).order_by(BrokerAccount.id.asc())).scalars().all()
    return {"accounts": [_ser(a) for a in rows]}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_account(body: BrokerAccountCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    existing = db.execute(
        select(BrokerAccount).where(BrokerAccount.name == body.name)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(409, detail=f"broker account with name '{body.name}' already exists")

    a = BrokerAccount(
        name=body.name,
        broker=body.broker,
        app_id=body.app_id,
        secret_key=body.secret_key,
        access_token=body.access_token,
        redirect_uri=body.redirect_uri,
        paper_mode=body.paper_mode,
        enabled=body.enabled,
    )
    db.add(a)
    db.flush()
    _audit(db, action="broker_account.create", target=f"broker_account:{a.id}", before=None, after=_ser_audit(a))
    db.commit()
    db.refresh(a)
    return _ser(a)


def _sync_trading_mode(db: Session, mode: str) -> Optional[str]:
    """Persist TRADING_MODE + hot-apply (env + settings cache). Returns
    the previous mode (or None when unchanged). Caller commits + publishes."""
    row = db.get(AppSetting, "TRADING_MODE")
    before = json.loads(row.value) if row is not None and row.value else None
    if before == mode:
        return None
    if row is None:
        db.add(AppSetting(key="TRADING_MODE", value=json.dumps(mode)))
    else:
        row.value = json.dumps(mode)
    db.flush()
    db.add(
        AuditLog(
            actor="ui",
            action="settings.trading_mode_changed",
            target="TRADING_MODE",
            before={"TRADING_MODE": before},
            after={"TRADING_MODE": mode, "via": "broker_account_toggle"},
        )
    )
    os.environ["TRADING_MODE"] = mode
    reset_settings_cache()
    return before or "paper"


@router.put("/{account_id}")
async def update_account(
    account_id: int,
    body: BrokerAccountUpdate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    a = _get_or_404(account_id, db)
    before = _ser_audit(a)
    if body.name is not None:
        existing = db.execute(
            select(BrokerAccount)
            .where(BrokerAccount.name == body.name)
            .where(BrokerAccount.id != account_id)
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(409, detail=f"broker account with name '{body.name}' already exists")
        a.name = body.name
    if body.broker is not None:
        a.broker = body.broker
    if body.app_id is not None:
        a.app_id = body.app_id
    if body.secret_key is not None:
        a.secret_key = body.secret_key or None
    if body.access_token is not None:
        a.access_token = body.access_token or None
    if body.redirect_uri is not None:
        a.redirect_uri = body.redirect_uri or None
    if body.paper_mode is not None:
        a.paper_mode = body.paper_mode
    if body.enabled is not None:
        a.enabled = body.enabled
    _audit(db, action="broker_account.update", target=f"broker_account:{a.id}", before=before, after=_ser_audit(a))

    # The REAL account's enable switch IS the live-trading switch
    # (frontend-only control): Fyers ON → TRADING_MODE=live, OFF →
    # paper. Paper-account toggles never touch the mode.
    mode_changed = False
    if body.enabled is not None and not a.paper_mode:
        mode_changed = (
            _sync_trading_mode(db, "live" if a.enabled else "paper") is not None
        )

    db.commit()
    db.refresh(a)

    if mode_changed:
        # Hot-reload subscribers (execution manager drops cached live
        # backends so the next signal routes with the new mode).
        await event_bus.publish(
            "settings.updated", {"changed_keys": ["TRADING_MODE"]}
        )
    return _ser(a)


@router.delete("/{account_id}")
def delete_account(account_id: int, db: Session = Depends(get_db)) -> Response:
    a = _get_or_404(account_id, db)
    before = _ser_audit(a)
    db.delete(a)
    _audit(db, action="broker_account.delete", target=f"broker_account:{account_id}", before=before, after=None)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{account_id}/test")
async def test_account(account_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Validate credentials by hitting a benign Fyers endpoint (GET /api/v3/profile).

    In paper mode or if no access_token, returns a clear error rather than
    making a live API call.
    """
    a = _get_or_404(account_id, db)
    if a.paper_mode:
        return {"ok": True, "message": "paper_mode — no live API call needed", "broker": a.broker}
    if not a.access_token:
        return {"ok": False, "message": "access_token is not set; run the OAuth flow first"}
    if not a.app_id:
        return {"ok": False, "message": "app_id is not set"}

    # Try hitting Fyers /api/v3/profile
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api-t1.fyers.in/api/v3/profile",
                headers={
                    "Authorization": f"{a.app_id}:{a.access_token}",
                    "Content-Type": "application/json",
                },
            )
        if resp.status_code == 200:
            data = resp.json()
            return {
                "ok": True,
                "message": "credentials valid",
                "broker": a.broker,
                "status_code": resp.status_code,
                "name": data.get("data", {}).get("name", "unknown"),
            }
        else:
            return {
                "ok": False,
                "message": f"Fyers returned {resp.status_code}",
                "status_code": resp.status_code,
            }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"connection failed: {exc}"}
