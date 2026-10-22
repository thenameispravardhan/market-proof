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

from app.algo import engine, fno, payoff
from app.algo import runner as rn
from app.db import session as dbs
from app.db.models import AlgoStrategy, AlgoTrade
from app.tests.test_algo import DAY0, _nifty, _opt_spec
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


# ---- B5: IV skew in option backtests ----------------------------------------------------------------

def test_skewed_iv_adds_points_per_percent_out_of_the_money():
    assert fno.skewed_iv("PE", 24500, 25000, 0.14, put_skew=0.8) == pytest.approx(0.14 + 0.016)   # 2% OTM put: +1.6 pts
    assert fno.skewed_iv("CE", 25500, 25000, 0.14, call_skew=0.5) == pytest.approx(0.14 + 0.01)   # 2% OTM call: +1 pt
    assert fno.skewed_iv("PE", 25500, 25000, 0.14, put_skew=0.8) == 0.14                          # ITM put: flat
    assert fno.skewed_iv("CE", 24500, 25000, 0.14, put_skew=0.8, call_skew=0.5) == 0.14           # ITM call: flat
    assert fno.skewed_iv("CE", 30000, 25000, 0.14, call_skew=-5) == 0.01                          # never below 1%
    leg = {"kind": "PE", "K": 24500, "skew": (0.8, 0.0)}
    assert fno.leg_iv(leg, 25000, 0.14) == pytest.approx(0.156) and fno.leg_iv({**leg, "skew": None}, 25000, 0.14) == 0.14


def _skew_inst(put=None, call=None, legs=None):
    iv = {"source": "fixed", "value": 14, **({"put_skew": put} if put is not None else {}),
          **({"call_skew": call} if call is not None else {})}
    return {"type": "option", "expiry_kind": "weekly", "levels_on": "underlying", "iv": iv,
            "legs_long": legs or [{"right": "PE", "action": "SELL", "strike": "OTM", "steps": 10, "lots": 1}]}


def test_skew_validates_and_is_left_out_when_off():
    assert "put_skew" not in _opt_spec(instrument=_skew_inst(put=0))["instrument"]["iv"]
    assert _opt_spec(instrument=_skew_inst(put=0.8, call=-0.2))["instrument"]["iv"] == {
        "source": "fixed", "value": 14, "put_skew": 0.8, "call_skew": -0.2}
    with pytest.raises(ValueError, match="put IV skew is -5 to 5"):
        _opt_spec(instrument=_skew_inst(put=9))
    with pytest.raises(ValueError, match="call IV skew must be >= -5"):
        _opt_spec(instrument=_skew_inst(call=-7))


def test_otm_put_is_priced_richer_with_skew_in_every_backtest_path():
    closes = [24500, 24520, 24540, 24560] + [24560] * 6
    flat = engine.run(_opt_spec(instrument=_skew_inst()), {"N": _nifty(closes)})["trades"][0]
    sk = engine.run(_opt_spec(instrument=_skew_inst(put=0.8)), {"N": _nifty(closes)})["trades"][0]
    assert flat["legs"][0]["label"] == sk["legs"][0]["label"]                         # same strike (OTM 10 steps)
    K = float(sk["legs"][0]["label"].split()[2])
    u = 24560.0
    want = flat["greeks"]["iv"] + 0.8 * (u - K) / u * 100
    assert sk["greeks"]["iv"] == pytest.approx(want, abs=0.01)                         # position_metrics at the skewed IV
    assert sk["legs"][0]["entry"] > flat["legs"][0]["entry"]                           # _leg_px: a richer put
    # call legs untouched by a put skew; an ITM put untouched too
    calls = _skew_inst(put=0.8, legs=[{"right": "CE", "action": "SELL", "strike": "OTM", "steps": 4, "lots": 1}])
    c1 = engine.run(_opt_spec(instrument=calls), {"N": _nifty(closes)})["trades"][0]
    c0 = engine.run(_opt_spec(instrument={**calls, "iv": {"source": "fixed", "value": 14}}), {"N": _nifty(closes)})["trades"][0]
    assert c1["legs"][0]["entry"] == c0["legs"][0]["entry"]


def test_delta_strike_search_and_premium_atr_use_the_skewed_iv():
    meta = {"symbol": "NSE:NIFTY50-INDEX", "name": "NIFTY", "exch": "NSE", "lot": 65, "step": 50}
    u, t = 25000.0, DAY0
    pe = lambda sk: _opt_spec(instrument=_skew_inst(put=sk, legs=[  # noqa: E731
        {"right": "PE", "action": "SELL", "strike": "DELTA", "delta": 0.2, "lots": 1}]))
    k_flat = engine.sim_legs(pe(0), "BUY", meta, u, t, 0.14)[0]["K"]
    leg = engine.sim_legs(pe(1.5), "BUY", meta, u, t, 0.14)[0]
    assert leg["K"] < k_flat                     # fatter put wing: the 20-delta put sits further out
    yrs = fno.years_to(leg["exp"], t)
    want = abs(fno.bs_greeks(u, leg["K"], yrs, fno.skewed_iv("PE", leg["K"], u, 0.14, 1.5), "PE")["delta"])
    assert want == pytest.approx(0.2, abs=0.03)
    a_flat = engine.premium_atr({14: 100.0}, {**leg, "skew": None}, u, t, 0.14)[14]
    a_skew = engine.premium_atr({14: 100.0}, leg, u, t, 0.14)[14]
    assert a_skew != pytest.approx(a_flat)       # the premium's ATR uses its own (skewed) delta


def test_payoff_preview_prices_the_skew():
    meta = {"symbol": "NSE:NIFTY50-INDEX", "name": "NIFTY", "exch": "NSE", "lot": 65, "step": 50}
    flat = payoff.preview(_opt_spec(instrument=_skew_inst()), "BUY", 25000.0, DAY0, 0.14, meta)
    sk = payoff.preview(_opt_spec(instrument=_skew_inst(put=0.8)), "BUY", 25000.0, DAY0, 0.14, meta)
    assert sk["legs"][0]["price"] > flat["legs"][0]["price"]
    assert sk["position"]["iv"] > flat["position"]["iv"] and sk["premium"]["net"] > flat["premium"]["net"]
    assert sk["margin"]["leverage"] > 0 and sk["curve"][0]["now"] != flat["curve"][0]["now"]
