"""Live desk (person A): guards that switch a strategy off, book-level risk, execution."""
from __future__ import annotations

import asyncio

import pytest

from app.algo import engine
from app.tests.test_algo import DAY0, _cross_spec


# ---- A1: guards ----------------------------------------------------------------

def test_guard_is_validated_and_absent_by_default():
    assert "guard" not in _cross_spec()
    s = _cross_spec(guard={"max_drawdown": 5000, "max_rejects": 3})
    assert s["guard"] == {"max_drawdown": 5000, "max_rejects": 3, "on_breach": "pause"}
    assert "guard" not in _cross_spec(guard={"max_drawdown": None})            # nothing set = no guard
    with pytest.raises(ValueError, match="on_breach"):
        _cross_spec(guard={"max_drawdown": 5000, "on_breach": "panic"})
    with pytest.raises(ValueError, match="rejected orders"):
        _cross_spec(guard={"max_rejects": 0})


def test_guard_breach_reads_drawdown_rejects_and_slippage():
    from app.algo.runner import guard_breach

    st = {"closed": 500.0, "peak": 2000.0, "rejects": 0, "slippage": []}
    assert guard_breach({"max_drawdown": 2000}, st, 0.0) is None              # 1,500 down
    assert "drawdown ₹2,000" in guard_breach({"max_drawdown": 2000}, st, -500.0)   # open MTM counts
    assert "3 entries rejected" in guard_breach({"max_rejects": 3}, {**st, "rejects": 3}, 0.0)
    few = {**st, "slippage": [0.5] * 9}
    assert guard_breach({"max_slippage_pct": 0.1}, few, 0.0) is None           # too few fills to judge
    assert "median slippage 0.500%" in guard_breach({"max_slippage_pct": 0.1}, {**st, "slippage": [0.5] * 10}, 0.0)
    assert engine.fill_slippage({"act": 1, "qty": 10, "entry_quote": 100.0, "entry": 101.0,
                                 "exit_quote": 110.0, "exit": 109.0}) == [(pytest.approx(1.0), pytest.approx(10.0)),
                                                                          (pytest.approx(10 / 11), pytest.approx(10.0))]


def test_runner_switches_a_breached_strategy_off_and_flattens(monkeypatch, isolated_db):
    from app.algo import runner as rn
    from app.db import session as dbs
    from app.db.models import AlgoStrategy, AlgoTrade, AuditLog

    spec = _cross_spec(guard={"max_drawdown": 1000, "on_breach": "flatten"})
    at = lambda s: rn._utc(DAY0 + s).replace(tzinfo=None)  # noqa: E731
    with dbs.SessionLocal() as db:
        s = AlgoStrategy(name="g", spec=spec, enabled=True, mode="paper")
        db.add(s)
        db.flush()
        for k, net in enumerate((500.0, -1300.0)):                              # peak 500, now -800
            db.add(AlgoTrade(strategy_id=s.id, symbol="NSE:TEST-EQ", side="BUY", quantity=1, mode="paper",
                             status="closed", entry_at=at(k * 60), exit_at=at(k * 60 + 30), net_pnl=net))
        db.add(AlgoTrade(strategy_id=s.id, symbol="NSE:TEST-EQ", side="BUY", quantity=10, mode="paper", status="open",
                         entry_at=at(600), entry_price=100.0, ref="u", ref_sign=1,
                         legs=[{"symbol": "NSE:TEST-EQ", "kind": "EQ", "act": 1, "qty": 10, "qty0": 10, "per_set": 1,
                                "entry": 100.0, "label": "TEST"}]))
        db.commit()
        sid = s.id

    async def fake_ltp(symbols):
        return {x.upper(): 80.0 for x in symbols}                               # open MTM -200 -> 1,500 down

    monkeypatch.setattr(rn, "_ltp", fake_ltp)
    r = rn.AlgoRunner()
    asyncio.run(r.tick(DAY0 + 3600))
    with dbs.SessionLocal() as db:
        assert db.get(AlgoStrategy, sid).enabled is False
        audit = db.query(AuditLog).filter(AuditLog.action == "algo.guard_pause").one()
        assert "drawdown" in audit.after["why"]
        flat = db.query(AlgoTrade).filter(AlgoTrade.status == "closed", AlgoTrade.exit_reason == "GUARD").one()
        assert flat.quantity == 10
    assert any("GUARD switched it off" in e["msg"] for e in r.events)


# ---- A2: the book — every automation together ------------------------------------

def _open_trade(sid, symbol="NSE:TEST-EQ", qty=10, entry=100.0, margin=1000.0):
    from app.algo import runner as rn
    from app.db.models import AlgoTrade

    return AlgoTrade(strategy_id=sid, symbol=symbol, side="BUY", quantity=qty, mode="paper", status="open",
                     entry_at=rn._utc(DAY0 + 600).replace(tzinfo=None), entry_price=entry, ref="u", ref_sign=1,
                     margin=margin, legs=[{"symbol": symbol, "kind": "EQ", "act": 1, "qty": qty, "qty0": qty,
                                           "per_set": 1, "entry": entry, "label": symbol}])


def _two_strategies(db, spec):
    from app.db.models import AlgoStrategy

    ids = []
    for name in ("one", "two"):
        s = AlgoStrategy(name=name, spec=spec, enabled=True, mode="paper")
        db.add(s)
        db.flush()
        ids.append(s.id)
    return ids


def test_book_day_loss_flattens_every_automation_and_stops_entries(monkeypatch, isolated_db):
    from app.algo import runner as rn
    from app.config import get_settings
    from app.db import session as dbs
    from app.db.models import AlgoTrade

    monkeypatch.setattr(get_settings(), "ALGO_BOOK_MAX_DAY_LOSS", 1000.0)
    with dbs.SessionLocal() as db:
        a, b = _two_strategies(db, _cross_spec())
        db.add(AlgoTrade(strategy_id=a, symbol="NSE:X-EQ", side="BUY", quantity=1, mode="paper", status="closed",
                         entry_at=rn._utc(DAY0).replace(tzinfo=None), exit_at=rn._utc(DAY0 + 60).replace(tzinfo=None),
                         net_pnl=-600.0))                                       # realised today
        db.add(_open_trade(a))
        db.add(_open_trade(b, symbol="NSE:Y-EQ"))
        db.commit()

    async def fake_ltp(symbols):
        return {x.upper(): 80.0 for x in symbols}                               # each open -200 -> book -1,000

    monkeypatch.setattr(rn, "_ltp", fake_ltp)
    r = rn.AlgoRunner()
    asyncio.run(r.tick(DAY0 + 3600))
    with dbs.SessionLocal() as db:
        assert {t.exit_reason for t in db.query(AlgoTrade).filter(AlgoTrade.status == "closed", AlgoTrade.net_pnl != -600.0)} \
            == {"BOOK_MAX_LOSS"}
        assert db.query(AlgoTrade).filter(AlgoTrade.status == "open").count() == 0
    assert r._book_halt == (DAY0 + 19800) // 86400
    assert any("ALL AUTOMATIONS halted" in e["msg"] for e in r.events)


def test_book_margin_cap_sizes_down_then_skips_an_entry(monkeypatch, isolated_db):
    from app.algo import runner as rn
    from app.config import get_settings
    from app.db import session as dbs
    from app.db.models import AlgoTrade
    from app.tests.test_algo import bars

    monkeypatch.setattr(get_settings(), "ALGO_BOOK_MAX_MARGIN", 9050.0)
    with dbs.SessionLocal() as db:
        a, _ = _two_strategies(db, _cross_spec(exit_long=None))
        db.add(_open_trade(a, symbol="NSE:OTHER-EQ", margin=9000.0))            # ₹50 of the cap left
        db.commit()

    async def fake_bars(sym, tf, bars_n=400, now=None, **kw):
        d = bars([100.0, 101, 102, 106], spread=0)
        keep = [i for i, t in enumerate(d["t"]) if t + 300 <= now]
        return {k: ([v[i] for i in keep] if isinstance(v, list) else v) for k, v in d.items()}

    async def fake_ltp(symbols):
        return {x.upper(): 106.0 for x in symbols}

    monkeypatch.setattr(rn.data, "recent_bars", fake_bars)
    monkeypatch.setattr(rn, "_ltp", fake_ltp)
    r = rn.AlgoRunner()
    asyncio.run(r.tick(DAY0 + 4 * 300 + 4))
    with dbs.SessionLocal() as db:
        assert db.query(AlgoTrade).filter(AlgoTrade.symbol == "NSE:TEST-EQ").count() == 0
    assert any("book margin cap is full" in e["msg"] for e in r.events)


def test_book_view_and_kill_switch(client, monkeypatch, isolated_db):
    from app.algo import runner as rn
    from app.db import session as dbs
    from app.db.models import AlgoTrade

    with dbs.SessionLocal() as db:
        a, b = _two_strategies(db, _cross_spec())
        db.add(_open_trade(a, qty=10, margin=1000.0))
        db.add(_open_trade(b, qty=5, margin=500.0))
        db.commit()

    async def fake_ltp(symbols):
        return {x.upper(): 110.0 for x in symbols}

    monkeypatch.setattr(rn, "_ltp", fake_ltp)
    bk = client.get("/api/algo/book").json()
    (u,) = bk["underlyings"]
    assert (u["positions"], u["margin"], u["mtm"], u["delta"]) == (2, 1500.0, 150.0, 15.0)   # 15 shares long
    assert bk["positions"] == 2 and bk["open_mtm"] == 150.0 and bk["halted"] is False
    k = client.post("/api/algo/book/kill").json()
    assert k == {"flattened": 2, "still_open": 0}
    assert client.get("/api/algo/book").json()["halted"] is True
    with dbs.SessionLocal() as db:
        assert {t.exit_reason for t in db.query(AlgoTrade)} == {"BOOK_KILL"}


# ---- A3: execution — freeze-quantity slices, limit-then-chase ---------------------

class _Status:
    def __init__(self, state, qty=0, avg=None):
        from app.execution.base import OrderState

        self.state, self.filled_quantity, self.average_price = OrderState(state), qty, avg


def test_orders_above_the_freeze_quantity_go_out_in_whole_lot_slices(monkeypatch):
    from app.algo import fno, runner as rn

    monkeypatch.setattr(fno, "freeze_qty", lambda s: 1800)
    monkeypatch.setattr(fno, "contract_lot", lambda s, now=None: 75)
    assert rn._slices("NSE:NIFTYX-CE", 1800) == [1800]
    assert rn._slices("NSE:NIFTYX-CE", 4500) == [1800, 1800, 900]
    monkeypatch.setattr(fno, "freeze_qty", lambda s: 1000)                     # not a lot multiple: 13 lots
    assert rn._slices("NSE:NIFTYX-CE", 2250) == [975, 975, 300]
    monkeypatch.setattr(fno, "freeze_qty", lambda s: None)
    assert rn._slices("NSE:SBIN-EQ", 50000) == [50000]


def test_a_refused_later_slice_unwinds_an_entry_but_not_an_exit(monkeypatch):
    from app.algo import fno, runner as rn

    monkeypatch.setattr(fno, "freeze_qty", lambda s: 1800)
    monkeypatch.setattr(fno, "contract_lot", lambda s, now=None: 75)
    sent, cancelled = [], []

    async def place(acc, symbol, side, qty, *, exit_, strategy_id, product="INTRADAY", order_type="MARKET", limit_price=None):
        sent.append((side, qty, exit_))
        if len(sent) == 2 and not exit_:
            return False, None, "RMS: margin"
        return True, f"o{len(sent)}", "ok"

    async def cancel(acc, oid):
        cancelled.append(oid)

    monkeypatch.setattr(rn, "_place", place)
    monkeypatch.setattr(rn, "_cancel", cancel)
    ok, oid, msg = asyncio.run(rn._order(object(), "NSE:NIFTYX-CE", "BUY", 3600, exit_=False, strategy_id=1))
    assert (ok, msg) == (False, "RMS: margin") and cancelled == ["o1"]
    assert sent == [("BUY", 1800, False), ("BUY", 1800, False), ("SELL", 1800, True)]     # the slice that filled, undone
    sent.clear()
    ok, oid, _ = asyncio.run(rn._order(object(), "NSE:NIFTYX-CE", "SELL", 3600, exit_=True, strategy_id=1))
    assert ok and oid == "o1,o2" and sent == [("SELL", 1800, True), ("SELL", 1800, True)]


def test_fill_price_is_the_quantity_weighted_average_of_the_slices(monkeypatch):
    from app.algo import runner as rn

    book = {"a": _Status("FILLED", 1800, 100.0), "b": _Status("FILLED", 900, 103.0), "c": _Status("PENDING")}

    class B:
        async def get_order_status(self, oid):
            return book[oid]

    monkeypatch.setattr(rn, "_backend", lambda acc: B())
    assert asyncio.run(rn._fill_price(object(), "a,b")) == pytest.approx(101.0)
    assert asyncio.run(rn._fill_price(object(), "a,c", tries=1)) is None       # not every slice filled yet


def test_limit_chase_reprices_then_goes_market_and_stops_once_filled(monkeypatch):
    from app.algo import fno, runner as rn

    monkeypatch.setattr(fno, "freeze_qty", lambda s: None)
    placed, mods = [], []
    state = {"fill_after": 99}

    async def place(acc, symbol, side, qty, *, exit_, strategy_id, product="INTRADAY", order_type="MARKET", limit_price=None):
        placed.append((order_type, limit_price))
        return True, "L1", "ok"

    class B:
        async def get_order_status(self, oid):
            return _Status("FILLED" if len(mods) >= state["fill_after"] else "PENDING")

        async def modify_order(self, oid, **kw):
            mods.append(kw)
            return True, "ok"

    async def ltp(symbols):
        return {s.upper(): 101.13 for s in symbols}

    monkeypatch.setattr(rn, "_place", place)
    monkeypatch.setattr(rn, "_backend", lambda acc: B())
    monkeypatch.setattr(rn, "_ltp", ltp)
    ok, oid, msg = asyncio.run(rn._order_chase(object(), "NSE:SBIN-EQ", "BUY", 10, strategy_id=1, product="INTRADAY",
                                               quote=100.02, wait_s=0.03, steps=2))
    assert ok and oid == "L1" and msg == "chased to market"
    assert placed == [("LIMIT", 100.05)]                                     # a buy limit rounds UP onto the 0.05 grid
    assert mods[:2] == [{"limit_price": 101.15, "symbol": "NSE:SBIN-EQ"}] * 2 and str(mods[2]["order_type"]) .endswith("MARKET")
    placed.clear()
    mods.clear()
    state["fill_after"] = 1                                                   # fills after the first re-price
    ok, _, msg = asyncio.run(rn._order_chase(object(), "NSE:SBIN-EQ", "BUY", 10, strategy_id=1, product="INTRADAY",
                                             quote=100.02, wait_s=0.03, steps=3))
    assert msg == "limit filled" and len(mods) == 1


def test_limit_chase_spec_is_validated_and_off_by_default():
    assert "limit" not in _cross_spec()["entry_order"]
    s = _cross_spec(entry_order={"type": "market", "limit": {"wait_s": 8}})
    assert s["entry_order"]["limit"] == {"wait_s": 8.0, "steps": 3}
    with pytest.raises(ValueError, match="at most 30 s"):
        _cross_spec(entry_order={"type": "market", "limit": {"wait_s": 60}})


# ---- A4: shadow tests ---------------------------------------------------------------

def test_shadow_a_version_compare_it_and_promote_it(client, isolated_db):
    from app.algo import runner as rn
    from app.db import session as dbs
    from app.db.models import AlgoStrategy, AlgoTrade

    v1 = _cross_spec()
    s = client.post("/api/algo/strategies", json={"name": "orb", "spec": v1}).json()
    v2 = _cross_spec(max_trades_per_day=1)
    client.put(f"/api/algo/strategies/{s['id']}", json={"spec": v2})
    client.post(f"/api/algo/strategies/{s['id']}/versions/1/activate")          # v1 runs, v2 is the candidate
    r = client.post(f"/api/algo/strategies/{s['id']}/versions/2/shadow")
    assert r.status_code == 201
    sh = r.json()
    assert (sh["name"], sh["mode"], sh["enabled"], sh["shadow_of"]) == ("orb · shadow v2", "paper", True, s["id"])
    assert client.post(f"/api/algo/strategies/{s['id']}/versions/2/shadow").status_code == 409
    assert client.post(f"/api/algo/strategies/{sh['id']}/versions/1/shadow").status_code == 422
    assert "only paper-trades" in client.put(f"/api/algo/strategies/{sh['id']}",
                                             json={"mode": "live", "confirm": "LIVE"}).json()["detail"]
    later = rn._utc(__import__("time").time() + 60).replace(tzinfo=None)
    with dbs.SessionLocal() as db:
        for sid, net in ((sh["id"], 300.0), (sh["id"], -100.0), (s["id"], -250.0)):
            db.add(AlgoTrade(strategy_id=sid, symbol="NSE:TEST-EQ", side="BUY", quantity=1, mode="paper",
                             status="closed", entry_at=later, exit_at=later, net_pnl=net))
        db.commit()
        assert rn._book_all(0)["today"] == -250.0                                  # the shadow is outside the book
    cmp_ = client.get(f"/api/algo/strategies/{sh['id']}/shadow").json()
    assert cmp_["shadow"] == {"trades": 2, "wins": 1, "net": 200.0} and cmp_["parent"]["net"] == -250.0
    p = client.post(f"/api/algo/strategies/{sh['id']}/promote").json()
    assert p["version"] == 2                                                     # the matching version, re-activated
    with dbs.SessionLocal() as db:
        assert db.get(AlgoStrategy, sh["id"]).enabled is False
    # a shadow whose rules were tweaked promotes as a NEW version
    sh2 = client.post(f"/api/algo/strategies/{s['id']}/versions/1/shadow").json()
    client.put(f"/api/algo/strategies/{sh2['id']}", json={"spec": _cross_spec(max_trades_per_day=2)})
    p2 = client.post(f"/api/algo/strategies/{sh2['id']}/promote").json()
    assert p2["version"] == 3 and p2["spec"]["max_trades_per_day"] == 2
    assert client.post(f"/api/algo/strategies/{s['id']}/promote").status_code == 422
