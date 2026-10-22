"""Algo Lab v3, half B: leg re-entry, rolls, delta hedges, IV skew, fills vs quotes (TCA)."""
from __future__ import annotations

import asyncio

import pytest

from app.algo import engine, fno
from app.tests.test_algo import DAY0, _RISE_FALL, _nifty, _opt_spec, _strangle

NIFTY = "NSE:NIFTY50-INDEX"


# ---- B5: IV skew in option backtests --------------------------------------------

def test_skew_prices_otm_wings_richer_and_is_off_by_default():
    assert "put_skew" not in _opt_spec()["instrument"]["iv"]
    flat = _opt_spec(instrument=_strangle())
    sk = _opt_spec(instrument={**_strangle(), "iv": {"source": "fixed", "value": 14, "put_skew": 0.8, "call_skew": 0.3}})
    meta = {"symbol": NIFTY, "name": "NIFTY", "exch": "NSE", "lot": 65, "step": 50}
    u, t = 24500.0, DAY0
    a, b = engine.sim_legs(flat, "BUY", meta, u, t, 0.14), engine.sim_legs(sk, "BUY", meta, u, t, 0.14)
    ce, pe = b
    assert engine.leg_iv(pe, u, 0.14) == pytest.approx(0.14 + (u - pe["K"]) / u * 100 * 0.8 / 100)
    assert engine.leg_iv(ce, u, 0.14) == pytest.approx(0.14 + (ce["K"] - u) / u * 100 * 0.3 / 100)
    assert engine.leg_iv({**pe, "K": u + 100}, u, 0.14) == 0.14            # in the money: no skew
    for x, y in zip(a, b):                                                # same strikes, richer wings
        assert x["K"] == y["K"] and engine._leg_px(y, u, t, 0.14) > engine._leg_px(x, u, t, 0.14)
    for bad in ({"put_skew": -1}, {"call_skew": 11}):
        with pytest.raises(ValueError, match="skew"):
            _opt_spec(instrument={**_strangle(), "iv": {"source": "fixed", "value": 14, **bad}})


# ---- B1-B3: what a position does on a candle's close ----------------------------

def test_adjustment_specs_are_validated():
    s = _opt_spec(instrument=_strangle(sl_pct=30, reentries=2, roll={"above": 0.45, "to": 0.25}),
                  position_rules={"hedge": {"above": 0.6}})
    lg = s["instrument"]["legs_long"][0]
    assert lg["reentries"] == 2 and lg["roll"] == {"above": 0.45, "to": 0.25, "max": 1}
    assert s["position_rules"] == {"hedge": {"above": 0.6, "max": 10}} and engine.adjusts(s)
    assert not engine.adjusts(_opt_spec(instrument=_strangle(sl_pct=30)))
    for kw, msg in [(dict(instrument=_strangle("all", sl_pct=30, reentries=1)), "re-entries can't"),
                    (dict(instrument=_strangle(roll={"above": 0.2, "to": 0.3})), "0 < to < above"),
                    (dict(instrument={"type": "option", "legs_long": [{"right": "FUT", "action": "BUY", "lots": 1, "reentries": 1}]}), "option legs"),
                    (dict(position_rules={"hedge": {"above": 0.5}}, instrument={"type": "equity"}), "option instrument"),
                    (dict(instrument=_strangle(sl_pct=30, reentries=1), stop_loss={"type": "points", "value": 50},
                          scale_out=[{"type": "rr", "value": 1, "qty_pct": 50}]), "can't be combined")]:
        with pytest.raises(ValueError, match=msg):
            _opt_spec(**kw)


def _legs(u=24500.0, t=DAY0):
    meta = {"symbol": NIFTY, "name": "NIFTY", "exch": "NSE", "lot": 65, "step": 50}
    spec = _opt_spec(instrument={**_strangle(sl_pct=30, reentries=1, roll={"above": 0.4, "to": 0.2}),
                                 "legs_long": [{"right": "CE", "action": "SELL", "strike": "ATM", "lots": 2, "sl_pct": 30,
                                                "reentries": 1, "roll": {"above": 0.4, "to": 0.2}},
                                               {"right": "PE", "action": "SELL", "strike": "OTM", "steps": 4, "lots": 1}]},
                     position_rules={"hedge": {"above": 0.3, "max": 2}})
    legs = engine.sim_legs(spec, "BUY", meta, u, t, 0.14)
    for lg in legs:
        lg["entry"] = engine._leg_px(lg, u, t, 0.14)
        lg["qty"] = lg["qty0"] = lg["per_set"]
    return spec, legs


def test_adjustments_reenter_a_stopped_leg_roll_a_tested_one_and_hedge_the_delta():
    spec, legs = _legs()
    px = lambda lg: engine._leg_px(lg, 24500.0, DAY0, 0.14)  # noqa: E731
    acts = engine.adjustments(spec, "BUY", legs, 24500.0, DAY0, 65, px, 0.14)
    kinds = [a[0] for a in acts]
    assert "roll" in kinds                                  # the ATM short call has |delta| ~0.5 > 0.4
    roll = next(a for a in acts if a[0] == "roll")
    assert roll[1] == 0 and roll[2]["strike"] == "DELTA" and roll[2]["delta"] == 0.2
    hedge = next(a for a in acts if a[0] == "hedge")       # short ATM call + far put: net short delta
    assert hedge[1] == 1 and hedge[2] == {"right": "FUT", "action": "BUY", "lots": 1}
    legs[0].update(exit=legs[0]["entry"] * 1.3, exit_reason="LEG_SL")
    acts = engine.adjustments(spec, "BUY", legs, 24500.0, DAY0, 65, px, 0.14)
    assert ("reenter", 0) in [a[:2] for a in acts]
    assert [a[0] for a in engine.adjustments(spec, "BUY", legs, 24500.0, DAY0, 65, px, 0.14, opens=False)] \
        in ([], ["hedge"])                                  # past the entry window: hedges only
    legs.append({**legs[0], "exit": None, "exit_reason": None, "via": "REENTRY"})
    legs[0]["redone"] = True
    assert "reenter" not in [a[0] for a in engine.adjustments(spec, "BUY", legs, 24500.0, DAY0, 65, px, 0.14)]


def test_backtest_reenters_rolls_and_hedges():
    ran = engine.run(_opt_spec(instrument=_strangle(sl_pct=30, reentries=1)), {"N": _nifty(_RISE_FALL)})["trades"][0]
    assert [lg.get("via") for lg in ran["legs"]].count("REENTRY") >= 1      # the call stopped on the rise, sold again
    first, again = ran["legs"][0], next(lg for lg in ran["legs"] if lg.get("via") == "REENTRY")
    assert first["reason"] == "LEG_SL" and again["label"] != first["label"]     # a new strike near the new spot
    rolled = engine.run(_opt_spec(instrument=_strangle(roll={"above": 0.4, "to": 0.2})), {"N": _nifty(_RISE_FALL)})["trades"][0]
    assert any(lg.get("reason") == "LEG_ROLL" for lg in rolled["legs"]) and any(lg.get("via") == "ROLL" for lg in rolled["legs"])
    hedged = engine.run(_opt_spec(instrument=_strangle(), position_rules={"hedge": {"above": 0.5, "max": 3}}),
                        {"N": _nifty(_RISE_FALL)})["trades"][0]
    hedges = [lg for lg in hedged["legs"] if lg.get("via") == "HEDGE"]
    assert hedges and hedges[0]["side"] == "BUY" and "FUT" in hedges[0]["label"]   # short calls on a rally: buy the future
    plain = engine.run(_opt_spec(instrument=_strangle()), {"N": _nifty(_RISE_FALL)})["trades"][0]
    assert len(plain["legs"]) == 2                                                 # absent = exactly what it was


def test_runner_paper_reentry_opens_one_new_leg_at_the_quote(monkeypatch, isolated_db):
    from app.algo import runner as rn
    from app.db import session as dbs
    from app.db.models import AlgoStrategy, AlgoTrade

    spec = _opt_spec(instrument=_strangle(sl_pct=30, reentries=1))
    exp = fno.expiry_after("NIFTY", "NSE", "weekly", "current", DAY0)
    with dbs.SessionLocal() as db:
        s = AlgoStrategy(name="re", spec=spec, enabled=True, mode="paper")
        db.add(s)
        db.flush()
        tr = AlgoTrade(strategy_id=s.id, symbol=NIFTY, side="BUY", quantity=65, mode="paper", status="open",
                       entry_at=rn._utc(DAY0).replace(tzinfo=None), entry_price=24500.0, ref="u", ref_sign=1,
                       legs=[{"symbol": "NSE:OLDCE", "kind": "CE", "act": -1, "qty": 65, "qty0": 65, "per_set": 65, "entry": 100.0,
                              "K": 24600, "exp": exp, "src": 0, "exit": 130.0, "exit_reason": "LEG_SL", "label": "CE"},
                             {"symbol": "NSE:OLDPE", "kind": "PE", "act": -1, "qty": 65, "qty0": 65, "per_set": 65, "entry": 90.0,
                              "K": 24400, "exp": exp, "src": 1, "label": "PE"}])
        db.add(tr)
        db.commit()
        sid, tid = s.id, tr.id
    orders = []

    async def fake_legs(spec_, sym, side, u, now):
        cfg = spec_["instrument"]["legs_long"]
        assert len(cfg) == 1 and cfg[0]["right"] == "CE"            # one leg: the configured call again
        return [{"symbol": "NSE:NEWCE", "kind": "CE", "act": -1, "per_set": 65, "label": "NEWCE", "price": 120.0,
                 "K": 24800, "exp": exp, "src": 0, "sl_pct": 30}]

    async def fake_ltp(symbols):
        return {s.upper(): {"NSE:OLDPE": 60.0, NIFTY: 24800.0}.get(s.upper(), 120.0) for s in symbols}

    async def no_orders(*a, **k):
        orders.append(a)
        return False, None, "paper must not order"

    monkeypatch.setattr(rn, "build_legs", fake_legs)
    monkeypatch.setattr(rn, "_ltp", fake_ltp)
    monkeypatch.setattr(rn, "_order", no_orders)
    r = rn.AlgoRunner()
    s_ = {"id": sid, "name": "re", "spec": spec}
    now = DAY0 + 3600
    asyncio.run(r._adjust(s_, rn._fresh(tid), NIFTY, now))
    asyncio.run(r._adjust(s_, rn._fresh(tid), NIFTY, now + 300))         # used up: nothing more
    with dbs.SessionLocal() as db:
        legs = db.get(AlgoTrade, tid).legs
    assert not orders and len(legs) == 3 and legs[0]["redone"]
    new = legs[2]
    assert (new["via"], new["src"], new["entry"], new["entry_quote"], new["qty"]) == ("REENTRY", 0, 120.0, 120.0, 65)
    assert new["sl"] == pytest.approx(120.0 * 1.3)                      # its own stop, from its own entry


# ---- B4: fills vs quotes ---------------------------------------------------------

def test_tca_reports_live_slippage_against_the_assumed(client, isolated_db):
    from app.db import session as dbs
    from app.db.models import AlgoStrategy, AlgoTrade
    from app.algo import runner as rn

    with dbs.SessionLocal() as db:
        s = AlgoStrategy(name="tca", spec=_opt_spec(), enabled=False, mode="live")
        db.add(s)
        db.flush()
        at = rn._utc(__import__("time").time() - 3600).replace(tzinfo=None)
        for mode in ("live", "paper"):
            db.add(AlgoTrade(strategy_id=s.id, symbol=NIFTY, side="BUY", quantity=65, mode=mode, status="closed",
                             entry_at=at, legs=[
                                 {"kind": "CE", "act": 1, "qty": 65, "entry_quote": 100.0, "entry": 101.0,       # paid 1% more
                                  "exit_quote": 120.0, "exit": 118.8},                                         # sold 1% lower
                                 {"kind": "PE", "act": -1, "qty": 65, "entry_quote": 80.0, "entry": 80.0,
                                  "exit_quote": 70.0, "exit": 70.7}]))                                          # bought back 1% higher
        db.commit()
    r = client.get("/api/algo/tca?days=5").json()["rows"]
    ce = next(x for x in r if x["kind"] == "CE")
    pe = next(x for x in r if x["kind"] == "PE")
    assert ce["fills"] == 2 and ce["median_pct"] == pytest.approx(1.0) and ce["rupees"] == pytest.approx(65 + 78)
    assert pe["fills"] == 2 and pe["median_pct"] == pytest.approx(0.5) and pe["assumed_pct"] == 0   # paper rows ignored
