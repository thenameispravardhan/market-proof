"""Audit service (buffered writer) and the audit hash chain.

The audit trail is what a regulator or examiner asks for first; these tests
pin the two properties that matter: entries actually reach the table, and a
sealed entry cannot be changed without `verify` noticing.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from sqlalchemy import select, text

from app.db import session as db_session_mod
from app.db.models import AuditLog
from app.services import audit_chain
from app.services.audit_service import AuditService, log_event


def _sf():
    return db_session_mod.SessionLocal


@pytest.mark.asyncio
async def test_buffered_entries_reach_the_table_and_are_sealed(isolated_db):
    svc = AuditService(session_factory=_sf())
    svc.start()
    svc.log("system", "test.one", "x:1", after={"a": 1})
    svc.log("system", "test.two", "x:2", before={"when": "t"})
    await asyncio.sleep(0)            # let call_soon_threadsafe append
    svc.stop()
    await svc.wait_until_stopped()
    with _sf()() as db:
        rows = db.execute(select(AuditLog).order_by(AuditLog.id)).scalars().all()
        assert [r.action for r in rows] == ["test.one", "test.two"]
        assert all(r.row_hash for r in rows)
        assert rows[1].prev_hash == rows[0].row_hash
        assert rows[0].prev_hash == audit_chain.GENESIS


@pytest.mark.asyncio
async def test_log_event_makes_payloads_json_safe(isolated_db, monkeypatch):
    """The analyzer passes datetimes in signal payloads; one of those used to
    be enough to fail a whole batch insert."""
    import app.services.audit_service as mod

    svc = AuditService(session_factory=_sf())
    monkeypatch.setattr(mod, "audit_service", svc)
    svc.start()
    log_event(actor="system", action="signal.created", target="signal:1",
              after={"created_at": datetime(2026, 10, 12, 4, 0)})
    await asyncio.sleep(0)
    svc.stop()
    await svc.wait_until_stopped()
    with _sf()() as db:
        row = db.execute(select(AuditLog)).scalars().one()
        assert row.after == {"created_at": "2026-10-12 04:00:00"}


def _seed(db, n):
    for i in range(n):
        db.add(AuditLog(actor="api", action=f"act.{i}", target=f"t:{i}", after={"i": i}))
    db.commit()


def test_verify_passes_on_an_untouched_chain(db_session, isolated_db):
    _seed(db_session, 5)
    assert audit_chain.seal_pending(db_session) == 5
    _seed(db_session, 2)                 # later rows extend, not restart, the chain
    assert audit_chain.seal_pending(db_session) == 2
    v = audit_chain.verify_chain(db_session)
    assert v["ok"] and v["checked"] == 7 and v["unsealed_tail"] == 0
    assert v["head_hash"] == audit_chain.chain_head(db_session)[1]


def test_editing_a_sealed_row_is_detected(db_session, isolated_db):
    _seed(db_session, 4)
    audit_chain.seal_pending(db_session)
    target = db_session.execute(select(AuditLog).order_by(AuditLog.id).offset(1).limit(1)).scalars().one()
    db_session.execute(text("UPDATE audit_log SET action='forged' WHERE id=:i"), {"i": target.id})
    db_session.commit()
    db_session.expire_all()
    v = audit_chain.verify_chain(db_session)
    assert not v["ok"] and v["first_bad_id"] == target.id and "edited" in v["reason"]


def test_deleting_a_sealed_row_is_detected(db_session, isolated_db):
    _seed(db_session, 4)
    audit_chain.seal_pending(db_session)
    ids = [r.id for r in db_session.execute(select(AuditLog).order_by(AuditLog.id)).scalars()]
    db_session.execute(text("DELETE FROM audit_log WHERE id=:i"), {"i": ids[1]})
    db_session.commit()
    db_session.expire_all()
    v = audit_chain.verify_chain(db_session)
    assert not v["ok"] and v["first_bad_id"] == ids[2] and "deleted" in v["reason"]


def test_verify_endpoint(client, db_session, isolated_db):
    _seed(db_session, 3)
    body = client.get("/api/audit-log/verify").json()
    assert body["ok"] is True and body["checked"] >= 3
