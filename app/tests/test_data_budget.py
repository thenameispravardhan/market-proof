"""The Fyers Data-API budget: plan caps, the bulk lane's reserve, the shared day count."""
from __future__ import annotations

import asyncio
import json

import pytest

from app.execution import data_budget as db_


@pytest.fixture
def plan(monkeypatch):
    def set_(**kw):
        from app.config import get_settings

        st = get_settings()
        for k, v in {"FYERS_PLAN": "standard", "FYERS_DATA_PER_SEC": 0, "FYERS_DATA_PER_MIN": 0,
                     "FYERS_DATA_PER_DAY": 0, "FYERS_WS_SYMBOLS": 0, "FYERS_DATA_RESERVE_PCT": 20.0, **kw}.items():
            monkeypatch.setattr(st, k, v, raising=False)
    set_()
    return set_


def test_plans_and_overrides(plan):
    assert db_.limits() | {} == {"plan": "standard", "per_sec": 5, "per_min": 50, "per_day": 5000,
                                 "ws_symbols": 100, "reserve_pct": 20.0}
    plan(FYERS_PLAN="prime", FYERS_DATA_PER_DAY=123)
    lim = db_.limits()
    assert (lim["per_sec"], lim["per_min"], lim["per_day"], lim["ws_symbols"]) == (10, 500, 123, 5000)


def test_bulk_keeps_the_reserve_and_ui_never_runs_out(plan, tmp_path):
    plan(FYERS_DATA_PER_DAY=10, FYERS_DATA_PER_SEC=100, FYERS_DATA_PER_MIN=1000)
    b = db_.DataBudget(tmp_path / "b.json")

    async def go():
        with db_.bulk():
            for _ in range(8):                 # 80% of 10
                await b.acquire()
            with pytest.raises(db_.DataBudgetExhausted):
                await b.acquire()
        for _ in range(5):                     # the Trade page / orders still get theirs
            await b.acquire()
    asyncio.run(go())
    assert b.used_today() == 13 and b.refused == 1 and b.bulk_left_today() == 0


def test_the_per_second_window_spaces_calls(plan, tmp_path, monkeypatch):
    plan(FYERS_DATA_PER_SEC=2, FYERS_DATA_PER_MIN=100)
    b = db_.DataBudget(tmp_path / "b.json")
    clock = {"t": 1_800_000_000.0}
    slept = []

    async def sleep(s):
        slept.append(s)
        clock["t"] += s

    monkeypatch.setattr(db_.time, "time", lambda: clock["t"])
    monkeypatch.setattr(db_.asyncio, "sleep", sleep)

    async def go():
        for _ in range(5):
            await b.acquire()
    asyncio.run(go())
    assert len(slept) == 2 and all(0.9 < s <= 1.02 for s in slept)        # 2 per second


def test_the_bulk_lane_gets_80_percent_of_a_minute_and_backs_off_on_429(plan, tmp_path, monkeypatch):
    plan(FYERS_DATA_PER_SEC=100, FYERS_DATA_PER_MIN=10)
    b = db_.DataBudget(tmp_path / "b.json")
    clock = {"t": 1_800_000_000.0}

    async def sleep(s):
        clock["t"] += s

    monkeypatch.setattr(db_.time, "time", lambda: clock["t"])
    monkeypatch.setattr(db_.asyncio, "sleep", sleep)

    async def go():
        with db_.bulk():
            for _ in range(8):
                await b.acquire()
            t0 = clock["t"]
            await b.acquire()                   # the 9th bulk call waits for the minute to roll
            assert clock["t"] - t0 >= 59
            b.rate_limited()
            t1 = clock["t"]
            await b.acquire()
            assert clock["t"] - t1 >= db_.PENALTY_S - 0.1
    asyncio.run(go())


def test_the_day_count_is_shared_through_the_file(plan, tmp_path):
    path = tmp_path / "b.json"
    a, c = db_.DataBudget(path), db_.DataBudget(path)

    async def go():
        for _ in range(3):
            await a.acquire()
        a._fold(db_.time.time())
        await c.acquire()
        c._fold(db_.time.time())
    asyncio.run(go())
    assert json.loads(path.read_text())["used"] == 4 and c.used_today() == 4
