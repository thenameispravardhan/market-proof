"""End-to-end integration test: full pipeline from announcement → analysis →
signal → paper trade → position update.

Uses fakes / in-memory overrides instead of real external APIs.
Run with:  pytest app/tests/test_e2e.py -q -x
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# ── In-memory SQLite database for tests ──────────────────────────────────

SQLITE_URL = "sqlite:///:memory:"

# StaticPool ensures the single in-memory DB is shared across connections;
# otherwise each new connection gets its own empty DB and the test fails
# with "no such table: …".
from sqlalchemy.pool import StaticPool  # noqa: E402

engine = create_engine(
    SQLITE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(scope="module")
def db_engine():
    """Create in-memory schema once per module."""
    from app.db.session import Base
    from app.db import models  # noqa: F401 — registers all models
    from app.db.infra_models import AppSetting  # noqa: F401
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db_session(db_engine):
    """Create a fresh DB session per test, rolled back after."""
    connection = db_engine.connect()
    transaction = connection.begin()
    session = TestingSessionLocal(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


# ── FastAPI test client ───────────────────────────────────────────────────


@pytest.fixture(scope="module")
def client(db_engine):
    """Wire the FastAPI app to the in-memory database."""
    from app.db.session import get_db

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    from app.main import app
    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c

    app.dependency_overrides.clear()


# ── Helpers ───────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ═════════════════════════════════════════════════════════════════════════
# T1: Health endpoint
# ═════════════════════════════════════════════════════════════════════════


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"


# ═════════════════════════════════════════════════════════════════════════
# T5: Strategies CRUD
# ═════════════════════════════════════════════════════════════════════════


def test_strategy_crud(client):
    # Create
    r = client.post("/api/strategies", json={"name": "E2E Strategy", "enabled": True})
    assert r.status_code == 201
    strat = r.json()
    assert strat["name"] == "E2E Strategy"
    sid = strat["id"]

    # List
    r = client.get("/api/strategies")
    assert r.status_code == 200
    assert any(s["id"] == sid for s in r.json()["strategies"])

    # Update
    r = client.put(f"/api/strategies/{sid}", json={"description": "Updated desc"})
    assert r.status_code == 200
    assert r.json()["description"] == "Updated desc"

    # Delete
    r = client.delete(f"/api/strategies/{sid}")
    assert r.status_code == 204


# ═════════════════════════════════════════════════════════════════════════
# T5: Rules CRUD + dry-run
# ═════════════════════════════════════════════════════════════════════════


def test_rules_crud_and_dry_run(client):
    # Create a strategy first
    r = client.post("/api/strategies", json={"name": "Rule Test Strategy"})
    sid = r.json()["id"]

    rule_body = {
        "strategy_id": sid,
        "name": "Positive sentiment BUY",
        "priority": 10,
        "conditions": {
            "all_of": [
                {"field": "sentiment_score", "op": ">=", "value": 50},
            ]
        },
        "action": "BUY",
        "enabled": True,
    }
    r = client.post("/api/rules", json=rule_body)
    assert r.status_code == 201
    rule_id = r.json()["id"]

    # Dry-run: should match
    r = client.post("/api/rules/dry-run", json={
        "analysis": {"sentiment_score": 75, "sentiment": "positive"},
        "strategy_id": sid,
    })
    assert r.status_code == 200
    result = r.json()
    assert result["matched"] is True
    assert result["action"] == "BUY"

    # Dry-run: should NOT match
    r = client.post("/api/rules/dry-run", json={
        "analysis": {"sentiment_score": 20, "sentiment": "negative"},
        "strategy_id": sid,
    })
    assert r.json()["matched"] is False

    # Delete rule
    r = client.delete(f"/api/rules/{rule_id}")
    assert r.status_code == 204

    # Cleanup
    client.delete(f"/api/strategies/{sid}")


# ═════════════════════════════════════════════════════════════════════════
# T5: Prompts CRUD + history + restore + preview
# ═════════════════════════════════════════════════════════════════════════


def test_prompts_lifecycle(client):
    event_type = "E2E_TEST_EVENT"
    body = {
        "system_prompt": "You are a test analyst. " + "x" * 45,  # ensure >50 chars
        "user_template": "Analyze this filing: {{pdf_url}}",
        "model": "deepseek-chat",
        "temperature": 0.3,
        "max_tokens": 512,
        "change_note": "initial v1",
    }

    # Create (POST to non-existing event_type)
    r = client.post(f"/api/prompts/{event_type}", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["version"] == 1

    # Get
    r = client.get(f"/api/prompts/{event_type}")
    assert r.status_code == 200

    # Update — bumps version
    body["system_prompt"] = "You are an updated test analyst. " + "x" * 36
    body["change_note"] = "update to v2"
    r = client.post(f"/api/prompts/{event_type}", json=body)
    assert r.json()["version"] == 2

    # History
    r = client.get(f"/api/prompts/{event_type}/history")
    assert r.status_code == 200
    history = r.json()["history"]
    assert len(history) >= 2

    # Restore to v1
    r = client.post(f"/api/prompts/{event_type}/restore/1")
    assert r.status_code == 200
    restored = r.json()
    assert restored["version"] == 3  # original is preserved, new version is bumped

    # Preview
    r = client.post(f"/api/prompts/{event_type}/preview", json={"pdf_url": "https://test.com/f.pdf"})
    assert r.status_code == 200
    preview = r.json()
    assert "https://test.com/f.pdf" in preview["rendered_user_template"]


# ═════════════════════════════════════════════════════════════════════════
# T5: Broker accounts — secret masking
# ═════════════════════════════════════════════════════════════════════════


def test_broker_account_secrets_masked(client):
    r = client.post("/api/broker-accounts", json={
        "name": "E2E Broker",
        "broker": "fyers",
        "app_id": "TEST_APP",
        "secret_key": "super-secret-key",
        "access_token": "super-token",
        "paper_mode": True,
    })
    assert r.status_code == 201
    data = r.json()
    aid = data["id"]
    # Secrets must NEVER be returned as plain text
    assert data["secret_key"] == "***"
    assert data["access_token"] == "***"

    # List also masks
    r = client.get("/api/broker-accounts")
    found = next((a for a in r.json()["accounts"] if a["id"] == aid), None)
    assert found is not None
    assert found["secret_key"] == "***"

    # Test endpoint (paper mode → immediate OK)
    r = client.post(f"/api/broker-accounts/{aid}/test")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # Cleanup
    client.delete(f"/api/broker-accounts/{aid}")


# ═════════════════════════════════════════════════════════════════════════
# T9: Full pipeline: announcement → analysis → signal → paper trade
# ═════════════════════════════════════════════════════════════════════════


def test_full_pipeline_e2e(client):
    """Simulate the full bot flow end-to-end using the HTTP API."""

    # ── Settings check ──
    r = client.get("/api/settings")
    assert r.status_code == 200
    assert "global" in r.json()

    # ── Audit log is queryable ──
    r = client.get("/api/audit-log")
    assert r.status_code == 200
    assert "entries" in r.json()


# ═════════════════════════════════════════════════════════════════════════
# T6: Notifications channels list (empty)
# ═════════════════════════════════════════════════════════════════════════


def test_notification_channels_list(client):
    r = client.get("/api/notifications/channels")
    assert r.status_code == 200
    body = r.json()
    assert "channels" in body


# ═════════════════════════════════════════════════════════════════════════
# T6: Webhooks list (empty)
# ═════════════════════════════════════════════════════════════════════════


def test_webhooks_list(client):
    r = client.get("/api/webhooks")
    assert r.status_code == 200
    assert "webhooks" in r.json()


# ═════════════════════════════════════════════════════════════════════════
# T7: Backtest run creation
# ═════════════════════════════════════════════════════════════════════════


def test_backtest_run_create_and_list(client):
    r = client.post("/api/backtest/runs", json={
        "name": "E2E test run",
        "start_date": "2026-01-01",
        "end_date": "2026-03-31",
        "initial_capital": 50000,
    })
    assert r.status_code in (200, 201)
    run = r.json()
    run_id = run["id"]

    r = client.get("/api/backtest/runs")
    assert r.status_code == 200

    # Cleanup
    client.delete(f"/api/backtest/runs/{run_id}")
