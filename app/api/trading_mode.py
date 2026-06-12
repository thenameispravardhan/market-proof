"""POST /api/settings/trading-mode — switch paper/live.

The endpoint requires the literal `confirm: true` field in the
request body; anything else (missing, false, 1, "yes", ...) is a
422. This is the "are you sure" gate the spec asks for — flipping
to live is the one operation we want operators to be deliberate
about.

On success:
  1. Validate `mode` is 'paper' or 'live'.
  2. Update (or create) the `app_settings.TRADING_MODE` row.
  3. Write an `audit_log` entry.
  4. **Hot-reload the in-memory `Settings` cache** by clearing
     `get_settings.cache_clear()` and then reading the override
     from the AppSetting row into the process env so the next
     `get_settings().is_paper` / `get_settings().TRADING_MODE` call
     returns the new mode WITHOUT requiring a process restart.
  5. Publish `settings.updated` on the event bus so the execution
     manager hot-reloads (drops cached FyersLiveBackend
     instances).

The `actor` is taken from the `x-actor` header (default: "ui").
We don't authenticate; this is a local-first app. The "loopback
only" guard from main.py is the primary gate.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings, reset_settings_cache
from app.db.infra_models import AppSetting
from app.db.models import AuditLog
from app.db.session import get_db
from app.services.event_bus import event_bus

router = APIRouter(prefix="/api/settings", tags=["settings"])


class TradingModeUpdate(BaseModel):
    mode: str = Field(..., description="'paper' or 'live'")
    confirm: bool = Field(...)

    @field_validator("mode")
    @classmethod
    def _mode_check(cls, v: str) -> str:
        v_norm = (v or "").strip().lower()
        if v_norm not in {"paper", "live"}:
            raise ValueError("mode must be 'paper' or 'live'")
        return v_norm

    @field_validator("confirm")
    @classmethod
    def _confirm_must_be_true(cls, v: Any) -> bool:
        # The literal `True` is the only acceptable value.
        if v is not True:
            raise ValueError("confirm must be the literal true")
        return v


@router.post("/trading-mode")
async def update_trading_mode(
    body: TradingModeUpdate,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    actor = (request.headers.get("x-actor") or "ui").strip().lower() or "ui"
    if actor not in {"ui", "api", "system"}:
        actor = "ui"

    before_row = db.get(AppSetting, "TRADING_MODE")
    before_value = (
        json.loads(before_row.value)
        if before_row is not None and before_row.value
        else None
    )

    # Persist the new value.
    if before_row is None:
        db.add(AppSetting(key="TRADING_MODE", value=json.dumps(body.mode)))
    else:
        before_row.value = json.dumps(body.mode)
    db.flush()

    # Audit.
    db.add(
        AuditLog(
            actor=actor,
            action="settings.trading_mode_changed",
            target="TRADING_MODE",
            before={"TRADING_MODE": before_value},
            after={"TRADING_MODE": body.mode},
        )
    )
    db.commit()

    # ---- Hot-reload: invalidate the cached Settings instance and
    # update the process env so the very next `get_settings().is_paper`
    # (or `.TRADING_MODE`) read returns the new mode. The execution
    # manager consults these on every signal, so the next signal
    # routes to the new backend.
    os.environ["TRADING_MODE"] = body.mode
    reset_settings_cache()

    # Re-broadcast on the bus so the manager's `_on_settings_updated`
    # drops cached FyersLiveBackend instances (forces rebuild on
    # next signal so it picks up the env-override too).
    await event_bus.publish(
        "settings.updated",
        {"changed_keys": ["TRADING_MODE"]},
    )

    # Read the FRESH settings (env override was just applied).
    settings = get_settings()
    return {
        "ok": True,
        "mode": body.mode,
        "effective": settings.TRADING_MODE,
    }

