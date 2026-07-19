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
from pathlib import Path
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

# Keys we accept in the `global` section. Mirror of `Settings` defaults.
_GLOBAL_KEYS: dict[str, tuple[type, Any]] = {
    "TRADING_MODE": (str, "paper"),
    "MAX_CAPITAL_RISK_PCT": (float, 1.0),
    "DAILY_MAX_LOSS_PCT": (float, 2.0),
    "MAX_CONCURRENT_POSITIONS": (int, 5),
    "MAX_SINGLE_POSITION_PCT": (float, 20.0),
    "MIN_LIQUIDITY_CRORE": (float, 5.0),
    "MAX_SIGNALS_PER_DAY": (int, 20),
    "POLL_INTERVAL_SECONDS": (float, 5.0),
    "PORTFOLIO_VALUE": (float, 1_000_000.0),
    "DEFAULT_SL_PCT": (float, 6.0),
    "DEFAULT_TARGET_RR": (float, 3.0),
    "QUOTE_REFRESH_SECONDS": (int, 5),
    # Pre-LLM noise filter: when ON, clearly-administrative filings
    # (trading-window notices, compliance certificates, newspaper
    # publications, …) are skipped before the paid LLM call. Turning it
    # OFF sends every filing to the AI (more cost, slower queue).
    "PRE_LLM_FILTER_ENABLED": (bool, True),
    # Master switch for AI news analysis (Dashboard toggle). OFF = news is
    # still collected but never sent to the LLM, so no signals / auto trades.
    "AI_ANALYSIS_ENABLED": (bool, True),
    # Extracted-text mode. OFF (default) = legacy behavior: DeepSeek gets
    # the pdf_url + headline metadata only. ON = the filing PDF is
    # downloaded and its relevant pages are extracted and sent as real
    # text. Extraction failures always fall back to the legacy path.
    "SEND_EXTRACTED_TEXT": (bool, False),
    # Deterministic fast track. OFF (default) = every filing takes the
    # LLM track. ON = unambiguous high-conviction headlines (order win /
    # buyback with explicit Rs-crore value, KMP resignation) skip the LLM
    # and hit the rules engine in milliseconds.
    "FAST_TRACK_ENABLED": (bool, False),
    # Phase 4 outcome logger: passive price tracking (+5m/+30m) for every
    # signal into signal_outcomes. Telemetry only, so default ON.
    "OUTCOME_LOGGER_ENABLED": (bool, True),
    # Phase 3 latency knobs. LLM output-token cap (shorter = faster
    # generation; a full signal JSON measured ~165 tokens, keep 2×+
    # headroom). Staleness gate age. Hard end-to-end deadline from
    # filed_at to signal creation — 0 disables; late signals are stored
    # but blocked.
    "LLM_MAX_TOKENS": (int, 400),
    "MAX_NEWS_AGE_SECONDS": (int, 90),
    # Which clock the staleness gate measures. OFF (default) = legacy
    # `now - filed_at`, which bundles in the EXCHANGE's publish lag
    # (measured median ~35s on live data) and so rejects filings for a
    # delay the bot didn't cause. ON = `now - received_at` (the bot's own
    # reaction time) — alpha decays from PUBLICATION, not submission.
    # Guarded by MAX_NEWS_AGE_ABSOLUTE_SECONDS.
    "NEWS_AGE_FROM_RECEIPT": (bool, False),
    "MAX_NEWS_AGE_ABSOLUTE_SECONDS": (int, 1800),
    "PIPELINE_DEADLINE_SECONDS": (int, 0),
    # Intraday buying-power multiplier (Fyers MIS ~5x). Notional caps only —
    # risk-per-trade and loss limits always stay on real equity.
    "INTRADAY_LEVERAGE": (float, 5.0),
    # ---- News sources (multi-channel detection racing) -------------------
    "NSE_API_ENABLED": (bool, True),
    "BSE_API_ENABLED": (bool, True),
    "NSE_RSS_ENABLED": (bool, False),
    "NSE_RSS_POLL_SECONDS": (float, 1.0),
    # ---- Edge Memory (self-learning conviction gate) ---------------------
    "EDGE_GATE_ENABLED": (bool, False),
    "EDGE_GATE_MIN_SAMPLES": (int, 30),
    "EDGE_GATE_MIN_EXPECTANCY_PCT": (float, 0.0),
    # ---- Exit Manager (UI page) ------------------------------------------
    # Every exit-engine knob, exposed for frontend-only control. Defaults
    # mirror app/config.py so exposing them changes nothing by itself.
    # Entry quality. NOTE the engine's real key is ENTRY_MAX_DRIFT_PCT
    # (config.py / entry_manager.py) — .env.example historically documented
    # a "MAX_ENTRY_DRIFT_PCT" that nothing reads.
    "ENTRY_MAX_DRIFT_PCT": (float, 1.5),
    # Initial stop-loss.
    "DEFAULT_SL_MIN_PCT": (float, 1.0),
    "DEFAULT_SL_SMALLCAP_PCT": (float, 1.5),
    "ATR_ENABLED": (bool, True),
    "ATR_PERIOD": (int, 14),
    "ATR_STOP_MULT": (float, 2.0),
    "ATR_MAX_STOP_PCT": (float, 8.0),
    # Breakeven lock.
    "BREAKEVEN_ENABLED": (bool, True),
    "BREAKEVEN_AT_PCT": (float, 2.0),
    "BREAKEVEN_LOCK_PCT": (float, 0.2),
    # Trailing stop + scale-out (open-ended positions only — an explicit
    # rule/AI target always exits the full position instead).
    "SCALE_OUT_ENABLED": (bool, True),
    "SCALE_OUT_R": (float, 2.0),
    "TRAIL_ACTIVATE_R": (float, 1.5),
    "TRAIL_DISTANCE_R": (float, 0.5),
    # Momentum-death exits.
    "CONSOLIDATION_EXIT_ENABLED": (bool, True),
    "CONSOLIDATION_WINDOW_SECONDS": (int, 120),
    "CONSOLIDATION_RANGE_PCT": (float, 0.5),
    "CONSOLIDATION_MIN_PROFIT_PCT": (float, 1.0),
    "CONSOLIDATION_MAX_PROFIT_PCT": (float, 2.5),
    "STALL_EXIT_ENABLED": (bool, True),
    "STALL_WINDOW_SECONDS": (int, 90),
    "STALL_ROC_PCT": (float, 0.3),
    "STALL_MIN_PROFIT_PCT": (float, 3.0),
    "STALL_MAX_PROFIT_PCT": (float, 6.0),
    # Time limits. SQUARE_OFF_TIME_IST is bounded (earlier is allowed,
    # later than 15:15 is not) and can never be disabled — intraday only.
    "MAX_HOLD_SECONDS": (int, 1080),
    "SQUARE_OFF_TIME_IST": (str, "15:10"),
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
    # Booleans need explicit handling: bool("false") is True, so the
    # generic typ(value) cast below would silently mangle a string.
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
        # EDGE_GATE_MIN_EXPECTANCY_PCT is a signed threshold (a losing
        # cohort has negative expectancy; 0 = block only proven losers),
        # so it is exempt from the strict (0, 100] percent rule.
        if (
            key.endswith("_PCT")
            and key != "EDGE_GATE_MIN_EXPECTANCY_PCT"
            and not (0 < coerced <= 100)
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"setting {key}: percent must be in (0, 100]",
            )
        if key == "EDGE_GATE_MIN_EXPECTANCY_PCT" and not (-50 <= coerced <= 50):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="EDGE_GATE_MIN_EXPECTANCY_PCT must be between -50 and 50",
            )
        if key in {"MAX_CONCURRENT_POSITIONS", "MAX_SIGNALS_PER_DAY"} and coerced < 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"setting {key}: must be non-negative",
            )
        # Mirror the Settings validator here — an out-of-range override
        # written to env would make every later get_settings() call blow up.
        if key == "INTRADAY_LEVERAGE" and not (1.0 <= coerced <= 10.0):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="INTRADAY_LEVERAGE must be between 1 and 10",
            )
        # POLL_INTERVAL_SECONDS accepts fractional seconds (float) but keeps
        # a >=1 floor, mirroring the Settings validator — an out-of-range
        # override written to env would make every later get_settings() blow up.
        if key in {"LLM_MAX_TOKENS", "MAX_NEWS_AGE_SECONDS", "POLL_INTERVAL_SECONDS", "ATR_PERIOD"} and coerced < 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"setting {key}: must be >= 1",
            )
        if key == "PIPELINE_DEADLINE_SECONDS" and coerced < 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="PIPELINE_DEADLINE_SECONDS must be >= 0 (0 = disabled)",
            )
        # Exit-engine bounds. R-multiples must be positive and a >10 value
        # is a typo, not a strategy; Settings' own validator only enforces
        # >0 for ATR_STOP_MULT, so mirror + tighten here at the UI door.
        if key in {"ATR_STOP_MULT", "SCALE_OUT_R", "TRAIL_ACTIVATE_R", "TRAIL_DISTANCE_R"} and not (
            0 < coerced <= 10
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"setting {key}: must be in (0, 10]",
            )
        # A sub-minute forced exit or sub-10s observation window is churn,
        # not a strategy.
        if key == "MAX_HOLD_SECONDS" and coerced < 60:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="MAX_HOLD_SECONDS must be >= 60",
            )
        if key in {"CONSOLIDATION_WINDOW_SECONDS", "STALL_WINDOW_SECONDS"} and coerced < 10:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"setting {key}: must be >= 10",
            )
        # RSS poll interval: 0 = follow the global setting; otherwise a
        # 0.5s floor keeps the racer from hammering the CDN.
        if key == "NSE_RSS_POLL_SECONDS" and coerced != 0 and not (0.5 <= coerced <= 60):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="NSE_RSS_POLL_SECONDS must be 0 (follow global) or 0.5–60",
            )
    if key == "TRADING_MODE" and coerced not in {"paper", "live"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="TRADING_MODE must be 'paper' or 'live'",
        )
    if key == "SQUARE_OFF_TIME_IST":
        parts = str(coerced).split(":")
        if len(parts) != 2 or not (parts[0].isdigit() and parts[1].isdigit()):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"SQUARE_OFF_TIME_IST must be 'HH:MM', got {coerced!r}",
            )
        hh, mm = int(parts[0]), int(parts[1])
        # Earlier is always allowed; later than 15:15 would collide with
        # the 15:30 close and the broker's own MIS auto-square-off.
        # There is deliberately NO way to disable the square-off.
        if not (9 * 60 + 30 <= hh * 60 + mm <= 15 * 60 + 15):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="SQUARE_OFF_TIME_IST must be between 09:30 and 15:15 IST",
            )
        coerced = f"{hh:02d}:{mm:02d}"
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
