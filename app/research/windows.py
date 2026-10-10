"""Pre-registered evaluation windows: live paper results that count as
out-of-sample evidence.

A backtest can be tuned until it looks good. A live window can be too, if
the configuration keeps changing while it runs. A window is declared
up front (hypothesis, start, length) together with a hash of every
decision-relevant input: effective settings, signal rules, strategy risk
overrides and prompt templates. While it runs, `check()` recomputes the
hash. The report says plainly whether the window is still frozen and, if
not, which keys moved.

Settings that cannot change a trading decision (logging, telemetry,
backups, alarms, shadow scoring) are left out of the hash, so turning on
an alarm does not invalidate a month of evidence.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    EvaluationWindow,
    PromptTemplate,
    Signal,
    SignalOutcome,
    SignalRule,
    Strategy,
    Trade,
)
from app.research import stats

# Keys that do not influence what the bot trades or how.
NON_DECISION_PREFIXES = (
    "LOG_LEVEL", "RESOURCE_", "HEALTH_REPORT_", "DATASET_", "BACKUP_", "OUTCOME_LOGGER_",
    "FYERS_SELFTEST_", "FYERS_REQUIRED_APP_TYPE", "FYERS_WHITELISTED_IPS", "FYERS_EGRESS_IP_URL",
    "LLM_SHADOW_", "QUOTE_REFRESH_SECONDS", "POSITION_RECONCILE_SECONDS", "FYERS_STREAMING_ENABLED",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def snapshot(db: Session) -> dict[str, Any]:
    """Every decision-relevant input, as plain JSON."""
    from app.api.settings_api import get_settings_endpoint
    from app.config import get_settings

    effective = get_settings_endpoint(db=db, settings=get_settings())["global"]
    settings = {k: v for k, v in sorted(effective.items()) if not k.startswith(NON_DECISION_PREFIXES)}
    rules = [
        {"id": r.id, "strategy_id": r.strategy_id, "name": r.name, "priority": r.priority,
         "conditions": r.conditions, "action": r.action, "action_params": r.action_params,
         "enabled": r.enabled}
        for r in db.execute(select(SignalRule).order_by(SignalRule.id)).scalars()
    ]
    strategies = [
        {"id": s.id, "name": s.name, "enabled": s.enabled, "config": s.config}
        for s in db.execute(select(Strategy).order_by(Strategy.id)).scalars()
    ]
    prompts = [
        {"event_type": p.event_type, "model": p.model, "temperature": p.temperature,
         "max_tokens": p.max_tokens,
         "system_sha": hashlib.sha256((p.system_prompt or "").encode()).hexdigest(),
         "user_sha": hashlib.sha256((p.user_template or "").encode()).hexdigest(),
         "reasoning_effort": getattr(p, "reasoning_effort", None),
         "thinking_enabled": getattr(p, "thinking_enabled", None)}
        for p in db.execute(select(PromptTemplate).order_by(PromptTemplate.event_type)).scalars()
    ]
    return {"settings": settings, "rules": rules, "strategies": strategies, "prompts": prompts}


def digest(snap: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(snap, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def diff(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Human-readable list of what changed between two snapshots."""
    out: list[str] = []
    os_, ns = old.get("settings", {}), new.get("settings", {})
    for k in sorted(set(os_) | set(ns)):
        if os_.get(k) != ns.get(k):
            out.append(f"setting {k}: {os_.get(k)!r} -> {ns.get(k)!r}")
    for section, key in (("rules", "id"), ("strategies", "id"), ("prompts", "event_type")):
        o = {x[key]: x for x in old.get(section, [])}
        n = {x[key]: x for x in new.get(section, [])}
        for k in sorted(set(o) | set(n), key=str):
            if k not in n:
                out.append(f"{section[:-1]} {k} removed")
            elif k not in o:
                out.append(f"{section[:-1]} {k} added")
            elif o[k] != n[k]:
                out.append(f"{section[:-1]} {k} changed")
    return out


def start(db: Session, *, name: str, hypothesis: str, weeks: int) -> EvaluationWindow:
    if not 1 <= weeks <= 26:
        raise ValueError("a window runs 1 to 26 weeks (4-8 is the useful range)")
    if not hypothesis.strip():
        raise ValueError("write the hypothesis down before the window starts")
    active = db.execute(select(EvaluationWindow).where(EvaluationWindow.status == "active")).scalars().first()
    if active is not None:
        raise ValueError(f"window {active.id} ({active.name!r}) is still active; close it first")
    snap = snapshot(db)
    now = _utcnow()
    w = EvaluationWindow(name=name.strip()[:128], hypothesis=hypothesis.strip(), started_at=now,
                         ends_at=now + timedelta(weeks=weeks), config_hash=digest(snap),
                         config_snapshot=snap, status="active")
    db.add(w)
    db.commit()
    db.refresh(w)
    return w


def check(db: Session, w: EvaluationWindow) -> Optional[dict[str, Any]]:
    """Record the FIRST configuration change seen while the window runs.
    Runs on every report, at close, and daily from the health report, so a
    change is caught within a day even if nobody opens the page. Changes
    after `ends_at` are not violations: the window is over."""
    if w.status != "active" or w.violation is not None or _utcnow() > w.ends_at:
        return w.violation
    current = snapshot(db)
    if digest(current) != w.config_hash:
        w.violation = {"at": _utcnow().isoformat(), "changes": diff(w.config_snapshot, current)[:50]}
        db.commit()
    return w.violation


def check_active(db: Session) -> Optional[dict[str, Any]]:
    w = db.execute(select(EvaluationWindow).where(EvaluationWindow.status == "active")).scalars().first()
    return check(db, w) if w is not None else None


def close(db: Session, window_id: int, *, abandon: bool = False) -> EvaluationWindow:
    w = db.get(EvaluationWindow, window_id)
    if w is None:
        raise LookupError(f"window {window_id} not found")
    if w.status != "active":
        raise ValueError(f"window {window_id} is already {w.status}")
    check(db, w)
    w.status = "abandoned" if abandon else "closed"
    w.closed_at = _utcnow()
    db.commit()
    db.refresh(w)
    return w


def report(db: Session, w: EvaluationWindow) -> dict[str, Any]:
    """What happened inside the window, and whether it is still evidence."""
    check(db, w)
    end = min(_utcnow(), w.closed_at or w.ends_at, w.ends_at)

    def count(*conds) -> int:
        return int(db.execute(select(func.count()).select_from(Signal)
                              .where(Signal.created_at >= w.started_at, Signal.created_at < end, *conds))
                   .scalar_one() or 0)

    exits = db.execute(
        select(Trade.pnl, Trade.r_multiple).where(
            Trade.created_at >= w.started_at, Trade.created_at < end,
            Trade.status == "filled", Trade.pnl.isnot(None))
    ).all()
    pnl = [float(p) for p, _ in exits]
    rs = [float(r) for _, r in exits if r is not None]
    moves = db.execute(
        select(SignalOutcome.action, SignalOutcome.move_30m_pct, SignalOutcome.signal_status).where(
            SignalOutcome.created_at >= w.started_at, SignalOutcome.created_at < end,
            SignalOutcome.move_30m_pct.isnot(None))
    ).all()
    planned = (w.ends_at - w.started_at).days
    elapsed = max(0, (end - w.started_at).days)
    return {
        "id": w.id, "name": w.name, "hypothesis": w.hypothesis, "status": w.status,
        "started_at": w.started_at.isoformat(), "ends_at": w.ends_at.isoformat(),
        "days_elapsed": elapsed, "days_planned": planned,
        "config_hash": w.config_hash,
        "frozen": w.violation is None,
        "violation": w.violation,
        "evidence_verdict": (
            ("abandoned" if w.status == "abandoned" else
             "valid: configuration unchanged" if w.violation is None else
             "INVALID as out-of-sample evidence: configuration changed during the window")
        ),
        "signals": count(), "signals_blocked": count(Signal.status == "blocked"),
        "closed_trades": stats.summarize_r(rs, pnl) if rs else {"n": len(pnl), "net_pnl": round(sum(pnl), 2)},
        "outcomes_with_30m_move": len(moves),
        "complete": w.status == "closed" or _utcnow() >= w.ends_at,
    }
