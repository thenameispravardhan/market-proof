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
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.settings_schema import (
    BOUNDS,
    TIME_BOUNDS_IST,
    build_registry,
    schema_payload,
)
from app.config import Settings, get_settings, reset_settings_cache
from app.db.init import init_db
from app.db.infra_models import AppSetting
from app.db.models import AuditLog
from app.db.session import get_db
from app.services.event_bus import event_bus

router = APIRouter(prefix="/api/settings", tags=["settings"])

# The project .env — the durable home for credentials. The credentials
# editor writes here so the file stays the single source of truth, AND
# pushes the values into the live process (os.environ + cache reset) so
# the change takes effect without a restart. Module-level so tests can
# monkeypatch it to a temp path.
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


# Make sure the table exists before the first request hits the endpoint.
# `init_db` is idempotent (CREATE TABLE IF NOT EXISTS).
init_db()


# -------------------------------------------------------------------------
# Pydantic schemas
# -------------------------------------------------------------------------

# Keys we accept in the `global` section — DERIVED from `Settings`, never
# hand-maintained. Every field on the Settings model is operator-editable
# except the credentials and boot-time keys listed in settings_schema.EXCLUDED.
#
# This used to be a hand-written dict. It drifted from config.py on seven
# defaults and was missing 67 keys entirely, which is exactly the failure the
# "frontend-only control" invariant exists to prevent. Deriving it means a new
# knob in config.py is reachable from the UI the moment it is declared.
_REGISTRY: dict[str, dict[str, Any]] = build_registry()
_GLOBAL_KEYS: dict[str, tuple[type, Any]] = {
    key: ({"bool": bool, "int": int, "float": float, "str": str}[spec["type"]], spec["default"])
    for key, spec in _REGISTRY.items()
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
    """Coerce an incoming value to the declared type and validate its range.

    Bounds come from the derived registry (settings_schema.BOUNDS + the
    suffix conventions), so a new knob is guarded the moment it is declared.
    The final check re-validates the whole Settings model: an override that
    slipped past the range checks would otherwise be written to os.environ and
    make EVERY later get_settings() call raise, taking the app down.
    """
    spec = _REGISTRY[key]
    typ, _default = _GLOBAL_KEYS[key]

    # NOTE: spec["read_only"] is a UI hint only — the generic form renders
    # those keys uneditable and points at their own control. It is deliberately
    # NOT enforced here: PUT /api/settings has always accepted TRADING_MODE,
    # and tests depend on that. It does mean this endpoint bypasses the typed
    # "LIVE" confirmation on /api/settings/trading-mode.
    #
    # Booleans need explicit handling: bool("false") is True, so the generic
    # typ(value) cast below would silently mangle a string.
    if typ is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"setting {key}: cannot coerce {value!r} to bool",
        )

    try:
        coerced = typ(value)
    except (TypeError, ValueError) as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"setting {key}: cannot coerce {value!r} to {typ.__name__}: {e}",
        ) from e

    if typ in (int, float):
        lo, hi = spec["min"], spec["max"]
        if lo is not None and hi is not None and not (lo <= coerced <= hi):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"setting {key}: must be between {lo:g} and {hi:g}",
            )
        # A percent that reaches 0 disables the check it guards, which is a
        # footgun rather than a setting. Thresholds that are meaningfully zero
        # (a disabled deadline, "follow the global interval", a signed
        # expectancy floor) declare their own lower bound in BOUNDS.
        if key.endswith("_PCT") and key not in BOUNDS and coerced <= 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"setting {key}: percent must be greater than 0",
            )

    if spec["choices"] and coerced not in spec["choices"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"setting {key}: must be one of {spec['choices']}",
        )

    if spec["widget"] == "time":
        coerced = _coerce_ist_time(key, coerced)

    _reject_if_settings_would_break(key, coerced)
    return coerced


def _coerce_ist_time(key: str, value: Any) -> str:
    """Validate an 'HH:MM' IST clock field against its allowed window."""
    parts = str(value).split(":")
    if len(parts) != 2 or not (parts[0].isdigit() and parts[1].isdigit()):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{key} must be 'HH:MM', got {value!r}",
        )
    hh, mm = int(parts[0]), int(parts[1])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{key}: time out of range: {value!r}",
        )
    window = TIME_BOUNDS_IST.get(key)
    if window:
        lo_s, hi_s = window
        as_min = hh * 60 + mm
        lo = int(lo_s[:2]) * 60 + int(lo_s[3:])
        hi = int(hi_s[:2]) * 60 + int(hi_s[3:])
        # Earlier is allowed for the square-off; later would collide with the
        # 15:30 close and the broker's own MIS auto-square-off. There is
        # deliberately NO way to disable it — the strategy is intraday-only.
        if not (lo <= as_min <= hi):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{key} must be between {lo_s} and {hi_s} IST",
            )
    return f"{hh:02d}:{mm:02d}"


def _reject_if_settings_would_break(key: str, coerced: Any) -> None:
    """Last line of defence: would this value make Settings() itself raise?

    Overrides are applied by writing os.environ and clearing the Settings
    cache, so a value that fails the model's own validators does not fail the
    PUT — it fails every subsequent request. Catch it here, at the door.
    """
    try:
        Settings(**{key: coerced})  # type: ignore[arg-type]
    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"setting {key}: {e.errors()[0].get('msg', 'invalid value')}",
        ) from e


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


def _clear_overrides(db: Session, keys: list[str]) -> list[str]:
    """Delete override rows so the key falls back to .env / the code default.

    Deleting the row is only half the job: `apply_overrides_to_env` only ever
    SETS os.environ, so a stale value would keep winning until the next
    restart. Pop it here and let pydantic re-read the .env file.

    (If a key was set in the real process environment — a systemd
    `Environment=` line rather than the UI — popping it loses that value until
    the service restarts. None of the current unit's vars are in the registry.)
    """
    cleared: list[str] = []
    for key in keys:
        row = db.get(AppSetting, key)
        if row is None:
            continue
        db.delete(row)
        os.environ.pop(key, None)
        cleared.append(key)
    if cleared:
        db.flush()
        reset_settings_cache()
    return cleared


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


@router.get("/schema")
def get_settings_schema(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Every operator-editable setting, grouped, with its current value.

    The UI renders this generically — it does not know a single key name — so
    a knob declared in config.py shows up on the Settings page with no
    frontend change at all.
    """
    effective = get_settings_endpoint(db=db, settings=settings)["global"]
    overridden = set(_read_overrides(db))
    groups = schema_payload(effective)
    for group in groups:
        for field in group["fields"]:
            # "Overridden" means an explicit row exists — NOT merely that the
            # value differs from the default. A key can be pinned to its own
            # default value, and that still blocks a future default change.
            field["overridden"] = field["key"] in overridden
    return {
        "groups": groups,
        "overridden_count": len(overridden),
        "version": settings.APP_VERSION,
    }


def _fresh_state(db: Session) -> dict[str, Any]:
    """Effective settings read AFTER a mutation.

    The request-scoped `Settings` from Depends(get_settings) is resolved before
    the handler runs, so it still carries the pre-reset values. That is
    invisible on a PUT (the override row wins over the env value anyway) but
    wrong on a delete, where the row is gone and the stale object IS the
    answer. Re-resolve past the cleared cache.
    """
    return get_settings_endpoint(db=db, settings=get_settings())


@router.delete("/overrides/{key}")
async def reset_setting(
    key: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Drop one override so the key falls back to .env / the code default."""
    if key not in _GLOBAL_KEYS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown setting: {key}"
        )
    before = _read_overrides(db)
    cleared = _clear_overrides(db, [key])
    if cleared:
        _audit(db, actor="ui", action="settings.reset", target=key,
               before={key: before.get(key)}, after=None)
    db.commit()
    apply_overrides_to_env(db)
    await event_bus.publish("settings.updated", {"changed_keys": cleared})
    return {"reset": cleared, **_fresh_state(db)}


class ResetRequest(BaseModel):
    """Empty group = every override. `confirm` is required either way — this
    discards operator tuning that has no other copy."""
    group: Optional[str] = None
    confirm: bool = False


@router.post("/reset")
async def reset_settings(
    body: ResetRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Reset a whole group (or everything) to the config.py defaults."""
    if not body.confirm:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="reset requires confirm=true",
        )
    registry = _REGISTRY
    targets = [
        key for key in _read_overrides(db)
        # Keys with their own guarded control (TRADING_MODE) are left alone:
        # a bulk reset must never be a back door into changing the mode.
        if not registry[key]["read_only"]
        and (body.group is None or registry[key]["group"] == body.group)
    ]
    before = {k: v for k, v in _read_overrides(db).items() if k in targets}
    cleared = _clear_overrides(db, targets)
    if cleared:
        _audit(db, actor="ui", action="settings.reset",
               target=body.group or "all", before=before, after=None)
    db.commit()
    apply_overrides_to_env(db)
    await event_bus.publish("settings.updated", {"changed_keys": cleared})
    return {"reset": cleared, **_fresh_state(db)}


@router.get("/export")
def export_settings(db: Session = Depends(get_db)) -> dict[str, Any]:
    """The overrides only — not the whole effective config.

    Exporting effective values would bake today's defaults into the file, so
    importing it later would pin every key and silently block future default
    changes. Only what the operator actually set travels.
    """
    return {
        "version": get_settings().APP_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "overrides": _read_overrides(db),
    }


class ImportRequest(BaseModel):
    overrides: dict[str, Any]
    # Default False = merge onto what is already there. True = the imported
    # file becomes the complete override set (anything absent goes to default).
    replace: bool = False


@router.post("/import")
async def import_settings(
    body: ImportRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Apply an exported settings file.

    Unknown keys are reported, not fatal — an export from a newer build (or one
    carrying another module's app_settings rows) should still import the keys
    this build understands. Every accepted value goes through the same
    validation as a normal PUT.
    """
    known = {k: v for k, v in body.overrides.items() if k in _GLOBAL_KEYS}
    ignored = sorted(set(body.overrides) - set(known))

    before = _read_overrides(db)
    cleared: list[str] = []
    if body.replace:
        cleared = _clear_overrides(db, [k for k in before if k not in known])
    after = _write_overrides(db, known)

    _audit(db, actor="ui", action="settings.import", target="global",
           before=before, after=after)
    db.commit()
    apply_overrides_to_env(db)
    await event_bus.publish("settings.updated", {"changed_keys": sorted(known)})
    return {
        "imported": sorted(known),
        "reset": cleared,
        "ignored": ignored,
        **_fresh_state(db),
    }


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


# -------------------------------------------------------------------------
# Credentials (Fyers + DeepSeek) — editable from the UI, hot-applied.
# -------------------------------------------------------------------------

# Secret-valued keys are never returned to the client; only a masked
# preview + a "set" flag.
_SECRET_CRED_KEYS = ("FYERS_SECRET_KEY", "DEEPSEEK_API_KEY")


def _mask_secret(value: Optional[str]) -> Optional[str]:
    """`••••••Hc` — confirm WHICH secret is stored without revealing it."""
    if not value:
        return None
    tail = value[-2:] if len(value) >= 4 else ""
    return "•" * max(4, len(value) - len(tail)) + tail


def _upsert_env_vars(updates: dict[str, str]) -> None:
    """Insert/replace `KEY=value` lines in the project .env, preserving the
    rest of the file. Creates .env if missing; backs up to .env.bak first."""
    text = _ENV_PATH.read_text(encoding="utf-8") if _ENV_PATH.exists() else ""
    if _ENV_PATH.exists():
        try:
            (_ENV_PATH.parent / ".env.bak").write_text(text, encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    for key, value in updates.items():
        line = f"{key}={value}"
        pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
        if pattern.search(text):
            text = pattern.sub(line, text)
        else:
            if text and not text.endswith("\n"):
                text += "\n"
            text += line + "\n"
    _ENV_PATH.write_text(text, encoding="utf-8")


class CredentialsUpdate(BaseModel):
    """All optional — only the non-empty fields are changed. An empty
    secret means 'leave the existing secret untouched'."""
    fyers_app_id: Optional[str] = None
    fyers_secret_key: Optional[str] = None
    fyers_redirect_uri: Optional[str] = None
    deepseek_api_key: Optional[str] = None


def _credentials_view(settings: Settings) -> dict[str, Any]:
    return {
        "fyers_app_id": settings.FYERS_APP_ID or "",
        "fyers_redirect_uri": settings.FYERS_REDIRECT_URI
        or "http://localhost:8000/api/fyers/callback",
        "fyers_secret_set": bool((settings.FYERS_SECRET_KEY or "").strip()),
        "fyers_secret_masked": _mask_secret(settings.FYERS_SECRET_KEY),
        "deepseek_key_set": bool((settings.DEEPSEEK_API_KEY or "").strip()),
        "deepseek_key_masked": _mask_secret(settings.DEEPSEEK_API_KEY),
    }


@router.get("/credentials")
def get_credentials(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """Effective Fyers + DeepSeek credentials. Secrets are NEVER returned —
    only a masked preview and a `*_set` boolean."""
    return _credentials_view(settings)


@router.put("/credentials")
async def update_credentials(
    body: CredentialsUpdate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Update credentials from the UI. Writes to .env (durable) and applies
    to the live process (os.environ + Settings cache reset) so the change
    takes effect immediately — no restart. After changing the Fyers App ID
    or Secret, the operator must re-run Connect Fyers (new app → new token)."""
    updates: dict[str, str] = {}
    if body.fyers_app_id is not None and body.fyers_app_id.strip():
        updates["FYERS_APP_ID"] = body.fyers_app_id.strip()
    if body.fyers_secret_key and body.fyers_secret_key.strip():
        updates["FYERS_SECRET_KEY"] = body.fyers_secret_key.strip()
    if body.fyers_redirect_uri is not None and body.fyers_redirect_uri.strip():
        updates["FYERS_REDIRECT_URI"] = body.fyers_redirect_uri.strip()
    if body.deepseek_api_key and body.deepseek_api_key.strip():
        updates["DEEPSEEK_API_KEY"] = body.deepseek_api_key.strip()

    if not updates:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="no credential fields provided",
        )

    # Durable: write to .env (skipped under TESTING so the suite never
    # touches the real file; the hot-apply below is still exercised).
    if not get_settings().is_testing:
        try:
            _upsert_env_vars(updates)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"could not write .env: {e}",
            )
    # Hot: live process picks the values up on the next get_settings().
    for key, value in updates.items():
        os.environ[key] = value
    reset_settings_cache()

    # Audit WITHOUT secret values.
    _audit(
        db,
        actor="ui",
        action="settings.credentials",
        target="fyers",
        before=None,
        after={
            k: ("***set***" if k in _SECRET_CRED_KEYS else v) for k, v in updates.items()
        },
    )
    db.commit()

    # Drop cached Fyers backends (built with the old app_id) so the next
    # order / OAuth rebuilds with the new credentials. The execution
    # manager's `settings.updated` handler does exactly this.
    await event_bus.publish("settings.updated", {"changed_keys": list(updates.keys())})

    return _credentials_view(get_settings())
