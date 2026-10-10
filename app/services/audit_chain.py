"""Tamper-evident audit trail: a SHA-256 hash chain over `audit_log`.

SEBI's 2026 retail-algo framework expects a broker-grade audit trail for
algorithmic orders. Rows in a SQLite table can be edited by anyone with a
shell on the box, so the trail is made tamper-EVIDENT: every row carries

    row_hash = sha256(prev_hash || canonical(row))

where canonical(row) is a sorted-key JSON of (id, actor, action, target,
before, after, created_at). Changing, re-ordering or deleting any sealed row
changes its hash and breaks every later link, which `verify_chain` reports
with the first bad id.

Why a separate SEALER instead of hashing at insert: audit rows are written
from ~30 call sites, many inside larger transactions. Hashing at insert
would need the previous row's hash inside each of those transactions and
two concurrent writers could fork the chain. One sealer, run from the audit
service's 1-second loop, chains rows in id order after they commit. SQLite
allocates ids under its single write lock, so commit order is id order.

An attacker with the DB can still recompute the whole chain. The daily
health report therefore publishes the chain head over the notification
channels: a copy outside the box that a rewritten chain will not match.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.models import AuditLog

GENESIS = "0" * 64
SEAL_BATCH = 5000


def canonical(row: AuditLog) -> str:
    return json.dumps(
        {
            "id": row.id,
            "actor": row.actor,
            "action": row.action,
            "target": row.target,
            "before": row.before,
            "after": row.after,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        },
        sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False,
    )


def link_hash(prev_hash: str, row: AuditLog) -> str:
    return hashlib.sha256((prev_hash + canonical(row)).encode("utf-8")).hexdigest()


def chain_head(db: Session) -> tuple[Optional[int], str]:
    """(id, row_hash) of the newest sealed row; (None, GENESIS) when empty."""
    last = db.execute(
        select(AuditLog.id, AuditLog.row_hash)
        .where(AuditLog.row_hash.is_not(None))
        .order_by(AuditLog.id.desc()).limit(1)
    ).first()
    return (last[0], last[1]) if last else (None, GENESIS)


def seal_pending(db: Session, *, batch: int = SEAL_BATCH) -> int:
    """Chain every unsealed row newer than the head. Returns rows sealed."""
    head_id, prev = chain_head(db)
    stmt = select(AuditLog).where(AuditLog.row_hash.is_(None)).order_by(AuditLog.id.asc()).limit(batch)
    if head_id is not None:
        stmt = stmt.where(AuditLog.id > head_id)
    rows = db.execute(stmt).scalars().all()
    for row in rows:
        h = link_hash(prev, row)
        db.execute(update(AuditLog).where(AuditLog.id == row.id).values(prev_hash=prev, row_hash=h))
        prev = h
    if rows:
        db.commit()
    return len(rows)


def verify_chain(db: Session, *, batch: int = 5000) -> dict[str, Any]:
    """Recompute the chain from the first row. Read-only."""
    prev = GENESIS
    checked = 0
    unsealed = 0
    last_id = 0
    while True:
        rows = db.execute(
            select(AuditLog).where(AuditLog.id > last_id).order_by(AuditLog.id.asc()).limit(batch)
        ).scalars().all()
        if not rows:
            break
        for row in rows:
            last_id = row.id
            if row.row_hash is None:
                # Unsealed rows are only legitimate at the TAIL (written in
                # the last second, not yet sealed). One in the middle means a
                # sealed row after it was inserted out of order.
                unsealed += 1
                continue
            if unsealed:
                return _bad(row.id, "sealed row follows an unsealed one (inserted out of order)",
                            checked, prev)
            if row.prev_hash != prev:
                return _bad(row.id, "prev_hash does not match the previous row (row deleted or reordered)",
                            checked, prev)
            if link_hash(prev, row) != row.row_hash:
                return _bad(row.id, "row content does not match its hash (row edited)", checked, prev)
            prev = row.row_hash
            checked += 1
    return {"ok": True, "checked": checked, "unsealed_tail": unsealed, "head_hash": prev,
            "first_bad_id": None, "reason": None}


def _bad(row_id: int, reason: str, checked: int, prev: str) -> dict[str, Any]:
    return {"ok": False, "checked": checked, "unsealed_tail": None, "head_hash": prev,
            "first_bad_id": row_id, "reason": reason}
