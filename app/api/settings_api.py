"""GET/PUT /api/settings — global settings + per-section configs.

Persists a snapshot of the previous and new values to `audit_log` on every
PUT, and broadcasts a `settings.updated` event on the event bus for hot
reload by sibling tracks.

The runtime overrides live in a tiny key-value table (`app_settings`)
created on first use. The .env file holds deployment-time config; this
table holds in-app tweaks made via the UI.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings, reset_settings_cache
from app.db.init import init_db
from app.db.infra_models import AppSetting
from app.db.models import AuditLog
from app.db.session import get_db
from app.services.event_bus import event_bus

router = APIRouter(prefix="/api/settings", tags=["settings"])


# Make sure the table exists before the first request hits the endpoint.
# `init_db` is idempotent (CREATE TABLE IF NOT EXISTS).
init_db()


# -------------------------------------------------------------------------
# Pydantic schemas
# -------------------------------------------------------------------------

# Keys we accept in the `global` section. Mirror of `Settings` defaults.
_GLOBAL_KEYS: dict[str, tuple[type, Any]] = {
    "TRADING_MODE": (str, "paper"),
    "MAX_CAPITAL_RISK_PCT": (float, 1.0),
    "DAILY_MAX_LOSS_PCT": (float, 2.0),
    "MAX_CONCURRENT_POSITIONS": (int, 5),
    "MAX_SINGLE_POSITION_PCT": (float, 20.0),
    "MIN_LIQUIDITY_CRORE": (float, 5.0),
    "MAX_SIGNALS_PER_DAY": (int, 20),
    "POLL_INTERVAL_SECONDS": (int, 5),
    "PORTFOLIO_VALUE": (float, 1_000_000.0),
    "DEFAULT_SL_PCT": (float, 6.0),
    "DEFAULT_TARGET_RR": (float, 3.0),
    "QUOTE_REFRESH_SECONDS": (int, 5),
}


def apply_overrides_to_env(db: Session) -> None:
    """Push DB overrides into the process env + reset the Settings cache.

    This is what makes UI edits *effective* without a restart: every
    component reads `get_settings()`, which is rebuilt from env on the
    next call after the cache reset. Called on PUT and at startup so
    overrides survive restarts.
    """
    overrides = _read_overrides(db)
    for key, value in overrides.items():
        if value is None:
            continue
        os.environ[key] = str(value)
    if overrides:
        reset_settings_cache()


class SettingsUpdate(BaseModel):
    global_: dict[str, Any] = Field(default_factory=dict, alias="global")
    sections: dict[str, dict[str, Any]] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}

    @field_validator("global_")
    @classmethod
    def _check_known_keys(cls, v: dict[str, Any]) -> dict[str, Any]:
        unknown = [k for k in v.keys() if k not in _GLOBAL_KEYS]
        if unknown:
            raise ValueError(f"unknown global setting(s): {unknown}")
        return v


# -------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------


def _coerce(key: str, value: Any) -> Any:
    """Coerce incoming value to the declared Python type and validate range."""
    typ, _default = _GLOBAL_KEYS[key]
    try:
        coerced = typ(value)
    except (TypeError, ValueError) as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"setting {key}: cannot coerce {value!r} to {typ.__name__}: {e}",
        ) from e
    if typ in (int, float):
        if key.endswith("_PCT") and not (0 < coerced <= 100):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"setting {key}: percent must be in (0, 100]",
            )
        if key in {"MAX_CONCURRENT_POSITIONS", "MAX_SIGNALS_PER_DAY", "POLL_INTERVAL_SECONDS"} and coerced < 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"setting {key}: must be non-negative",
            )
    if key == "TRADING_MODE" and coerced not in {"paper", "live"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="TRADING_MODE must be 'paper' or 'live'",
        )
    return coerced


def _read_overrides(db: Session) -> dict[str, Any]:
    rows = db.execute(select(AppSetting)).scalars().all()
    out: dict[str, Any] = {}
    for row in rows:
        if row.key not in _GLOBAL_KEYS:
            continue
        if row.value is None:
            out[row.key] = None
            continue
        try:
            out[row.key] = _GLOBAL_KEYS[row.key][0](json.loads(row.value))
        except (TypeError, ValueError):
            out[row.key] = row.value
    return out


def _write_overrides(db: Session, updates: dict[str, Any]) -> dict[str, Any]:
    for key, raw_value in updates.items():
        value = _coerce(key, raw_value)
        existing = db.get(AppSetting, key)
        if existing is None:
            db.add(AppSetting(key=key, value=json.dumps(value)))
        else:
            existing.value = json.dumps(value)
    db.flush()
    return _read_overrides(db)


def _audit(
    db: Session,
    *,
    actor: str,
    action: str,
    target: Optional[str],
    before: Optional[dict[str, Any]],
    after: Optional[dict[str, Any]],
) -> None:
    db.add(
        AuditLog(
            actor=actor,
            action=action,
            target=target,
            before=before,
            after=after,
        )
    )
    db.flush()


# -------------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------------


@router.get("")
def get_settings_endpoint(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Return the effective global settings (env defaults + DB overrides)."""
    overrides = _read_overrides(db)

    effective: dict[str, Any] = {}
    for key, (_typ, default) in _GLOBAL_KEYS.items():
        env_value = getattr(settings, key, default)
        if key in overrides and overrides[key] is not None:
            effective[key] = overrides[key]
        else:
            effective[key] = env_value

    # Sections: not used in v1. Sibling tracks can extend.
    return {"global": effective, "sections": {}, "version": settings.APP_VERSION}


@router.put("")
async def update_settings(
    body: SettingsUpdate,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    before_overrides = _read_overrides(db)
    new_overrides = _write_overrides(db, body.global_)

    if before_overrides != new_overrides:
        _audit(
            db,
            actor="api",
            action="settings.update",
            target="global",
            before=before_overrides,
            after=new_overrides,
        )
    db.commit()

    # Make the new values effective immediately: env + cache reset.
    apply_overrides_to_env(db)

    # Broadcast for hot reload. Subscribers re-read DB/env on receipt.
    changed = sorted(set(new_overrides) - set(before_overrides)) or sorted(new_overrides.keys())
    await event_bus.publish("settings.updated", {"changed_keys": changed})

    return get_settings_endpoint(db=db, settings=settings)
