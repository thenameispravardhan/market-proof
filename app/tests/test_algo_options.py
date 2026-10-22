"""Algo Lab v3, Half B — option adjustments, fills & pricing.

B4 TCA (live fill vs the quote it was decided at), B5 IV skew in option
backtests, B1 leg re-entry after its own stop, B2 rolling a tested leg, B3
auto delta-hedge with a FUT leg. Each adjustment is checked in the backtest
(engine.run) and on the live runner's paper path (no orders ever sent).
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.algo import runner as rn
from app.db import session as dbs
from app.db.models import AlgoStrategy, AlgoTrade
from app.tests.test_algo_live import T_CLOSE, FakeBroker, _install, _open_live_trade, _row_dict, _strategy


# ---- B4: TCA ---------------------------------------------------------------------------------------

def test_live_entry_keeps_the_quote_beside_the_fill(monkeypatch, isolated_db):
    px = {"NSE:NIFTY50-INDEX": 24570.0, "NSE:NIFTYCE": 100.0, "NSE:NIFTYPE": 90.0}
    broker = FakeBroker(fills={"NSE:NIFTYCE": 101.5, "NSE:NIFTYPE": 89.0})
    _install(monkeypatch, broker, px)
    _strategy()
    asyncio.run(rn.AlgoRunner().tick(T_CLOSE))
    with dbs.SessionLocal() as db:
        t = db.query(AlgoTrade).one()
        assert [(lg["entry"], lg["entry_quote"]) for lg in t.legs] == [(101.5, 100.0), (89.0, 90.0)]


def test_paper_fills_carry_no_quote(monkeypatch, isolated_db):
    px = {"NSE:NIFTY50-INDEX": 24570.0, "NSE:NIFTYCE": 100.0, "NSE:NIFTYPE": 90.0}
    _install(monkeypatch, FakeBroker(), px)
    sid = _strategy()
    with dbs.SessionLocal() as db:
        db.get(AlgoStrategy, sid).mode = "paper"
        db.commit()
    r = rn.AlgoRunner()
    asyncio.run(r.tick(T_CLOSE))
    with dbs.SessionLocal() as db:
        t = rn._row(db.query(AlgoTrade).one())
    assert all("entry_quote" not in lg for lg in t["legs"])
    assert asyncio.run(r._exit(t, px, "MANUAL", T_CLOSE + 60))
    with dbs.SessionLocal() as db:
        assert all("exit_quote" not in lg for lg in db.query(AlgoTrade).one().legs)


def test_live_exit_keeps_the_quote_and_tca_measures_slippage(monkeypatch, isolated_db, client):
    px = {"NSE:NIFTYCE": 120.0, "NSE:NIFTYPE": 80.0}
    broker = FakeBroker(net={"NSE:NIFTYCE": 65, "NSE:NIFTYPE": -65},
                        fills={"NSE:NIFTYCE": 119.0, "NSE:NIFTYPE": 80.5})
    _install(monkeypatch, broker, px)
    sid = _strategy()
    tid = _open_live_trade(sid)
    with dbs.SessionLocal() as db:                         # entry fills with their quotes
        t = db.get(AlgoTrade, tid)
        t.entry_at = rn._utc(time.time() - 3600).replace(tzinfo=None)
        t.legs = [{**t.legs[0], "entry_quote": 99.0}, {**t.legs[1], "entry_quote": 90.0}]
        db.commit()
    assert asyncio.run(rn.AlgoRunner()._exit(_row_dict(tid), px, "TARGET", T_CLOSE + 60))
    ce, pe = _row_dict(tid)["legs"]
    assert (ce["exit"], ce["exit_quote"], pe["exit"], pe["exit_quote"]) == (119.0, 120.0, 80.5, 80.0)

    r = client.get("/api/algo/tca?days=30").json()
    (s,) = r["strategies"]
    assert s["strategy_id"] == sid and s["fills"] == 4 and s["assumed_pct"] == 0
    kinds = {k["kind"]: k for k in s["kinds"]}
    # CE bought 100 vs 99 (+1 cost), sold 119 vs 120 (+1 cost); PE sold 90 vs 90 (0), bought back 80.5 vs 80 (+0.5)
    assert kinds["CE"]["fills"] == 2 and kinds["CE"]["total_rs"] == pytest.approx(65 + 65)
    assert kinds["CE"]["mean_pct"] == pytest.approx((1 / 99 + 1 / 120) / 2 * 100, abs=1e-3)
    assert kinds["PE"]["total_rs"] == pytest.approx(0.5 * 65) and kinds["PE"]["median_rs"] == pytest.approx(0.25 * 65)


def test_tca_ignores_paper_and_rejected_trades(isolated_db, client):
    with dbs.SessionLocal() as db:
        s = AlgoStrategy(name="p", spec={}, enabled=False, mode="paper")
        db.add(s)
        db.flush()
        leg = {"symbol": "X", "kind": "EQ", "act": 1, "qty": 10, "entry": 101.0, "entry_quote": 100.0}
        for mode, status in (("paper", "closed"), ("live", "rejected")):
            db.add(AlgoTrade(strategy_id=s.id, symbol="X", side="BUY", quantity=10, mode=mode, status=status,
                             entry_price=101.0, legs=[leg]))
        db.commit()
    assert client.get("/api/algo/tca").json()["strategies"] == []
