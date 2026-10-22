"""B6: nightly re-validation of switched-on strategies, and the flags it raises."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.research import revalidate as rv
from app.tests.test_research_loop import GRID, _ema, _walk

SAT_19 = int(datetime(2025, 3, 15, 13, 30, tzinfo=timezone.utc).timestamp())   # Saturday 19:00 IST
TUE_19 = int(datetime(2025, 3, 18, 13, 30, tzinfo=timezone.utc).timestamp())   # Tuesday 19:00 IST
TUE_11 = int(datetime(2025, 3, 18, 5, 30, tzinfo=timezone.utc).timestamp())    # Tuesday 11:00 IST


@pytest.fixture()
def cfg_root(tmp_path, monkeypatch):
    monkeypatch.setattr(rv, "ROOT", tmp_path)
    return tmp_path


def test_config_is_off_by_default_and_never_set_into_market_hours(cfg_root):
    assert rv.load_config() == {"enabled": False, "time": "18:30"}
    assert rv.save_config({"enabled": True, "time": "7:05"})["time"] == "07:05"
    for bad in ("11:00", "15:40", "25:00", "noon"):
        with pytest.raises(ValueError):
            rv.save_config({"time": bad})
    assert rv.in_market(TUE_11) and not rv.in_market(TUE_19) and not rv.in_market(SAT_19)


def test_the_tick_runs_once_a_weekday_after_its_time(cfg_root, monkeypatch):
    ran = []

    async def fake(now=None, heavy=None):
        ran.append(now)
        return {"ok": True}

    monkeypatch.setattr(rv, "run_nightly", fake)
    assert asyncio.run(rv.tick(TUE_19)) is None                 # off
    rv.save_config({"enabled": True, "time": "18:30"})
    assert asyncio.run(rv.tick(TUE_19 - 3600)) is None          # 18:00: not yet
    assert asyncio.run(rv.tick(TUE_19)) == {"ok": True}
    assert asyncio.run(rv.tick(TUE_19 + 600)) is None           # once a day
    assert asyncio.run(rv.tick(SAT_19)) is None                 # not at weekends
    assert ran == [TUE_19]


def test_judge():
    res = {"verdict": "pass", "checks": [], "min_trades": 10}
    assert rv.judge(res, None, "paper", True) == []
    assert rv.judge(res, None, "live", False) == ["trades LIVE without ever passing a research check"]
    killed = {"verdict": "kill", "min_trades": 10, "checks": [{"label": "Deflated Sharpe ≥ 95%", "pass": False},
                                                             {"label": "PBO", "pass": None}]}
    assert rv.judge(killed, None, "paper", True) == ["below the bar on the latest data: Deflated Sharpe ≥ 95%"]
    lost = rv.judge(res, {"trades": 12, "net_pnl": -800.0}, "paper", True)
    assert lost == ["lost ₹800 over 12 trades since its passing card"]
    assert rv.judge(res, {"trades": 4, "net_pnl": -800.0}, "paper", True) == []     # too few to say


def _fake_assemble(monkeypatch, holder):
    from app.algo import data
    from app.tests.test_algo import DAY0

    async def fake(spec, start, end):
        return {s: holder["d"] for s in spec["symbols"]}, [], DAY0

    monkeypatch.setattr(data, "assemble", fake)


def test_a_strategy_that_stops_holding_is_flagged_once_and_reported(client, cfg_root, monkeypatch, isolated_db):
    from app.db import session as dbs
    from app.db.models import AlgoResearchCard, AlgoResearchFlag, AlgoStrategy
    from app.services.event_bus import event_bus

    holder = {"d": _walk(60, 0.02)}
    _fake_assemble(monkeypatch, holder)
    sent = []

    async def publish(ch, payload):
        sent.append((ch, payload))
        return 1

    monkeypatch.setattr(event_bus, "publish", publish)
    spec = _ema()
    s = client.post("/api/algo/strategies", json={"name": "ema", "spec": spec}).json()
    card = client.post("/api/algo/research/check", json={"spec": spec, "start": "2025-01-01", "end": "2025-03-01",
                                                         "grid": GRID, "min_trades": 5, "strategy_id": s["id"]}).json()
    assert card["verdict"] == "pass"
    assert client.put(f"/api/algo/strategies/{s['id']}", json={"enabled": True}).status_code == 200
    off = client.post("/api/algo/strategies", json={"name": "off", "spec": spec}).json()       # not switched on: skipped

    first = asyncio.run(rv.run_nightly(SAT_19, asyncio.Semaphore(1)))
    assert first["strategies"] == 1 and first["results"][0]["status"] == "holds" and first["new_flags"] == 0
    with dbs.SessionLocal() as db:
        nightly = db.query(AlgoResearchCard).filter_by(kind="nightly").one()
        assert nightly.strategy_id == s["id"] and nightly.verdict == "pass" and nightly.setup["min_trades"] == 5
        trials = db.get(AlgoStrategy, s["id"]).trials
    assert trials == card["trials"]                                  # monitoring is not a new try

    holder["d"] = _walk(60, 0.0)                                     # the edge is gone
    second = asyncio.run(rv.run_nightly(SAT_19, asyncio.Semaphore(1)))
    r = second["results"][0]
    assert r["status"] == "flagged" and r["new_flag"] and "below the bar" in r["reasons"][0]
    mine = [p for ch, p in sent if ch == "system.error" and p.get("where") == "research nightly re-validation"]
    assert len(mine) == 1 and "ema" in mine[0]["error"]          # other services may publish errors too
    third = asyncio.run(rv.run_nightly(SAT_19, asyncio.Semaphore(1)))
    assert third["results"][0]["status"] == "flagged" and third["new_flags"] == 0       # one open flag, not one a night
    with dbs.SessionLocal() as db:
        assert db.query(AlgoResearchFlag).count() == 1
        assert [f.strategy_id for f in rv.active_flags(db)] == [s["id"]]

    body = client.get("/api/algo/research/nightly").json()
    assert body["last"]["strategies"] == 1 and body["flags"][0]["name"] == "ema"
    fid = body["flags"][0]["id"]
    cleared = client.post(f"/api/algo/research/flags/{fid}/clear", json={"note": "paused, re-research"}).json()
    assert cleared["cleared_note"] == "paused, re-research" and client.get("/api/algo/research/flags").json()["flags"] == []
    assert len(client.get("/api/algo/research/flags", params={"active": False}).json()["flags"]) == 1
    assert client.post("/api/algo/research/flags/999/clear", json={}).status_code == 404
    kinds = {c["kind"] for c in client.get("/api/algo/research/cards").json()["cards"]}
    assert kinds == {"check", "nightly"}
    assert {c["kind"] for c in client.get("/api/algo/research/cards", params={"kind": "nightly"}).json()["cards"]} == {"nightly"}
    assert off["id"] not in {x["strategy_id"] for x in second["results"]}


def test_losses_since_the_card_and_live_without_one_are_flagged(client, cfg_root, monkeypatch, isolated_db):
    from app.db import session as dbs
    from app.db.models import AlgoResearchCard, AlgoStrategy

    holder = {"d": _walk(60, 0.02)}
    _fake_assemble(monkeypatch, holder)
    spec = _ema()
    s = client.post("/api/algo/strategies", json={"name": "late", "spec": spec}).json()
    client.post("/api/algo/research/check", json={"spec": spec, "start": "2025-01-01", "end": "2025-03-01",
                                                  "min_trades": 5, "strategy_id": s["id"]})
    with dbs.SessionLocal() as db:
        c = db.query(AlgoResearchCard).one()
        c.verdict = "pass"                                   # say it passed, in mid-January
        c.created_at = datetime(2025, 1, 20)
        db.get(AlgoStrategy, s["id"]).enabled = True
        live = AlgoStrategy(name="naked", spec=spec, enabled=True, mode="live")
        db.add(live)
        db.commit()
    # after the card, the market turns against it: falling prices for the long-only EMA strategy
    import random

    from app.tests.test_algo import DAY0, bars
    rng, px, t, closes = random.Random(9), 100.0, [], []
    for k in range(60):
        for i in range(75):
            t.append(DAY0 + k * 86400 + i * 300)
            px += rng.gauss(0.02 if k < 19 else -0.03, 0.15)
            closes.append(px)
    d = bars(closes, spread=0.05)
    d["t"] = t
    holder["d"] = d
    out = asyncio.run(rv.run_nightly(SAT_19, asyncio.Semaphore(1)))
    by = {r["name"]: r for r in out["results"]}
    assert by["late"]["since_card"]["trades"] >= 5 and by["late"]["since_card"]["net_pnl"] < 0
    assert any("since its passing card" in x for x in by["late"]["reasons"])
    assert "trades LIVE without ever passing a research check" in by["naked"]["reasons"]
    assert out["new_flags"] == 2


def test_it_will_not_run_in_market_hours(client, cfg_root, monkeypatch):
    with pytest.raises(RuntimeError, match="market hours"):
        asyncio.run(rv.run_nightly(TUE_11, asyncio.Semaphore(1)))
    monkeypatch.setattr(rv, "in_market", lambda now: True)
    assert client.post("/api/algo/research/nightly/run").status_code == 409
    assert client.put("/api/algo/research/nightly", json={"time": "10:00"}).status_code == 422
    assert client.put("/api/algo/research/nightly", json={"enabled": True, "time": "18:45"}).json()["enabled"] is True
