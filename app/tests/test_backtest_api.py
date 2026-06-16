"""Tests for the /api/backtest REST API.

Coverage:
  - POST a run, GET it back, retrieve trades and equity curve.
  - GET /api/backtest/runs returns a list (the new run shows up).
  - DELETE removes the run; subsequent GET returns 404.
  - Validation: end_date < start_date → 422.
  - GET on a missing id → 404.
  - Background task populates results; the metrics dict is
    present in the detail response.
"""
from __future__ import annotations

import time
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient


# We need to ensure the engine can run in-process during the
# test. The test client runs the background task in the same
# event loop the request returns from; the engine uses asyncio
# internally. To make the test deterministic, we also expose
# a helper that runs the engine synchronously and updates the
# row, mirroring what the background task does.


def _seed_minimum(client: TestClient) -> dict:
    """Seed the minimum data needed for a backtest to produce a
    result. Inserts a default strategy + paper account + 1 rule +
    a handful of announcements. Uses the same upsert helpers as
    scripts/backtest_seed.py."""
    from app.analyzer.prompts import seed_defaults
    from app.analyzer.rules_engine import get_or_create_default_strategy
    from app.db.models import (
        Announcement,
        BrokerAccount,
        SignalRule,
        compute_content_hash,
    )
    from app.db.session import SessionLocal
    from datetime import datetime, timezone

    s = SessionLocal()
    try:
        strategy = get_or_create_default_strategy(s)
        seed_defaults(s)
        if s.query(BrokerAccount).count() == 0:
            s.add(
                BrokerAccount(
                    name="api-test-paper",
                    broker="paper",
                    paper_mode=True,
                    enabled=True,
                )
            )
            s.flush()
        if s.query(SignalRule).count() == 0:
            s.add(
                SignalRule(
                    strategy_id=int(strategy.id),
                    name="default-buy-positive",
                    priority=10,
                    conditions={
                        "all_of": [
                            {"field": "sentiment_score", "op": ">=", "value": 50.0},
                            {"field": "confidence", "op": ">=", "value": 0.7},
                        ],
                    },
                    action="BUY",
                    action_params={"position_size_pct": 5.0},
                    enabled=True,
                )
            )
            s.flush()
        # 3 announcements across the last 3 days.
        today = date.today()
        for d in range(3):
            filed = datetime.combine(
                today - timedelta(days=d), datetime.min.time()
            ).replace(tzinfo=timezone.utc, hour=10)
            for sym in ("RELIANCE", "TCS"):
                pdf = f"https://synth.local/{sym}/{filed.date().isoformat()}.pdf"
                ch = compute_content_hash(
                    exchange="NSE", symbol=sym, filed_at=filed, pdf_url=pdf,
                )
                existing = (
                    s.query(Announcement).filter_by(content_hash=ch).one_or_none()
                )
                if existing is not None:
                    continue
                s.add(
                    Announcement(
                        content_hash=ch,
                        symbol=sym,
                        exchange="NSE",
                        event_type="ORDER_WIN",
                        headline=(
                            f"{sym} wins a Rs 1000 crore order from a "
                            f"leading client on day {d}"
                        ),
                        body="synth body",
                        pdf_url=pdf,
                        source="synthetic",
                        filed_at=filed,
                        received_at=filed,
                    )
                )
        s.commit()
    finally:
        s.close()


def _wait_for_run_done(client: TestClient, run_id: int, timeout_s: float = 10.0) -> dict:
    """Poll GET /api/backtest/runs/{id} until status flips from
    'pending' / 'running' to 'done' or 'failed'."""
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        r = client.get(f"/api/backtest/runs/{run_id}")
        if r.status_code == 200:
            last = r.json()
            if last["status"] in ("done", "failed"):
                return last
        time.sleep(0.1)
    raise AssertionError(f"backtest run {run_id} did not finish: {last}")


# ---- Tests -------------------------------------------------------------


def test_create_and_get_run(client: TestClient):
    _seed_minimum(client)
    end = date.today()
    start = end - timedelta(days=3)
    r = client.post(
        "/api/backtest/runs",
        json={
            "name": "api-test-1",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "initial_capital": 150_000.0,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    run_id = body["id"]
    assert body["status"] in ("pending", "running", "done")
    assert body["name"] == "api-test-1"
    assert body["initial_capital"] == 150_000.0
    # Poll for completion.
    final = _wait_for_run_done(client, run_id)
    assert final["status"] == "done", final
    # Detail includes metrics.
    assert final["metrics"] is not None
    assert "total_return" in final["metrics"]
    assert "sharpe_ratio" in final["metrics"]
    assert final["announcements_processed"] is not None


def test_list_runs_filter_by_strategy_id(client: TestClient):
    _seed_minimum(client)
    end = date.today()
    start = end - timedelta(days=3)
    # Create a run with strategy_id=1 (the default seeded strategy).
    r = client.post(
        "/api/backtest/runs",
        json={
            "name": "filter-test",
            "strategy_id": 1,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "initial_capital": 100_000.0,
        },
    )
    assert r.status_code == 201, r.text
    run_id = r.json()["id"]
    _wait_for_run_done(client, run_id)
    # Filter by strategy_id=1 — the run should appear.
    r = client.get("/api/backtest/runs?strategy_id=1")
    assert r.status_code == 200
    runs = r.json()
    assert any(x["id"] == run_id for x in runs)
    # Filter by strategy_id=99999 (no such strategy) → empty.
    r = client.get("/api/backtest/runs?strategy_id=99999")
    assert r.status_code == 200
    assert r.json() == []


def test_list_runs_filter_by_status(client: TestClient):
    _seed_minimum(client)
    end = date.today()
    start = end - timedelta(days=3)
    r = client.post(
        "/api/backtest/runs",
        json={
            "name": "status-filter-test",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "initial_capital": 100_000.0,
        },
    )
    run_id = r.json()["id"]
    _wait_for_run_done(client, run_id)
    r = client.get("/api/backtest/runs?status=done")
    assert r.status_code == 200
    runs = r.json()
    assert all(x["status"] == "done" for x in runs)
    assert any(x["id"] == run_id for x in runs)


def test_list_runs_includes_new_run(client: TestClient):
    _seed_minimum(client)
    end = date.today()
    start = end - timedelta(days=3)
    r = client.post(
        "/api/backtest/runs",
        json={
            "name": "api-list-test",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "initial_capital": 100_000.0,
        },
    )
    assert r.status_code == 201
    run_id = r.json()["id"]
    _wait_for_run_done(client, run_id)
    # Now list.
    r = client.get("/api/backtest/runs")
    assert r.status_code == 200
    runs = r.json()
    ids = [x["id"] for x in runs]
    assert run_id in ids


def test_get_trades_and_equity_curve(client: TestClient):
    _seed_minimum(client)
    end = date.today()
    start = end - timedelta(days=3)
    r = client.post(
        "/api/backtest/runs",
        json={
            "name": "api-curves",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "initial_capital": 100_000.0,
        },
    )
    run_id = r.json()["id"]
    final = _wait_for_run_done(client, run_id)
    assert final["status"] == "done"

    # Trades.
    r = client.get(f"/api/backtest/runs/{run_id}/trades")
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == run_id
    assert "trades" in body
    # Equity curve.
    r = client.get(f"/api/backtest/runs/{run_id}/equity-curve")
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == run_id
    assert "curve" in body
    assert len(body["curve"]) >= 2


def test_backtest_run_does_not_pollute_live_endpoints(client: TestClient):
    """A finished backtest must not add anything to the endpoints the
    Dashboard / Trade History / Positions panels read. We compare the
    row counts before and after the run (a delta of 0), so the check
    is robust to whatever the rest of the session left in the DB."""
    _seed_minimum(client)

    def _count(path: str) -> int:
        r = client.get(path)
        assert r.status_code == 200, r.text
        return len(r.json())

    before = {
        "trades": _count("/api/trades?limit=1000"),
        "positions": _count("/api/positions"),
        "signals": _count("/api/signals/recent?limit=200"),
        "analyses": _count("/api/analyses/recent?limit=100"),
    }

    end = date.today()
    start = end - timedelta(days=3)
    r = client.post(
        "/api/backtest/runs",
        json={
            "name": "isolation-api",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "initial_capital": 100_000.0,
        },
    )
    assert r.status_code == 201, r.text
    run_id = r.json()["id"]
    final = _wait_for_run_done(client, run_id)
    assert final["status"] == "done", final
    # The run genuinely exercised the pipeline inside its sandbox...
    assert (final.get("signals_generated") or 0) > 0, final

    after = {
        "trades": _count("/api/trades?limit=1000"),
        "positions": _count("/api/positions"),
        "signals": _count("/api/signals/recent?limit=200"),
        "analyses": _count("/api/analyses/recent?limit=100"),
    }
    assert after == before, (
        f"backtest leaked into live tables: before={before} after={after}"
    )
    # ...and the results are reachable under the backtest run itself.
    trades = client.get(f"/api/backtest/runs/{run_id}/trades").json()
    assert "trades" in trades


def test_get_run_404(client: TestClient):
    r = client.get("/api/backtest/runs/999999")
    assert r.status_code == 404


def test_get_trades_404(client: TestClient):
    r = client.get("/api/backtest/runs/999999/trades")
    assert r.status_code == 404


def test_get_equity_curve_404(client: TestClient):
    r = client.get("/api/backtest/runs/999999/equity-curve")
    assert r.status_code == 404


def test_validation_end_before_start(client: TestClient):
    r = client.post(
        "/api/backtest/runs",
        json={
            "name": "bad-dates",
            "start_date": "2025-06-10",
            "end_date": "2025-06-01",
            "initial_capital": 100_000.0,
        },
    )
    assert r.status_code == 422


def test_validation_initial_capital_must_be_positive(client: TestClient):
    r = client.post(
        "/api/backtest/runs",
        json={
            "name": "bad-cap",
            "start_date": "2025-06-01",
            "end_date": "2025-06-10",
            "initial_capital": -1.0,
        },
    )
    assert r.status_code == 422


def test_validation_unknown_strategy_id(client: TestClient):
    r = client.post(
        "/api/backtest/runs",
        json={
            "name": "bad-strategy",
            "strategy_id": 999999,
            "start_date": "2025-06-01",
            "end_date": "2025-06-10",
            "initial_capital": 100_000.0,
        },
    )
    assert r.status_code == 404


def test_delete_run(client: TestClient):
    _seed_minimum(client)
    end = date.today()
    start = end - timedelta(days=3)
    r = client.post(
        "/api/backtest/runs",
        json={
            "name": "api-delete-test",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "initial_capital": 100_000.0,
        },
    )
    run_id = r.json()["id"]
    r = client.delete(f"/api/backtest/runs/{run_id}")
    assert r.status_code == 204
    # Subsequent GET → 404.
    r = client.get(f"/api/backtest/runs/{run_id}")
    assert r.status_code == 404


def test_delete_run_404(client: TestClient):
    r = client.delete("/api/backtest/runs/999999")
    assert r.status_code == 404
