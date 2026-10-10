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
