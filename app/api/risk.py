"""Risk / circuit-breaker control endpoints (RISK.md §6).

  GET  /api/risk/state   — current circuit-breaker / kill-switch state +
                           a live equity / counters snapshot.
  POST /api/risk/kill    — KILL SWITCH: flatten every open position and
                           disable trading until an explicit resume.
  POST /api/risk/resume  — clear the halt / cooldown and trade again.

Local-first, no auth (the loopback bind in main.py is the gate), matching
the other routers. The kill switch flattens via the running TradeManager
(app.state.trade_manager) when present.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.db.models import AuditLog
from app.db.session import get_db
from app.logging_config import get_logger
from app.risk import circuit_breakers as cb
from app.risk import position_sizer
from app.services.event_bus import event_bus

router = APIRouter(prefix="/api/risk", tags=["risk"])
log = get_logger(__name__)


def _state_snapshot(db: Session, request: Request) -> dict[str, Any]:
    state = cb.get_or_create_state(db)
    md = getattr(getattr(request.app, "state", None), "execution_manager", None)
    md_bus = md.market_data if md is not None else None
    equity = position_sizer.compute_equity(db, md_bus)
    allowed, reason = cb.entry_allowed(db, confidence=1.0)
    return {
        "trading_disabled": state.trading_disabled,
        "disabled_reason": state.disabled_reason,
        "halted_until": state.halted_until.isoformat() if state.halted_until else None,
        "current_risk_pct": state.current_risk_pct,
        "equity": round(equity, 2),
        "day_start_equity": state.day_start_equity,
        "week_peak_equity": state.week_peak_equity,
        "month_start_equity": state.month_start_equity,
        "trades_today": cb.entries_today(db),
        "consecutive_losers": cb.trailing_consecutive_losers(db),
        "entries_allowed": allowed,
        "entry_block_reason": reason,
    }


@router.get("/state")
def risk_state(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    return _state_snapshot(db, request)


@router.get("/breaker-history")
def breaker_history(
    limit: int = 100, db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    """Every circuit-breaker trip / manual kill / resume, newest first —
    the persistent answer to "WHY was the bot halted?". Each row carries
    the threshold that tripped, the value at trip time, and the action
    taken (halt / flatten / throttle / cooldown)."""
    from sqlalchemy import select

    from app.db.models import RiskEvent

    limit = max(1, min(int(limit), 500))
    rows = (
        db.execute(
            select(RiskEvent)
            .where(RiskEvent.event_type.like("BREAKER_%"))
            .order_by(RiskEvent.id.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": r.id,
            "event_type": r.event_type,
            "severity": r.severity,
            "message": r.message,
            "threshold_value": (r.context or {}).get("threshold_value"),
            "current_value": (r.context or {}).get("current_value"),
            "action_taken": (r.context or {}).get("action_taken"),
            "halted": r.halted,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.post("/kill")
async def kill_switch(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Engage the kill switch: flatten everything + disable trading."""
    cb.trip_manual_kill(db)
    closed: list[dict[str, Any]] = []
    tm = getattr(getattr(request.app, "state", None), "trade_manager", None)
    if tm is not None:
        try:
            closed = await tm.close_all(reason="KILL_SWITCH")
        except Exception:  # noqa: BLE001
            log.exception("risk.kill_switch.flatten_failed")
    actor = (request.headers.get("x-actor") or "ui").strip().lower() or "ui"
    db.add(AuditLog(
        actor=actor if actor in {"ui", "api", "system"} else "ui",
        action="risk.kill_switch",
        target="risk_state",
        after={"closed_positions": len(closed)},
    ))
    db.commit()
    await event_bus.publish(
        "risk.kill_switch",
        {"reason": "manual", "closed_positions": len(closed)},
    )
    log.warning("risk.kill_switch.engaged", closed_positions=len(closed))
    return {
        "ok": True,
        "trading_disabled": True,
        "closed_positions": len(closed),
        "closed": closed,
    }


@router.post("/resume")
async def resume(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Clear the halt / cooldown and allow trading again."""
    cb.clear_halt(db)
    actor = (request.headers.get("x-actor") or "ui").strip().lower() or "ui"
    db.add(AuditLog(
        actor=actor if actor in {"ui", "api", "system"} else "ui",
        action="risk.resume",
        target="risk_state",
        after={"trading_disabled": False},
    ))
    db.commit()
    await event_bus.publish("risk.resumed", {})
    return {"ok": True, "trading_disabled": False}
