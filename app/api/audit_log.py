"""`/api/audit-log` — queryable audit trail.

GET /api/audit-log?action=&target=&actor=&limit=50
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.init import init_db
from app.db.models import AuditLog
from app.db.session import get_db

router = APIRouter(prefix="/api/audit-log", tags=["audit"])
init_db()


def _ser(e: AuditLog) -> dict[str, Any]:
    return {
        "id": e.id,
        "actor": e.actor,
        "action": e.action,
        "target": e.target,
        "before": e.before,
        "after": e.after,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


@router.get("")
def list_audit_log(
    action: Optional[str] = Query(None, description="filter by action prefix"),
    target: Optional[str] = Query(None, description="filter by target prefix"),
    actor: Optional[str] = Query(None, description="filter by actor"),
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc())
    if action:
        stmt = stmt.where(AuditLog.action.startswith(action))
    if target:
        stmt = stmt.where(AuditLog.target.startswith(target))
    if actor:
        stmt = stmt.where(AuditLog.actor == actor)
    stmt = stmt.limit(limit)
    rows = db.execute(stmt).scalars().all()
    return {"entries": [_ser(e) for e in rows], "count": len(rows)}
