"""Pre-registered evaluation windows and the per-signal "why" card."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import EvaluationWindow, RiskEvent, SignalRule, Strategy
from app.research import windows
from app.tests.factories import seed_signal_chain


def test_snapshot_ignores_non_decision_settings(db_session, isolated_db, monkeypatch):
    snap = windows.snapshot(db_session)
    assert "MAX_CAPITAL_RISK_PCT" in snap["settings"]
    assert not any(k.startswith(("LOG_LEVEL", "BACKUP_", "LLM_SHADOW_")) for k in snap["settings"])
    assert windows.digest(snap) == windows.digest(windows.snapshot(db_session))


@pytest.fixture()
def restore_env():
    """PUT /api/settings writes overrides into os.environ; put it back."""
    import os

    from app.config import get_settings

    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)
    get_settings.cache_clear()


def test_window_stays_valid_until_a_decision_setting_changes(client, db_session, isolated_db, restore_env):
    r = client.post("/api/research/windows", json={"name": "oct", "hypothesis": "taken trades E[R] > 0", "weeks": 6})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["frozen"] and body["evidence_verdict"].startswith("valid")
    # A second active window is refused.
    assert client.post("/api/research/windows", json={"name": "x", "hypothesis": "y", "weeks": 4}).status_code == 400

    # A telemetry-only change does not invalidate it...
    client.put("/api/settings", json={"global": {"LOG_LEVEL": "DEBUG"}})
    assert client.get("/api/research/windows").json()["windows"][0]["frozen"] is True
    # ...a risk setting does, and the report names it.
    client.put("/api/settings", json={"global": {"MAX_CAPITAL_RISK_PCT": 0.5}})
    w = client.get("/api/research/windows").json()["windows"][0]
    assert w["frozen"] is False and w["evidence_verdict"].startswith("INVALID")
    assert any("MAX_CAPITAL_RISK_PCT" in c for c in w["violation"]["changes"])
    client.put("/api/settings", json={"global": {"MAX_CAPITAL_RISK_PCT": 0.75, "LOG_LEVEL": "INFO"}})
    # Reverting does not erase the first violation.
    assert client.get("/api/research/windows").json()["windows"][0]["frozen"] is False
    closed = client.post(f"/api/research/windows/{w['id']}/close").json()
    assert closed["status"] == "closed"


def test_rule_changes_are_detected(db_session, isolated_db):
    strat = Strategy(name="s")
    db_session.add(strat)
    db_session.flush()
    rule = SignalRule(strategy_id=strat.id, name="r", priority=1, conditions={"all": []}, action="BUY")
    db_session.add(rule)
    db_session.commit()
    w = windows.start(db_session, name="w", hypothesis="h", weeks=4)
    rule.action = "HOLD"
    db_session.commit()
    v = windows.check(db_session, w)
    assert v is not None and any("rule" in c for c in v["changes"])


def test_changes_after_the_window_ended_do_not_count(db_session, isolated_db):
    w = windows.start(db_session, name="w", hypothesis="h", weeks=1)
    w.ends_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    db_session.commit()
    strat = Strategy(name="late")
    db_session.add(strat)
    db_session.commit()
    assert windows.check(db_session, w) is None


def test_window_validation(db_session, isolated_db):
    with pytest.raises(ValueError):
        windows.start(db_session, name="w", hypothesis="", weeks=6)
    with pytest.raises(ValueError):
        windows.start(db_session, name="w", hypothesis="h", weeks=52)


def test_why_card_collects_the_whole_decision(client, db_session, isolated_db):
    chain = seed_signal_chain(db_session, status="blocked", rationale="ORDER_WIN rule | blocked: HIGH_VIX (vix 31)",
                              move_5m=0.8, move_30m=2.1, headline="ACME bags Rs 500 crore order")
    sig = chain["signal"]
    db_session.add(RiskEvent(event_type="HIGH_VIX", severity="warning", message="India VIX 31 > 30",
                             context={"signal_id": sig.id}, halted=False, created_at=sig.created_at))
    db_session.commit()
    body = client.get(f"/api/research/why/{sig.id}").json()
    assert body["block_reason"] == "HIGH_VIX"
    assert body["filing"]["headline"] == "ACME bags Rs 500 crore order"
    assert body["analysis"]["recommendation"] == "buy"
    assert body["risk_checks_blocked"][0]["code"] == "HIGH_VIX"
    assert body["outcome"]["move_30m_pct"] == 2.1
    assert body["signal"]["rule_rationale"] == "ORDER_WIN rule"
    assert client.get("/api/research/why/999999").status_code == 404
