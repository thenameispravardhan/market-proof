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


# ---- B1-B3 in the backtest ----------------------------------------------------------------------------

from app.tests.test_algo import _RISE_FALL, _strangle  # noqa: E402


def _run(spec, closes=_RISE_FALL):
    return engine.run(spec, {"N": _nifty(closes)})


def test_no_adjustment_keys_means_todays_backtest():
    plain = _opt_spec(instrument=_strangle(sl_pct=30))
    r = _run(plain)
    assert not engine.adjusts(plain) and "reentries" not in plain["instrument"]["legs_long"][0]
    assert all("entry_reason" not in lg for t in r["trades"] for lg in t["legs"])
    assert set(r["stats"]["skipped"]) == {"no_capital", "max_positions", "daily_halt", "entry_expired", "entry_rules"}


def test_a_stopped_leg_re_enters_at_the_current_spot_up_to_n_times():
    r = _run(_opt_spec(instrument=_strangle(sl_pct=30, reentries=1)))
    (t,) = r["trades"]
    ce, pe, ce2 = t["legs"]
    assert ce["reason"] == "LEG_SL" and ce2["entry_reason"] == "LEG_REENTRY"
    assert ce2["label"].endswith("CE") and float(ce2["label"].split()[2]) > float(ce["label"].split()[2])  # re-resolved higher
    assert ce2["exit"] == pytest.approx(ce2["entry"] * 1.3, abs=0.06)          # its own 30% stop, from its own entry
    assert sum(1 for lg in t["legs"] if lg.get("entry_reason") == "LEG_REENTRY") == 1     # reentries: 1 = once
    # a target never re-enters
    tg = _run(_opt_spec(instrument=_strangle(tg_pct=60, sl_pct=300, reentries=2)), _RISE_FALL[:13])["trades"][0]
    assert [lg.get("entry_reason") for lg in tg["legs"]] == [None, None]


def test_reentry_validation():
    with pytest.raises(ValueError, match="on_leg_stop 'all'"):
        _opt_spec(instrument=_strangle("all", sl_pct=30, reentries=1))
    with pytest.raises(ValueError, match="needs its own stop"):
        _opt_spec(instrument=_strangle(reentries=1))
    with pytest.raises(ValueError, match="re-entries are 0-5"):
        _opt_spec(instrument=_strangle(sl_pct=30, reentries=6))
    with pytest.raises(ValueError, match="can't be combined with scale-out"):
        _opt_spec(instrument={**_strangle(), "legs_long": _roll_legs()}, stop_loss={"type": "points", "value": 50},
                  scale_out=[{"type": "rr", "value": 1, "qty_pct": 50}])
    with pytest.raises(ValueError, match="need an option instrument"):
        engine.normalize({**_opt_spec(instrument=_strangle(sl_pct=30, reentries=1)),
                          "instrument": {**_strangle(sl_pct=30, reentries=1), "type": "future"}})
    assert _opt_spec(instrument=_strangle("cost", reentries=1))["instrument"]["legs_long"][0]["reentries"] == 1


def _roll_legs(**roll):
    return [{"right": "CE", "action": "SELL", "strike": "DELTA", "delta": 0.3, "lots": 1,
             "roll": {"above": 0.45, "to": 0.25, "max": 2, **roll}},
            {"right": "PE", "action": "SELL", "strike": "DELTA", "delta": 0.3, "lots": 1}]


def test_a_tested_leg_rolls_out_to_the_target_delta_at_most_max_times():
    t = _run(_opt_spec(instrument={**_strangle(), "legs_long": _roll_legs()}))["trades"][0]
    ce, pe, r1, r2 = t["legs"]
    assert (ce["reason"], r1["entry_reason"], r1["reason"], r2["entry_reason"]) == ("LEG_ROLL", "LEG_ROLLED", "LEG_ROLL", "LEG_ROLLED")
    assert "reason" not in r2 and "entry_reason" not in pe                     # max 2: the third test is held
    k = [float(x["label"].split()[2]) for x in (ce, r1, r2)]
    assert k[0] < k[1] < k[2]                                                  # each roll moves further out
    one = _run(_opt_spec(instrument={**_strangle(), "legs_long": _roll_legs(max=1)}))["trades"][0]
    assert sum(1 for lg in one["legs"] if lg.get("entry_reason") == "LEG_ROLLED") == 1
    with pytest.raises(ValueError, match="0 < to < above < 1"):
        _opt_spec(instrument={**_strangle(), "legs_long": _roll_legs(to=0.5)})
    with pytest.raises(ValueError, match="rolls per position are 1-5"):
        _opt_spec(instrument={**_strangle(), "legs_long": _roll_legs(max=9)})


def test_the_delta_hedge_adds_fut_legs_and_needs_free_capital():
    spec = _opt_spec(instrument=_strangle(), position_rules={"hedge": {"above": 0.5, "max": 3}})
    t = _run(spec)["trades"][0]
    hedges = [lg for lg in t["legs"] if lg.get("entry_reason") == "HEDGE"]
    assert len(hedges) == 3 and all(h["label"].startswith("NIFTY FUT") for h in hedges)   # max 3
    assert hedges[0]["side"] == "BUY"                        # the rally made the short call's delta win: buy futures
    assert hedges[-1]["side"] == "SELL"                      # the fall swung it back: sell them
    assert t["margin"] > _run(_opt_spec(instrument=_strangle()))["trades"][0]["margin"]   # margin recomputed
    poor = _run(_opt_spec(instrument=_strangle(), position_rules={"hedge": {"above": 0.5, "max": 3}},
                          portfolio={"capital": 450000, "leverage": 8}))
    assert all("entry_reason" not in lg for lg in poor["trades"][0]["legs"])
    assert poor["stats"]["skipped"]["hedge_no_capital"] > 0
    with pytest.raises(ValueError, match="hedges per position are 1-50"):
        _opt_spec(instrument=_strangle(), position_rules={"hedge": {"above": 0.5, "max": 99}})


def test_hedge_lots_round_half_away_from_zero_and_respect_the_threshold():
    spec = _opt_spec(instrument=_strangle(), position_rules={"hedge": {"above": 0.5, "max": 2}})
    assert engine.hedge_lots(spec, [], -0.6, 2) == 1 and engine.hedge_lots(spec, [], 0.75, 2) == -2
    assert engine.hedge_lots(spec, [], 0.5, 4) == 0                            # not above the threshold
    assert engine.hedge_lots(spec, [], 0.6, 0.5) == 0                          # rounds to 0 lots: skip
    two = [{"entry_reason": "HEDGE"}, {"entry_reason": "HEDGE"}]
    assert engine.hedge_lots(spec, two, 3.0, 1) == 0                           # max reached


def test_adjustments_stop_at_the_entry_window_end_but_the_hedge_runs_to_square_off():
    spec = _opt_spec(instrument=_strangle(), session={"start": "09:15", "end": "09:40", "square_off": "15:15"})
    assert engine.adjust_window(spec, 9 * 60 + 40) == (True, True)
    assert engine.adjust_window(spec, 9 * 60 + 45) == (False, True)
    assert engine.adjust_window(spec, 15 * 60 + 15) == (False, False)
    late = _run(_opt_spec(instrument={**_strangle(), "legs_long": _roll_legs()},
                          session={"start": "09:15", "end": "09:35", "square_off": "15:15"}))["trades"][0]
    assert all("entry_reason" not in lg for lg in late["legs"])                # tested only after 09:35: no roll


# ---- B1-B3 on the live runner ---------------------------------------------------------------------------

from app.db.models import BrokerAccount  # noqa: E402

EXP = T_CLOSE + 5 * 86400
U = "NSE:NIFTY50-INDEX"


def _adj_strategy(legs, mode="paper", **spec_kw) -> tuple[int, dict]:
    spec = _opt_spec(direction="long", sizing={"mode": "lots", "value": 1}, **spec_kw,
                     instrument={"type": "option", "levels_on": "underlying", "legs_long": legs,
                                 "iv": {"source": "fixed", "value": 14}})
    with dbs.SessionLocal() as db:
        acc = BrokerAccount(name="live", broker="fyers", paper_mode=False, enabled=True, access_token="t")
        db.add(acc)
        db.flush()
        s = AlgoStrategy(name="adjuster", spec=spec, enabled=True, mode=mode, account_id=acc.id)
        db.add(s)
        db.commit()
        return s.id, spec


def _trade(sid: int, legs: list[dict], mode="paper") -> int:
    with dbs.SessionLocal() as db:
        t = AlgoTrade(strategy_id=sid, symbol=U, side="BUY", quantity=65, mode=mode, status="open", entry_price=24570.0,
                      entry_at=rn._utc(T_CLOSE - 1800).replace(tzinfo=None), ref="u", ref_sign=1, margin=100000.0,
                      legs=legs)
        db.add(t)
        db.commit()
        return t.id


def _opt(sym, kind, act, K, entry, src, **kw):
    return {"symbol": sym, "kind": kind, "act": act, "qty": 65, "qty0": 65, "per_set": 65, "entry": entry,
            "label": sym.split(":")[-1], "K": K, "exp": EXP, "src": src, "gen": 0, **kw}


def _longer_candles(monkeypatch):
    """Candles past the first close, so a second tick one candle later reaches _decide again."""
    from app.tests.test_algo import bars

    async def bundle(spec_, sym, now):
        d = bars([24500, 24520, 24540, 24560, 24570, 24570], spread=0)
        keep = [i for i, t in enumerate(d["t"]) if t + 300 <= now]
        d = {k: ([v[i] for i in keep] if isinstance(v, list) else v) for k, v in d.items()}
        d.update(tf_min=5, htf={}, meta={"symbol": sym, "name": "NIFTY", "exch": "NSE", "lot": 65})
        return d

    monkeypatch.setattr(rn.data, "live_bundle", bundle)


def _chain_legs(contracts: dict):
    """build_legs stand-in: the contract for a one-leg spec's leg, by (right, strike rule)."""
    async def legs(spec_, sym, side, u, now):
        cfg = spec_["instrument"]["legs_long"][0]
        c = contracts[(cfg["right"], cfg["strike"])]
        lot = 65 * cfg["lots"]
        return [{**c, "act": 1 if cfg["action"] == "BUY" else -1, "per_set": lot, "label": c["symbol"].split(":")[-1],
                 "src": 0, "gen": 0, **({k: cfg[k] for k in ("sl_pct", "tg_pct") if cfg.get(k)})}]
    return legs


@pytest.mark.parametrize("n, legs_after", [(1, 3), (2, 4)])
def test_paper_reentry_reopens_the_stopped_leg_without_an_order(monkeypatch, isolated_db, n, legs_after):
    px = {U: 24570.0, "NSE:PE24400": 60.0, "NSE:CE24700": 70.0}
    broker = FakeBroker()
    _install(monkeypatch, broker, px)
    _longer_candles(monkeypatch)
    monkeypatch.setattr(rn, "build_legs", _chain_legs({("CE", "OTM"): {"symbol": "NSE:CE24700", "kind": "CE",
                                                                        "K": 24700, "exp": EXP, "price": 70.0}}))
    legs_cfg = [{"right": "CE", "action": "SELL", "strike": "OTM", "steps": 2, "lots": 1, "sl_pct": 30, "reentries": n},
                {"right": "PE", "action": "SELL", "strike": "OTM", "steps": 2, "lots": 1, "sl_pct": 30}]
    sid, _ = _adj_strategy(legs_cfg)
    tid = _trade(sid, [_opt("NSE:CE24650", "CE", -1, 24650, 45.0, 0, sl=58.5, exit=58.5, exit_reason="LEG_SL"),
                       _opt("NSE:PE24400", "PE", -1, 24400, 50.0, 1, sl=65.0)])
    r = rn.AlgoRunner()
    asyncio.run(r.tick(T_CLOSE))
    assert broker.orders == []                                         # paper: never an order
    legs = _row_dict(tid)["legs"]
    new = legs[2]
    assert (new["symbol"], new["entry"], new["src"], new["gen"], new["entry_reason"]) == (
        "NSE:CE24700", 70.0, 0, 1, "LEG_REENTRY")
    assert new["sl"] == pytest.approx(91.0) and new["qty"] == 65 and "entry_quote" not in new
    assert any("LEG_REENTRY" in e["msg"] for e in r.events)
    # the next candle: re-entered again only while re-entries are left
    legs[2].update(exit=91.0, exit_reason="LEG_SL")
    with dbs.SessionLocal() as db:
        db.get(AlgoTrade, tid).legs = legs
        db.commit()
    asyncio.run(r.tick(T_CLOSE + 300))
    assert len(_row_dict(tid)["legs"]) == legs_after and broker.orders == []


def test_paper_roll_closes_the_tested_leg_then_opens_the_target_delta(monkeypatch, isolated_db):
    px = {U: 24570.0, "NSE:CE24450": 190.0, "NSE:PE24700": 150.0, "NSE:CE24800": 40.0}
    broker = FakeBroker()
    _install(monkeypatch, broker, px)
    _longer_candles(monkeypatch)
    monkeypatch.setattr(rn, "build_legs", _chain_legs({("CE", "DELTA"): {"symbol": "NSE:CE24800", "kind": "CE",
                                                                          "K": 24800, "exp": EXP, "price": 40.0}}))
    legs_cfg = [{"right": "CE", "action": "SELL", "strike": "DELTA", "delta": 0.3, "lots": 1,
                 "roll": {"above": 0.45, "to": 0.25, "max": 1}},
                {"right": "PE", "action": "SELL", "strike": "DELTA", "delta": 0.3, "lots": 1}]
    sid, _ = _adj_strategy(legs_cfg)
    tid = _trade(sid, [_opt("NSE:CE24450", "CE", -1, 24450, 80.0, 0), _opt("NSE:PE24700", "PE", -1, 24700, 60.0, 1)])
    ce = _row_dict(tid)["legs"][0]
    assert engine.leg_abs_delta(ce, 24570.0, T_CLOSE, None, 190.0) > 0.45     # ITM: tested
    asyncio.run(rn.AlgoRunner().tick(T_CLOSE))
    assert broker.orders == []
    old, pe, new = _row_dict(tid)["legs"]
    assert (old["exit"], old["exit_reason"]) == (190.0, "LEG_ROLL") and "exit" not in pe
    assert (new["symbol"], new["entry_reason"], new["gen"], new["src"]) == ("NSE:CE24800", "LEG_ROLLED", 1, 0)
    asyncio.run(rn.AlgoRunner().tick(T_CLOSE + 300))                       # max 1: the PE (also tested) has no roll
    assert len(_row_dict(tid)["legs"]) == 3


def test_paper_hedge_buys_futures_against_a_short_call_gone_in_the_money(monkeypatch, isolated_db):
    px = {U: 24570.0, "NSE:CE24300": 290.0, "NSE:PE24300": 20.0, "NSE:NIFTYFUT": 24600.0}
    broker = FakeBroker()
    _install(monkeypatch, broker, px)
    _longer_candles(monkeypatch)
    monkeypatch.setattr(rn, "build_legs", _chain_legs({("FUT", "ATM"): {"symbol": "NSE:NIFTYFUT", "kind": "FUT",
                                                                        "price": 24600.0}}))
    legs_cfg = [{"right": "CE", "action": "SELL", "strike": "ATM", "lots": 2},
                {"right": "PE", "action": "SELL", "strike": "ATM", "lots": 2}]
    sid, _ = _adj_strategy(legs_cfg, position_rules={"hedge": {"above": 0.5, "max": 1}})
    tid = _trade(sid, [_opt("NSE:CE24300", "CE", -1, 24300, 150.0, 0, qty=130, qty0=130, per_set=130),
                       _opt("NSE:PE24300", "PE", -1, 24300, 120.0, 1, qty=130, qty0=130, per_set=130)])
    asyncio.run(rn.AlgoRunner().tick(T_CLOSE))
    assert broker.orders == []
    legs = _row_dict(tid)["legs"]
    h = legs[2]
    assert (h["kind"], h["act"], h["hedge"], h["entry_reason"], h["entry"]) == ("FUT", 1, True, "HEDGE", 24600.0)
    assert h["qty"] % 65 == 0 and 65 <= h["qty"] <= 130                      # ~ -(−1.x lots) → +1 or +2 lots
    m = engine.position_metrics(legs, 24570.0, T_CLOSE, 65, lambda lg: px[lg["symbol"]])
    assert abs(m["delta"]) < 0.5                                             # neutral again
    asyncio.run(rn.AlgoRunner().tick(T_CLOSE + 300))                        # max 1
    assert len(_row_dict(tid)["legs"]) == 3


def test_live_roll_buys_back_first_and_a_refused_replacement_never_unwinds(monkeypatch, isolated_db):
    px = {U: 24570.0, "NSE:CE24450": 190.0, "NSE:PE24700": 150.0, "NSE:CE24800": 40.0}
    broker = FakeBroker(net={"NSE:CE24450": -65, "NSE:CE24800": 0}, fills={"NSE:CE24450": 191.0},
                        reject={"NSE:CE24800"})
    _install(monkeypatch, broker, px)
    monkeypatch.setattr(rn, "build_legs", _chain_legs({("CE", "DELTA"): {"symbol": "NSE:CE24800", "kind": "CE",
                                                                          "K": 24800, "exp": EXP, "price": 40.0}}))
    legs_cfg = [{"right": "CE", "action": "SELL", "strike": "DELTA", "delta": 0.3, "lots": 1,
                 "roll": {"above": 0.45, "to": 0.25, "max": 1}},
                {"right": "PE", "action": "SELL", "strike": "DELTA", "delta": 0.3, "lots": 1}]
    sid, _ = _adj_strategy(legs_cfg, mode="live")
    tid = _trade(sid, [_opt("NSE:CE24450", "CE", -1, 24450, 80.0, 0), _opt("NSE:PE24700", "PE", -1, 24700, 60.0, 1)],
                 mode="live")
    r = rn.AlgoRunner()
    asyncio.run(r.tick(T_CLOSE))
    assert [(o["symbol"], o["side"], o["exit"]) for o in broker.orders] == [
        ("NSE:CE24450", "BUY", True), ("NSE:CE24800", "SELL", False)]          # out before in
    t = _row_dict(tid)
    assert t["status"] == "open" and len(t["legs"]) == 2                     # the PE is untouched, nothing unwound
    old = t["legs"][0]
    assert (old["exit"], old["exit_quote"], old["exit_reason"]) == (191.0, 190.0, "LEG_ROLL")
    assert "LEG_ROLLED not opened" in t["note"] and any("not opened" in e["msg"] for e in r.events)


def test_the_guide_example_uses_every_adjustment():
    import json
    import re

    from app.algo import guide

    last = json.loads(re.findall(r"```json\n(.*?)\n```", guide.text(), re.S)[-1])
    spec = engine.normalize(last["spec"])
    legs = spec["instrument"]["legs_long"]
    assert engine.adjusts(spec) and spec["position_rules"]["hedge"] == {"above": 0.6, "max": 4}
    assert all(lg["reentries"] == 1 and lg["roll"]["to"] == 0.2 for lg in legs)
    assert spec["instrument"]["iv"]["put_skew"] == 0.8


def test_live_roll_of_a_leg_already_flat_at_the_broker_opens_nothing(monkeypatch, isolated_db):
    px = {U: 24570.0, "NSE:CE24450": 190.0, "NSE:PE24700": 150.0, "NSE:CE24800": 40.0}
    broker = FakeBroker(net={"NSE:CE24450": 0})                     # squared off by hand in the Fyers app
    _install(monkeypatch, broker, px)
    monkeypatch.setattr(rn, "build_legs", _chain_legs({("CE", "DELTA"): {"symbol": "NSE:CE24800", "kind": "CE",
                                                                          "K": 24800, "exp": EXP, "price": 40.0}}))
    sid, _ = _adj_strategy(_roll_legs(max=1), mode="live")
    tid = _trade(sid, [_opt("NSE:CE24450", "CE", -1, 24450, 80.0, 0), _opt("NSE:PE24700", "PE", -1, 24700, 60.0, 1)],
                 mode="live")
    asyncio.run(rn.AlgoRunner().tick(T_CLOSE))
    assert broker.orders == []
    legs = _row_dict(tid)["legs"]
    assert len(legs) == 2 and legs[0]["external"] is True and legs[0]["exit_reason"] == "LEG_ROLL"
