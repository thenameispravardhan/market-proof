"""Algo Lab LIVE-order path (app/algo/runner.py).

The paper path is covered in test_algo.py; until now nothing exercised the
code that sends real orders. These tests drive the runner through a full
bar-close entry and exit with the broker layer faked at `_order` /
`_fill_price` / `_broker_net`, and pin the money-safety properties:

  * the broker's average fill is booked, not the quote;
  * a multi-leg entry that fails half-way rolls back the legs that filled;
  * an exit never sends an order for a position the broker no longer holds;
  * a failed exit leaves the trade open and schedules a retry.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.algo import runner as rn
from app.db import session as dbs
from app.db.models import AlgoStrategy, AlgoTrade, BrokerAccount
from app.tests.test_algo import DAY0, _opt_spec, bars

T_CLOSE = DAY0 + 4 * 300 + 4          # bar 3 just closed (see test_algo)


class FakeBroker:
    def __init__(self, *, reject: set[str] | None = None, fills: dict[str, float] | None = None,
                 net: dict[str, int] | None = None):
        self.orders: list[dict] = []
        self.reject = reject or set()
        self.fills = fills or {}
        self.net = net if net is not None else {}
        self.cancelled: list[str] = []

    async def order(self, account, symbol, side, qty, *, exit_, strategy_id, product="INTRADAY"):
        self.orders.append({"symbol": symbol, "side": side, "qty": qty, "exit": exit_, "product": product})
        if symbol in self.reject and not exit_:
            return False, None, "RMS: insufficient margin"
        return True, f"OID{len(self.orders)}", "placed"

    async def fill_price(self, account, order_id, tries=4):
        sym = next(o["symbol"] for i, o in enumerate(self.orders, 1) if f"OID{i}" == order_id)
        return self.fills.get(sym)

    async def broker_net(self, account, symbol):
        return self.net.get(symbol)

    async def cancel(self, account, order_id):
        self.cancelled.append(order_id)


def _install(monkeypatch, broker: FakeBroker, px: dict[str, float], *, account_ok=True):
    acc = SimpleNamespace(id=1, name="live")

    def account(_aid):
        return (acc, None) if account_ok else (None, "account 'live' is switched off")

    async def fake_bundle(spec_, sym, now):
        d = bars([24500, 24520, 24540, 24560], spread=0)
        keep = [i for i, t in enumerate(d["t"]) if t + 300 <= now]
        d = {k: ([v[i] for i in keep] if isinstance(v, list) else v) for k, v in d.items()}
        d.update(tf_min=5, htf={}, meta={"symbol": sym, "name": "NIFTY", "exch": "NSE", "lot": 65})
        return d

    async def fake_legs(spec_, sym, side, u, now):
        return [{"symbol": "NSE:NIFTYCE", "kind": "CE", "act": 1, "per_set": 65, "label": "CE",
                 "price": px["NSE:NIFTYCE"]},
                {"symbol": "NSE:NIFTYPE", "kind": "PE", "act": -1, "per_set": 65, "label": "PE",
                 "price": px["NSE:NIFTYPE"]}]

    async def fake_ltp(symbols):
        return {s.upper(): px[s.upper()] for s in symbols if s.upper() in px}

    monkeypatch.setattr(rn, "_account", account)
    monkeypatch.setattr(rn, "_order", broker.order)
    monkeypatch.setattr(rn, "_fill_price", broker.fill_price)
    monkeypatch.setattr(rn, "_broker_net", broker.broker_net)
    monkeypatch.setattr(rn, "_cancel", broker.cancel)
    monkeypatch.setattr(rn.data, "live_bundle", fake_bundle)
    monkeypatch.setattr(rn, "build_legs", fake_legs)
    monkeypatch.setattr(rn, "_ltp", fake_ltp)
    monkeypatch.setattr(rn, "_notify_entry", lambda p: None)
    monkeypatch.setattr(rn, "_notify_exit", lambda p: None)


def _strategy(**spec_kw):
    spec = _opt_spec(direction="long", instrument={"type": "option", "levels_on": "underlying", "legs_long": [
        {"right": "CE", "action": "BUY", "strike": "ATM", "lots": 1},
        {"right": "PE", "action": "SELL", "strike": "ATM", "lots": 1}]},
        sizing={"mode": "lots", "value": 1}, **spec_kw)
    with dbs.SessionLocal() as db:
        acc = BrokerAccount(name="live", broker="fyers", paper_mode=False, enabled=True, access_token="t")
        db.add(acc)
        db.flush()
        s = AlgoStrategy(name="live-spread", spec=spec, enabled=True, mode="live", account_id=acc.id)
        db.add(s)
        db.commit()
        return s.id


def test_live_entry_books_the_broker_fill_and_order_ids(monkeypatch, isolated_db):
    px = {"NSE:NIFTY50-INDEX": 24570.0, "NSE:NIFTYCE": 100.0, "NSE:NIFTYPE": 90.0}
    broker = FakeBroker(fills={"NSE:NIFTYCE": 101.5, "NSE:NIFTYPE": 89.0})
    _install(monkeypatch, broker, px)
    _strategy()
    asyncio.run(rn.AlgoRunner().tick(T_CLOSE))

    # Bought leg first (a short leg without its hedge is the margin blow-up).
    assert [(o["symbol"], o["side"], o["exit"]) for o in broker.orders] == [
        ("NSE:NIFTYCE", "BUY", False), ("NSE:NIFTYPE", "SELL", False)]
    with dbs.SessionLocal() as db:
        t = db.query(AlgoTrade).one()
        assert t.status == "open" and t.mode == "live"
        assert [lg["entry"] for lg in t.legs] == [101.5, 89.0]     # broker fills, not quotes
        assert t.entry_order_id == "OID1,OID2"


def test_half_filled_entry_is_rolled_back(monkeypatch, isolated_db):
    px = {"NSE:NIFTY50-INDEX": 24570.0, "NSE:NIFTYCE": 100.0, "NSE:NIFTYPE": 90.0}
    broker = FakeBroker(reject={"NSE:NIFTYPE"})
    _install(monkeypatch, broker, px)
    _strategy()
    asyncio.run(rn.AlgoRunner().tick(T_CLOSE))

    assert [(o["symbol"], o["side"], o["exit"]) for o in broker.orders] == [
        ("NSE:NIFTYCE", "BUY", False),     # filled
        ("NSE:NIFTYPE", "SELL", False),    # rejected
        ("NSE:NIFTYCE", "SELL", True),     # the filled leg is flattened
    ]
    assert broker.cancelled == ["OID1"]    # any unfilled remainder cancelled before flattening
    with dbs.SessionLocal() as db:
        t = db.query(AlgoTrade).one()
        assert t.status == "rejected" and "rolled back" in t.note and "insufficient margin" in t.note


def test_no_account_means_no_orders(monkeypatch, isolated_db):
    px = {"NSE:NIFTY50-INDEX": 24570.0, "NSE:NIFTYCE": 100.0, "NSE:NIFTYPE": 90.0}
    broker = FakeBroker()
    _install(monkeypatch, broker, px, account_ok=False)
    _strategy()
    asyncio.run(rn.AlgoRunner().tick(T_CLOSE))
    assert broker.orders == []
    with dbs.SessionLocal() as db:
        assert db.query(AlgoTrade).one().status == "rejected"


def _open_live_trade(strategy_id: int) -> int:
    with dbs.SessionLocal() as db:
        t = AlgoTrade(strategy_id=strategy_id, symbol="NSE:NIFTY50-INDEX", side="BUY", quantity=65,
                      mode="live", status="open", entry_price=24570.0,
                      entry_at=rn._utc(T_CLOSE).replace(tzinfo=None), ref="u", ref_sign=1,
                      legs=[{"symbol": "NSE:NIFTYCE", "kind": "CE", "act": 1, "qty": 65, "entry": 100.0, "label": "CE"},
                            {"symbol": "NSE:NIFTYPE", "kind": "PE", "act": -1, "qty": 65, "entry": 90.0, "label": "PE"}])
        db.add(t)
        db.commit()
        return t.id


def _row_dict(trade_id: int) -> dict:
    with dbs.SessionLocal() as db:
        return rn._row(db.get(AlgoTrade, trade_id))


def test_exit_skips_legs_the_broker_no_longer_holds(monkeypatch, isolated_db):
    px = {"NSE:NIFTYCE": 120.0, "NSE:NIFTYPE": 80.0}
    # CE already squared off by hand in the Fyers app; PE still short 65.
    broker = FakeBroker(net={"NSE:NIFTYCE": 0, "NSE:NIFTYPE": -65}, fills={"NSE:NIFTYPE": 79.5})
    _install(monkeypatch, broker, px)
    tid = _open_live_trade(_strategy())
    ok = asyncio.run(rn.AlgoRunner()._exit(_row_dict(tid), px, "TARGET", T_CLOSE + 60))

    assert ok
    assert [(o["symbol"], o["side"], o["qty"], o["exit"]) for o in broker.orders] == [
        ("NSE:NIFTYPE", "BUY", 65, True)]
    with dbs.SessionLocal() as db:
        t = db.get(AlgoTrade, tid)
        assert t.status == "closed"
        assert t.legs[0].get("external") is True and t.legs[1]["exit"] == 79.5


def test_failed_exit_keeps_the_trade_open_and_backs_off(monkeypatch, isolated_db):
    px = {"NSE:NIFTYCE": 120.0, "NSE:NIFTYPE": 80.0}
    broker = FakeBroker(net={"NSE:NIFTYCE": 65, "NSE:NIFTYPE": -65})

    async def failing(account, symbol, side, qty, *, exit_, strategy_id, product="INTRADAY"):
        broker.orders.append({"symbol": symbol})
        return False, None, "fyers 503"

    _install(monkeypatch, broker, px)
    monkeypatch.setattr(rn, "_order", failing)
    tid = _open_live_trade(_strategy())
    r = rn.AlgoRunner()
    assert asyncio.run(r._exit(_row_dict(tid), px, "STOP", T_CLOSE + 60)) is False
    assert r._retry_at[tid] == pytest.approx(T_CLOSE + 90)
    # Inside the back-off window nothing is re-sent.
    n = len(broker.orders)
    assert asyncio.run(r._exit(_row_dict(tid), px, "STOP", T_CLOSE + 70)) is False
    assert len(broker.orders) == n
    with dbs.SessionLocal() as db:
        t = db.get(AlgoTrade, tid)
        assert t.status == "open" and "exit failed: fyers 503" in t.note


def test_unknown_broker_position_never_guesses_flat(monkeypatch, isolated_db):
    """A failed positions read (None) must not be treated as 'already flat'."""
    px = {"NSE:NIFTYCE": 120.0, "NSE:NIFTYPE": 80.0}
    broker = FakeBroker(net={})            # every lookup -> None
    _install(monkeypatch, broker, px)
    tid = _open_live_trade(_strategy())
    assert asyncio.run(rn.AlgoRunner()._exit(_row_dict(tid), px, "STOP", T_CLOSE + 60)) is False
    assert broker.orders == []
    with dbs.SessionLocal() as db:
        assert db.get(AlgoTrade, tid).status == "open"


def test_live_scale_out_closes_part_of_each_leg_once(monkeypatch, isolated_db):
    """A scale-out sends partial exits (written leg first), books them as their
    own closed row and shrinks the open one. A leg whose order fails stays
    whole in the open position and the level is never re-sent."""
    px = {"NSE:NIFTY50-INDEX": 24620.0, "NSE:NIFTYCE": 130.0, "NSE:NIFTYPE": 70.0}
    broker = FakeBroker(net={"NSE:NIFTYCE": 130, "NSE:NIFTYPE": -130}, fills={"NSE:NIFTYCE": 129.0, "NSE:NIFTYPE": 70.5})
    _install(monkeypatch, broker, px)
    tid = _open_live_trade(_strategy())
    with dbs.SessionLocal() as db:
        t = db.get(AlgoTrade, tid)
        t.legs = [{**lg, "qty": 130} for lg in t.legs]
        t.scale = {"left": 2, "levels": [[24600.0, 1]], "done": 0}
        db.commit()
    r = rn.AlgoRunner()
    asyncio.run(r._scale_out(_row_dict(tid), px, T_CLOSE + 60))
    assert [(o["symbol"], o["side"], o["qty"], o["exit"]) for o in broker.orders] == [
        ("NSE:NIFTYPE", "BUY", 65, True), ("NSE:NIFTYCE", "SELL", 65, True)]
    with dbs.SessionLocal() as db:
        rows = db.query(AlgoTrade).order_by(AlgoTrade.id).all()
        assert [lg["qty"] for lg in rows[0].legs] == [65, 65] and rows[0].status == "open"
        assert rows[0].scale == {"left": 1, "levels": [], "done": 1}
        part = rows[1]
        assert (part.status, part.exit_reason, part.note) == ("closed", "SCALE_OUT_1", f"partial exit of #{tid}")
        assert [(lg["qty"], lg["exit"]) for lg in part.legs] == [(65, 129.0), (65, 70.5)]   # broker fills


def _scaled_trade(strategy_id: int, sets: int, levels: list) -> int:
    tid = _open_live_trade(strategy_id)
    with dbs.SessionLocal() as db:
        t = db.get(AlgoTrade, tid)
        t.legs = [{**lg, "qty": 65 * sets, "qty0": 65 * sets} for lg in t.legs]
        t.scale = {"left": sets, "sets": sets, "levels": levels, "done": 0}
        db.commit()
    return tid


def _refusing(broker: FakeBroker, symbol: str):
    async def order(account, sym, side, qty, *, exit_, strategy_id, product="INTRADAY"):
        broker.orders.append({"symbol": sym, "qty": qty})
        return (False, None, "RMS reject") if sym == symbol else (True, f"OID{len(broker.orders)}", "ok")
    return order


PX = {"NSE:NIFTY50-INDEX": 24620.0, "NSE:NIFTYCE": 130.0, "NSE:NIFTYPE": 70.0}


def test_scale_out_never_sells_the_hedge_when_the_short_buy_back_fails(monkeypatch, isolated_db):
    """Written PE buy-back refused -> the CE hedge is NOT reduced (no naked short), the level is spent."""
    broker = FakeBroker(net={"NSE:NIFTYCE": 130, "NSE:NIFTYPE": -130})
    _install(monkeypatch, broker, PX)
    monkeypatch.setattr(rn, "_order", _refusing(broker, "NSE:NIFTYPE"))
    tid = _scaled_trade(_strategy(), 2, [[24600.0, 1]])
    r = rn.AlgoRunner()
    asyncio.run(r._manage([_row_dict(tid)], {}, T_CLOSE + 60, 10 * 60, DAY0 - 33300))
    asyncio.run(r._manage([_row_dict(tid)], {}, T_CLOSE + 61, 10 * 60, DAY0 - 33300))   # next tick: level is spent
    assert [o["symbol"] for o in broker.orders] == ["NSE:NIFTYPE"]                     # one attempt, no hedge sale
    with dbs.SessionLocal() as db:
        t = db.get(AlgoTrade, tid)
        assert [lg["qty"] for lg in t.legs] == [130, 130] and t.scale["levels"] == [] and t.status == "open"


def test_scale_out_after_a_failed_step_still_closes_whole_lots(monkeypatch, isolated_db):
    broker = FakeBroker(net={"NSE:NIFTYCE": 195, "NSE:NIFTYPE": -195}, fills={"NSE:NIFTYCE": 129.0, "NSE:NIFTYPE": 70.5})
    _install(monkeypatch, broker, PX)
    monkeypatch.setattr(rn, "_order", _refusing(broker, "NSE:NIFTYCE"))     # step 1: short bought back, the CE sale fails
    tid = _scaled_trade(_strategy(), 3, [[24600.0, 1], [24610.0, 1]])
    r = rn.AlgoRunner()
    asyncio.run(r._scale_out(_row_dict(tid), PX, T_CLOSE + 60))
    broker.net = {"NSE:NIFTYCE": 195, "NSE:NIFTYPE": -130}
    monkeypatch.setattr(rn, "_order", broker.order)
    asyncio.run(r._scale_out(_row_dict(tid), PX, T_CLOSE + 120))           # past the failed step's retry back-off
    assert [o["qty"] for o in broker.orders] == [65, 65, 65, 65]       # every partial a whole lot
    with dbs.SessionLocal() as db:
        assert [lg["qty"] for lg in db.get(AlgoTrade, tid).legs] == [130, 65]   # CE kept its unsold lot: over-hedged, never naked


def test_scale_out_is_never_resent_and_never_double_booked(monkeypatch, isolated_db):
    """The level is committed before any order: a failure after the orders
    doesn't resend it. An exit (manual square-off) during an in-flight
    scale-out waits, then books only what is left."""
    broker = FakeBroker(net={"NSE:NIFTYCE": 130, "NSE:NIFTYPE": -130}, fills={"NSE:NIFTYCE": 129.0, "NSE:NIFTYPE": 70.5})
    _install(monkeypatch, broker, PX)
    sid = _strategy()
    tid = _scaled_trade(sid, 2, [[24600.0, 1]])
    r = rn.AlgoRunner()

    def boom(*a, **k):
        raise RuntimeError("db locked")

    real_split = rn._split
    monkeypatch.setattr(rn, "_split", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(r._scale_out(_row_dict(tid), PX, T_CLOSE + 60))
    monkeypatch.setattr(rn, "_split", real_split)
    n = len(broker.orders)
    asyncio.run(r._manage([_row_dict(tid)], {}, T_CLOSE + 61, 10 * 60, DAY0 - 33300))
    assert len(broker.orders) == n                                     # not re-sent

    tid2 = _scaled_trade(sid, 2, [[24600.0, 1]])
    slow_order = broker.order

    async def slow(*a, **k):
        await asyncio.sleep(0.05)
        return await slow_order(*a, **k)

    monkeypatch.setattr(rn, "_order", slow)

    async def race():
        scale = asyncio.ensure_future(r._scale_out(_row_dict(tid2), PX, T_CLOSE + 70))
        await asyncio.sleep(0.01)
        manual = await r._exit(_row_dict(tid2), PX, "MANUAL", T_CLOSE + 70)   # stale dict: full size
        await scale
        return manual

    assert asyncio.run(race()) is False                                # busy: refused, not interleaved
    broker.net = {"NSE:NIFTYCE": 65, "NSE:NIFTYPE": -65}
    assert asyncio.run(r._exit(_row_dict(tid2), PX, "MANUAL", T_CLOSE + 80)) is True
    with dbs.SessionLocal() as db:
        rows = db.query(AlgoTrade).filter(AlgoTrade.id >= tid2).all()
        main = next(x for x in rows if x.id == tid2)
        assert main.status == "closed" and [lg["qty"] for lg in main.legs] == [65, 65]   # only the remainder
        assert sum(1 for x in rows if x.note == f"partial exit of #{tid2}") == 1


def test_accepted_but_unfilled_exit_keeps_the_trade_open(monkeypatch, isolated_db):
    """Fyers accepting the exit is not a fill: still held at the broker -> retried, not booked."""
    px = {"NSE:NIFTYCE": 120.0, "NSE:NIFTYPE": 80.0}
    broker = FakeBroker(net={"NSE:NIFTYCE": 65, "NSE:NIFTYPE": -65})   # no fills reported
    _install(monkeypatch, broker, px)
    tid = _open_live_trade(_strategy())
    r = rn.AlgoRunner()
    assert asyncio.run(r._exit(_row_dict(tid), px, "STOP", T_CLOSE + 60)) is False
    assert broker.cancelled == ["OID1"]        # cancelled first, so the retry can't double the exit
    with dbs.SessionLocal() as db:
        assert db.get(AlgoTrade, tid).status == "open"


# ---- per-leg stops, entry / exit rules on the option position ----

def _strangle_strategy(on_leg_stop=None, **spec_kw) -> tuple[int, dict]:
    legs = [{"right": "CE", "action": "SELL", "strike": "OTM", "steps": 2, "lots": 1, "sl_pct": 30},
            {"right": "PE", "action": "SELL", "strike": "OTM", "steps": 2, "lots": 1, "sl_pct": 30}]
    spec = _opt_spec(direction="long", sizing={"mode": "lots", "value": 1}, **spec_kw,
                     instrument={"type": "option", "levels_on": "underlying", "legs_long": legs,
                                 **({"on_leg_stop": on_leg_stop} if on_leg_stop else {})})
    with dbs.SessionLocal() as db:
        acc = BrokerAccount(name="live", broker="fyers", paper_mode=False, enabled=True, access_token="t")
        db.add(acc)
        db.flush()
        s = AlgoStrategy(name="live-strangle", spec=spec, enabled=True, mode="live", account_id=acc.id)
        db.add(s)
        db.commit()
        return s.id, spec


def _short_strangle_trade(strategy_id: int) -> int:
    with dbs.SessionLocal() as db:
        t = AlgoTrade(strategy_id=strategy_id, symbol="NSE:NIFTY50-INDEX", side="BUY", quantity=65,
                      mode="live", status="open", entry_price=24570.0,
                      entry_at=rn._utc(T_CLOSE).replace(tzinfo=None), ref="u", ref_sign=1,
                      legs=[{"symbol": "NSE:NIFTYCE", "kind": "CE", "act": -1, "qty": 65, "entry": 100.0, "label": "CE",
                             "per_set": 65, "sl": 130.0},
                            {"symbol": "NSE:NIFTYPE", "kind": "PE", "act": -1, "qty": 65, "entry": 90.0, "label": "PE",
                             "per_set": 65, "sl": 117.0}])
        db.add(t)
        db.commit()
        return t.id


def _manage_at(r: rn.AlgoRunner, sid: int, spec: dict, tid: int, px: dict) -> None:
    now = T_CLOSE + 120
    mins = int((now + rn.IST) % 86400) // 60
    asyncio.run(r._manage([_row_dict(tid)], {sid: {"spec": spec}}, now, mins, now - (now + rn.IST) % 86400))


def test_live_leg_stop_closes_that_leg_then_the_rest_at_cost(monkeypatch, isolated_db):
    """The put's own stop buys back only the put; with on_leg_stop=cost the
    call's stop moves to its entry, and when the call returns there it exits
    too — one closed position with both legs' fills."""
    px = {"NSE:NIFTY50-INDEX": 24400.0, "NSE:NIFTYCE": 95.0, "NSE:NIFTYPE": 120.0}
    broker = FakeBroker(net={"NSE:NIFTYCE": -65, "NSE:NIFTYPE": -65}, fills={"NSE:NIFTYPE": 120.5, "NSE:NIFTYCE": 101.0})
    _install(monkeypatch, broker, px)
    sid, spec = _strangle_strategy("cost")
    tid = _short_strangle_trade(sid)
    r = rn.AlgoRunner()
    _manage_at(r, sid, spec, tid, px)
    assert [(o["symbol"], o["side"], o["qty"], o["exit"]) for o in broker.orders] == [("NSE:NIFTYPE", "BUY", 65, True)]
    row = _row_dict(tid)
    assert row["status"] == "open"
    ce, pe = row["legs"]
    assert (pe["exit"], pe["exit_reason"]) == (120.5, "LEG_SL") and "exit" not in ce
    assert ce["sl"] == 100.0 and ce["at_cost"] is True                  # moved to cost
    px.update({"NSE:NIFTYCE": 101.0})
    _manage_at(r, sid, spec, tid, px)
    assert [o["symbol"] for o in broker.orders] == ["NSE:NIFTYPE", "NSE:NIFTYCE"]
    with dbs.SessionLocal() as db:
        t = db.get(AlgoTrade, tid)
        assert t.status == "closed" and t.exit_reason == "LEG_COST"
        assert t.gross_pnl == pytest.approx(-(101.0 - 100.0) * 65 - (120.5 - 90.0) * 65)


def test_live_leg_stop_can_exit_every_leg(monkeypatch, isolated_db):
    px = {"NSE:NIFTY50-INDEX": 24400.0, "NSE:NIFTYCE": 95.0, "NSE:NIFTYPE": 120.0}
    broker = FakeBroker(net={"NSE:NIFTYCE": -65, "NSE:NIFTYPE": -65}, fills={"NSE:NIFTYPE": 120.0, "NSE:NIFTYCE": 95.0})
    _install(monkeypatch, broker, px)
    sid, spec = _strangle_strategy("all")
    tid = _short_strangle_trade(sid)
    _manage_at(rn.AlgoRunner(), sid, spec, tid, px)
    assert sorted(o["symbol"] for o in broker.orders) == ["NSE:NIFTYCE", "NSE:NIFTYPE"]
    with dbs.SessionLocal() as db:
        assert db.get(AlgoTrade, tid).exit_reason == "LEG_SL"


def _legs_with_contracts(px: dict):
    async def legs(spec_, sym, side, u, now):
        exp = T_CLOSE + 5 * 86400
        return [{"symbol": "NSE:NIFTYCE", "kind": "CE", "act": -1, "per_set": 65, "label": "CE", "K": 24700, "exp": exp,
                 "price": px["NSE:NIFTYCE"], "sl_pct": 30},
                {"symbol": "NSE:NIFTYPE", "kind": "PE", "act": -1, "per_set": 65, "label": "PE", "K": 24400, "exp": exp,
                 "price": px["NSE:NIFTYPE"], "sl_pct": 30}]
    return legs


def test_live_entry_rules_use_live_premiums(monkeypatch, isolated_db):
    px = {"NSE:NIFTY50-INDEX": 24570.0, "NSE:NIFTYCE": 60.0, "NSE:NIFTYPE": 55.0}
    broker = FakeBroker(fills={"NSE:NIFTYCE": 60.0, "NSE:NIFTYPE": 55.0})
    _install(monkeypatch, broker, px)
    monkeypatch.setattr(rn, "build_legs", _legs_with_contracts(px))
    _strangle_strategy(position_rules={"entry": [{"metric": "credit", "op": ">=", "value": 10000}]})
    r = rn.AlgoRunner()
    asyncio.run(r.tick(T_CLOSE))
    assert broker.orders == []                                          # (60 + 55) x 65 = ₹7,475 < ₹10,000
    assert any("entry rule credit >= 10000 not met" in e["msg"] for e in r.events)


def test_live_entry_sets_each_legs_stop_from_its_fill(monkeypatch, isolated_db):
    px = {"NSE:NIFTY50-INDEX": 24570.0, "NSE:NIFTYCE": 60.0, "NSE:NIFTYPE": 55.0}
    broker = FakeBroker(fills={"NSE:NIFTYCE": 62.0, "NSE:NIFTYPE": 54.0})
    _install(monkeypatch, broker, px)
    monkeypatch.setattr(rn, "build_legs", _legs_with_contracts(px))
    _strangle_strategy(position_rules={"entry": [{"metric": "abs_delta", "op": "<=", "value": 0.5}]})
    asyncio.run(rn.AlgoRunner().tick(T_CLOSE))
    with dbs.SessionLocal() as db:
        t = db.query(AlgoTrade).one()
        assert t.status == "open"
        assert [(lg["entry"], lg["sl"], lg["per_set"]) for lg in t.legs] == [
            (62.0, pytest.approx(80.6), 65), (54.0, pytest.approx(70.2), 65)]    # +30% of each fill


def test_live_exit_rule_on_the_bar_close(monkeypatch, isolated_db):
    px = {"NSE:NIFTY50-INDEX": 24570.0, "NSE:NIFTYCE": 60.0, "NSE:NIFTYPE": 55.0}
    broker = FakeBroker(net={"NSE:NIFTYCE": -65, "NSE:NIFTYPE": -65}, fills={"NSE:NIFTYCE": 60.0, "NSE:NIFTYPE": 55.0})
    _install(monkeypatch, broker, px)
    sid, spec = _strangle_strategy(position_rules={"exit": [{"metric": "pnl_pct", "op": ">=", "value": 20}]})
    exp = T_CLOSE + 5 * 86400
    with dbs.SessionLocal() as db:
        db.add(AlgoTrade(strategy_id=sid, symbol="NSE:NIFTY50-INDEX", side="BUY", quantity=65, mode="live", status="open",
                         entry_price=24570.0, entry_at=rn._utc(T_CLOSE - 3600).replace(tzinfo=None), ref="u", ref_sign=1,
                         legs=[{"symbol": "NSE:NIFTYCE", "kind": "CE", "act": -1, "qty": 65, "entry": 80.0, "label": "CE",
                                "K": 24700, "exp": exp, "per_set": 65},
                               {"symbol": "NSE:NIFTYPE", "kind": "PE", "act": -1, "qty": 65, "entry": 70.0, "label": "PE",
                                "K": 24400, "exp": exp, "per_set": 65}]))
        db.commit()
    r = rn.AlgoRunner()
    asyncio.run(r.tick(T_CLOSE))                                        # premiums 150 -> 115: 23% of the credit kept
    with dbs.SessionLocal() as db:
        t = db.query(AlgoTrade).one()
        assert t.status == "closed" and t.exit_reason == "RULE_PNL_PCT"


def test_a_tick_through_the_target_exits_without_waiting_for_the_sweep(monkeypatch, isolated_db):
    """The 1 s sweep still reads the old price; a streamed tick past the
    target closes the position on that tick, and the runner log names the
    strategy whose target it was."""
    px = {"NSE:NIFTY50-INDEX": 24570.0, "NSE:NIFTYCE": 100.0, "NSE:NIFTYPE": 90.0}
    broker = FakeBroker(net={"NSE:NIFTYCE": 65, "NSE:NIFTYPE": -65},
                        fills={"NSE:NIFTYCE": 130.0, "NSE:NIFTYPE": 70.0})
    _install(monkeypatch, broker, px)
    sid = _strategy()
    tid = _open_live_trade(sid)
    with dbs.SessionLocal() as db:
        db.get(AlgoStrategy, sid).enabled = False        # exits only: no new signals in this test
        db.get(AlgoTrade, tid).target = 24600.0
        db.commit()
    r = rn.AlgoRunner()
    now = T_CLOSE + 60
    asyncio.run(r.tick(now))
    assert _row_dict(tid)["status"] == "open"
    assert {"NSE:NIFTY50-INDEX", "NSE:NIFTYCE", "NSE:NIFTYPE"} <= r._watch

    r.on_quote(SimpleNamespace(symbol="NSE:RELIANCE-EQ", last_price=1.0, extra={}))   # not held: ignored
    r.on_quote(SimpleNamespace(symbol="NSE:NIFTYCE", last_price=999.0, extra={"simulated": True}))  # paper fake
    assert not r._dirty
    for sym, p in {"NSE:NIFTY50-INDEX": 24610.0, "NSE:NIFTYCE": 130.0, "NSE:NIFTYPE": 70.0}.items():
        r.on_quote(SimpleNamespace(symbol=sym, last_price=p, extra={}))
    r._live_px = {k: (p, now) for k, (p, _) in r._live_px.items()}   # received "now" on the test clock
    asyncio.run(r.on_ticks(now + 0.3))

    t = _row_dict(tid)
    assert t["status"] == "closed" and t["exit_reason"] == "TARGET"
    assert r.last_tick_exit == now + 0.3
    assert any(e["msg"].startswith("live-spread: TARGET") and e.get("strategy_id") == sid for e in r.events)


# ---- carried F&O positions and their expiry --------------------------------------

DAY_START = DAY0 - 33300                       # 2025-01-01 00:00 IST
EXP_TODAY = DAY_START + 55800                   # 15:30 IST that day: the contracts' expiry
CARRY = {"session": {"start": "09:15", "end": "15:00", "square_off": None}}


def _carried_trade(strategy_id: int, exp: int, *, far_exp: int | None = None) -> int:
    """An open carried call spread on NIFTY: bought 24500 CE, sold 24500 PE
    (NRML), both expiring at `exp` — or the PE at `far_exp` (a calendar)."""
    with dbs.SessionLocal() as db:
        t = AlgoTrade(strategy_id=strategy_id, symbol="NSE:NIFTY50-INDEX", side="BUY", quantity=65,
                      mode="live", status="open", entry_price=24570.0,
                      entry_at=rn._utc(DAY_START - 5 * 86400 + 40000).replace(tzinfo=None), ref="u", ref_sign=1,
                      legs=[{"symbol": "NSE:NIFTYCE", "kind": "CE", "act": 1, "qty": 65, "entry": 100.0, "label": "CE",
                             "K": 24500.0, "exp": exp, "product": "MARGIN"},
                            {"symbol": "NSE:NIFTYPE", "kind": "PE", "act": -1, "qty": 65, "entry": 90.0, "label": "PE",
                             "K": 24500.0, "exp": far_exp or exp, "product": "MARGIN"}])
        db.add(t)
        db.commit()
        return t.id


def _at(hh: int, mm: int, day_offset: int = 0) -> float:
    return DAY_START + day_offset * 86400 + hh * 3600 + mm * 60


def test_carried_position_is_closed_at_15_15_on_its_expiry_day(monkeypatch, isolated_db):
    px = {"NSE:NIFTY50-INDEX": 24600.0, "NSE:NIFTYCE": 101.0, "NSE:NIFTYPE": 2.0}
    broker = FakeBroker(net={"NSE:NIFTYCE": 65, "NSE:NIFTYPE": -65}, fills={"NSE:NIFTYCE": 100.5, "NSE:NIFTYPE": 2.1})
    _install(monkeypatch, broker, px)
    sid = _strategy(**CARRY)
    spec = _row_spec(sid)
    tid = _carried_trade(sid, EXP_TODAY)
    r = rn.AlgoRunner()
    for now in (_at(15, 14), _at(15, 15)):
        asyncio.run(r._manage([_row_dict(tid)], {sid: {"spec": spec}}, now, int((now + rn.IST) % 86400) // 60, DAY_START))
        if now == _at(15, 14):
            assert broker.orders == [] and _row_dict(tid)["status"] == "open"     # not before 15:15
    # the written leg is bought back first, then the bought one sold — NRML, as it was opened
    assert [(o["symbol"], o["side"], o["exit"], o["product"]) for o in broker.orders] == [
        ("NSE:NIFTYPE", "BUY", True, "MARGIN"), ("NSE:NIFTYCE", "SELL", True, "MARGIN")]
    t = _row_dict(tid)
    assert t["status"] == "closed" and t["exit_reason"] == "EXPIRY"
    # the same position on a day it does NOT expire is left alone at 15:15
    tid2 = _carried_trade(sid, EXP_TODAY + 7 * 86400)
    n = len(broker.orders)
    asyncio.run(r._manage([_row_dict(tid2)], {sid: {"spec": spec}}, _at(15, 16), 15 * 60 + 16, DAY_START))
    assert len(broker.orders) == n and _row_dict(tid2)["status"] == "open"


def _row_spec(sid: int) -> dict:
    with dbs.SessionLocal() as db:
        return rn.engine.normalize(db.get(AlgoStrategy, sid).spec)


def _daily(closes: dict[int, float]):
    """Fake data.recent_bars: daily candles {IST day offset from DAY_START: close}."""
    async def recent_bars(sym, tf, bars=400, now=None):
        assert tf == 1440, "settlement reads the daily candle"
        ts = [DAY_START + k * 86400 for k in sorted(closes)]
        cs = [closes[k] for k in sorted(closes)]
        return {"t": ts, "o": cs, "h": cs, "l": cs, "c": cs, "v": [0] * len(cs), "tf_s": 86400}
    return recent_bars


def test_runner_books_a_carried_contract_found_already_expired(monkeypatch, isolated_db):
    """The runner was down at Wednesday's 15:15 exit. On Thursday it books the
    expired spread at its settlement value — the call at its intrinsic value
    against NIFTY's Wednesday close, the put at 0 — and sends no order: the
    exchange has already settled it."""
    px = {"NSE:NIFTY50-INDEX": 24700.0}
    broker = FakeBroker(net={"NSE:NIFTYCE": 0, "NSE:NIFTYPE": 0})
    _install(monkeypatch, broker, px)
    monkeypatch.setattr(rn.data, "recent_bars", _daily({-1: 24480.0, 0: 24620.0, 1: 24700.0}))
    sid = _strategy(**CARRY)
    with dbs.SessionLocal() as db:
        db.get(AlgoStrategy, sid).enabled = False        # settlement only: no new signals in this test
        db.commit()
    tid = _carried_trade(sid, EXP_TODAY)
    asyncio.run(rn.AlgoRunner().tick(_at(9, 20, day_offset=1)))          # Thursday 09:20
    assert broker.orders == []
    t = _row_dict(tid)
    assert t["status"] == "closed" and t["exit_reason"] == "EXPIRY"      # not CLOSED_EXTERNAL
    assert rn._epoch(t["exit_at"]) == EXP_TODAY                           # Wednesday's P&L, not Thursday's
    ce, pe = t["legs"]
    assert (ce["exit"], pe["exit"]) == (120.0, 0.0) and ce["external"] and pe["external"]
    assert t["gross_pnl"] == pytest.approx((120.0 - 100.0) * 65 + (90.0 - 0.0) * 65)


def test_an_intraday_position_keeps_the_broker_checked_square_off(monkeypatch, isolated_db):
    """MIS legs of an intraday strategy are squared off by the broker itself
    (~15:20), not settled by the exchange: after the close they still go the
    usual square-off way (the broker is flat -> booked at the last price)."""
    px = {"NSE:NIFTY50-INDEX": 24620.0, "NSE:NIFTYCE": 118.0, "NSE:NIFTYPE": 0.05}
    broker = FakeBroker(net={"NSE:NIFTYCE": 0, "NSE:NIFTYPE": 0})
    _install(monkeypatch, broker, px)
    monkeypatch.setattr(rn.data, "recent_bars", _daily({0: 24620.0}))
    sid = _strategy()                                                      # square-off 15:15
    with dbs.SessionLocal() as db:
        db.get(AlgoStrategy, sid).enabled = False
        t = db.get(AlgoTrade, _carried_trade(sid, EXP_TODAY))
        t.legs = [{**lg, "product": "INTRADAY"} for lg in t.legs]
        t.entry_at = rn._utc(_at(10, 0)).replace(tzinfo=None)               # opened today, 10:00
        tid = t.id
        db.commit()
    asyncio.run(rn.AlgoRunner().tick(_at(15, 40)))
    t = _row_dict(tid)
    assert broker.orders == [] and t["status"] == "closed"
    assert t["exit_reason"] == "CLOSED_EXTERNAL" and t["legs"][0]["exit"] == 118.0     # not settled at 120


def test_after_the_close_on_expiry_day_the_settlement_is_booked_not_ordered(monkeypatch, isolated_db):
    px = {"NSE:NIFTY50-INDEX": 24620.0}                # NIFTY's last price after the close
    broker = FakeBroker(net={"NSE:NIFTYCE": 65, "NSE:NIFTYPE": -65})
    _install(monkeypatch, broker, px)
    monkeypatch.setattr(rn.data, "recent_bars", _daily({-1: 24480.0}))   # today's candle not out yet
    sid = _strategy(**CARRY)
    with dbs.SessionLocal() as db:
        db.get(AlgoStrategy, sid).enabled = False
        db.commit()
    tid = _carried_trade(sid, EXP_TODAY)
    asyncio.run(rn.AlgoRunner().tick(_at(15, 40)))                         # the 15:15 exit failed / was missed
    assert broker.orders == []                                             # nothing sent after the close
    t = _row_dict(tid)
    assert t["status"] == "closed" and t["exit_reason"] == "EXPIRY" and t["legs"][0]["exit"] == 120.0


def test_a_calendar_s_far_leg_is_closed_when_the_market_opens(monkeypatch, isolated_db):
    """The near leg (the call) expired while the runner was down: it is settled
    at once; the put of next week is closed by the next session's first pass —
    never by an order after hours."""
    px = {"NSE:NIFTY50-INDEX": 24700.0, "NSE:NIFTYCE": 0.05, "NSE:NIFTYPE": 30.0}
    broker = FakeBroker(net={"NSE:NIFTYCE": 0, "NSE:NIFTYPE": -65}, fills={"NSE:NIFTYPE": 30.5})
    _install(monkeypatch, broker, px)
    monkeypatch.setattr(rn.data, "recent_bars", _daily({0: 24620.0}))
    sid = _strategy(**CARRY)
    with dbs.SessionLocal() as db:
        db.get(AlgoStrategy, sid).enabled = False
        db.commit()
    tid = _carried_trade(sid, EXP_TODAY, far_exp=EXP_TODAY + 7 * 86400)
    r = rn.AlgoRunner()
    asyncio.run(r.tick(_at(8, 30, day_offset=1)))                          # Thursday, before the open
    assert broker.orders == []
    t = _row_dict(tid)
    assert t["status"] == "open" and t["legs"][0]["exit"] == 120.0 and t["legs"][0]["exit_reason"] == "EXPIRY"
    assert "exit" not in t["legs"][1]
    asyncio.run(r.tick(_at(9, 16, day_offset=1)))                          # the market is open: the rest goes
    assert [(o["symbol"], o["side"], o["exit"]) for o in broker.orders] == [("NSE:NIFTYPE", "BUY", True)]
    t = _row_dict(tid)
    assert t["status"] == "closed" and t["exit_reason"] == "EXPIRY"
    assert t["gross_pnl"] == pytest.approx((120.0 - 100.0) * 65 - (30.5 - 90.0) * 65)


def test_a_future_opened_before_legs_carried_their_expiry_is_still_known():
    # No scrip master here: NIFTY's January 2025 future expired on the last Thursday (Jan 30).
    from datetime import date

    assert rn._leg_expiry_day({"kind": "FUT", "symbol": "NSE:NIFTY25JANFUT"}) == (date(2025, 1, 30) - date(1970, 1, 1)).days
    assert rn._leg_expiry_day({"kind": "EQ", "symbol": "NSE:SBIN-EQ"}) is None


def test_a_carried_strategy_opens_nothing_expiring_that_day_after_15_15(monkeypatch, isolated_db):
    px = {"NSE:NIFTY50-INDEX": 24570.0, "NSE:NIFTYCE": 100.0, "NSE:NIFTYPE": 90.0}
    broker = FakeBroker(fills={"NSE:NIFTYCE": 100.0, "NSE:NIFTYPE": 90.0})
    _install(monkeypatch, broker, px)

    async def expiring_legs(spec_, sym, side, u, now):
        return [{"symbol": "NSE:NIFTYCE", "kind": "CE", "act": 1, "per_set": 65, "label": "CE", "price": 100.0,
                 "K": 24500.0, "exp": EXP_TODAY}]

    monkeypatch.setattr(rn, "build_legs", expiring_legs)
    sid = _strategy(**CARRY)
    r = rn.AlgoRunner()
    strategies, _ = rn._snapshot()
    s = next(x for x in strategies if x["id"] == sid)
    book = {"realized": 0.0, "today": 0.0, "margin": 0.0, "open": 0}
    asyncio.run(r._enter(s, "NSE:NIFTY50-INDEX", "BUY", {}, _at(15, 16), book))
    assert broker.orders == [] and any("expires today" in e["msg"] for e in r.events)
    asyncio.run(r._enter(s, "NSE:NIFTY50-INDEX", "BUY", {}, _at(15, 10), book))     # before 15:15: allowed
    assert [o["symbol"] for o in broker.orders] == ["NSE:NIFTYCE"]
