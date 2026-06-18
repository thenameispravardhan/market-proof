"""Fyers order postback (webhook) — order reconciliation, NOT signals.

When Fyers fills / rejects / cancels an order the bot placed, it POSTs
an order-update to the webhook URL configured in the Fyers dashboard.
This endpoint **reconciles the existing trade** (matched by
`broker_order_id`) — updating its status, fill price and P&L — and
publishes `trades.filled`. It deliberately does NOT create a new
analysis/signal (that would risk a re-trade feedback loop).

Auth: the shared secret is carried in the URL as a `?token=` query
param, so authentication does not depend on Fyers' (changing) request
signing. The operator pastes the full URL — including the token — into
the Fyers dashboard's Postback URL field.

Everything is frontend-configurable:
  GET  /api/fyers/postback/config   -> URL, secret, public base, preference
  POST /api/fyers/postback/config   -> set public base URL / regenerate secret
  POST /api/fyers/postback?token=.. -> the receiver Fyers calls
"""
from __future__ import annotations

import json
import os
import secrets
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.infra_models import AppSetting
from app.db.models import AuditLog
from app.db.session import get_db
from app.execution.order_reconcile import reconcile_order_update

router = APIRouter(prefix="/api/fyers", tags=["fyers"])

_SECRET_KEY = "FYERS_POSTBACK_SECRET"
_PUBLIC_URL_KEY = "PUBLIC_BASE_URL"
_POSTBACK_PATH = "/api/fyers/postback"
# What to enable in the Fyers dashboard's "Webhook Preference" / triggers.
_PREFERENCE = "Order Update (placed / filled / rejected / cancelled)"


# ---- tiny AppSetting helpers (frontend-editable, survive restarts) -------


def _get(db: Session, key: str) -> Optional[str]:
    row = db.get(AppSetting, key)
    if row is None or row.value is None:
        return None
    try:
        return json.loads(row.value)
    except (TypeError, ValueError):
        return row.value


def _set(db: Session, key: str, value: Optional[str]) -> None:
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=json.dumps(value)))
    else:
        row.value = json.dumps(value)
    db.flush()


def _effective_secret(db: Session) -> Optional[str]:
    # .env (FYERS_POSTBACK_SECRET) is the single source of truth when
    # set — keeping all Fyers secrets in one place. The DB AppSetting is
    # only a fallback for when .env leaves it blank (e.g. the operator
    # uses the UI "Regenerate secret" button instead of editing .env).
    return (get_settings().FYERS_POSTBACK_SECRET or None) or _get(db, _SECRET_KEY)


def _build_url(public_base: Optional[str], secret: Optional[str]) -> Optional[str]:
    if not public_base or not secret:
        return None
    return f"{public_base.rstrip('/')}{_POSTBACK_PATH}?token={secret}"


# ---- config endpoints ----------------------------------------------------


@router.get("/postback/config")
def postback_config(db: Session = Depends(get_db)) -> dict[str, Any]:
    secret = _effective_secret(db)
    public = _get(db, _PUBLIC_URL_KEY) or ""
    return {
        "public_base_url": public,
        "secret": secret,
        "path": _POSTBACK_PATH,
        "url": _build_url(public, secret),
        "preference": _PREFERENCE,
        "configured": bool(secret and public),
        "secret_present": bool(secret),
    }


class PostbackConfigUpdate(BaseModel):
    public_base_url: Optional[str] = None
    regenerate_secret: bool = False


@router.post("/postback/config")
def update_postback_config(
    body: PostbackConfigUpdate, db: Session = Depends(get_db)
) -> dict[str, Any]:
    if body.public_base_url is not None:
        _set(db, _PUBLIC_URL_KEY, body.public_base_url.strip())
    if body.regenerate_secret or not _effective_secret(db):
        new_secret = "fy_" + secrets.token_urlsafe(24)
        _set(db, _SECRET_KEY, new_secret)
        # `_effective_secret` reads .env (FYERS_POSTBACK_SECRET) FIRST — it's
        # the source of truth — so a DB-only write is ignored in production
        # and the displayed secret never changes ("regenerate not working").
        # Persist to .env and hot-apply so the new value actually takes
        # effect. Skipped under TESTING (where .env is blank and the DB
        # override is authoritative).
        if not get_settings().is_testing:
            from app.api.settings_api import _upsert_env_vars
            from app.config import reset_settings_cache

            try:
                _upsert_env_vars({"FYERS_POSTBACK_SECRET": new_secret})
            except Exception:  # noqa: BLE001
                pass
            os.environ["FYERS_POSTBACK_SECRET"] = new_secret
            reset_settings_cache()
    db.add(
        AuditLog(
            actor="ui",
            action="fyers.postback_config",
            target="fyers_postback",
            after={
                "public_base_url_set": body.public_base_url is not None,
                "secret_rotated": body.regenerate_secret,
            },
        )
    )
    db.commit()
    return postback_config(db=db)


# ---- the receiver Fyers calls -------------------------------------------


@router.post("/postback")
async def fyers_postback(
    request: Request,
    token: str = Query(default=""),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    secret = _effective_secret(db)
    if secret and token != secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad token")

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid json")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="expected object")

    # Both the postback and the order WebSocket funnel through the same
    # reconciler so they behave identically (match by broker_order_id,
    # mirror the fill into positions, publish trades.filled). Never
    # creates a signal.
    return await reconcile_order_update(db, payload, source="fyers_postback")
