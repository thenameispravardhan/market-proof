"""B3 two-symbol positions: a pair leg opened and closed with the position, priced on its own candles."""
from __future__ import annotations

import statistics

import pytest

from app.algo import engine
from app.research import pairs
from app.tests.test_algo import _cross_spec, bars
from app.tests.test_research_pairs import _bars, _cointegrated, _hourly

REF = "NSE:REF-EQ"
BANK = "NSE:NIFTYBANK-INDEX"


def _leg(**kw):
    return {"symbol": REF, "kind": "EQ", "ratio": {"type": "fixed", "value": 1.0}, **kw}


def _spec(**kw):
    s = _cross_spec(stop_loss=None, target=None, sizing={"mode": "qty", "value": 10})
    s.pop("pair_leg", None)
    return engine.normalize({**s, "pair_leg": _leg(), **kw})


def test_pair_leg_is_validated():
    s = _spec()
    assert s["pair_leg"] == {"symbol": REF, "kind": "EQ", "ratio": {"type": "fixed", "value": 1.0}, "expiry": "current"}
    assert engine.ref_frames(s) == {REF: {5}}                          # its candles get loaded
    beta = _spec(pair_leg={"symbol": REF})["pair_leg"]
    assert beta["kind"] == "FUT" and beta["ratio"] == {"type": "beta", "n": 100}
    base = _cross_spec(stop_loss=None, target=None)
    for bad, msg in [
        ({"pair_leg": _leg(), "stop_loss": {"type": "pct", "value": 1}}, "switch off the stop loss"),
        ({"pair_leg": _leg(), "target": {"type": "pct", "value": 1}}, "switch off the target"),
        ({"pair_leg": _leg(symbol="NSE:TEST-EQ")}, "another symbol"),
        ({"pair_leg": _leg(kind="OPT")}, "FUT .* or EQ"),
        ({"pair_leg": _leg(ratio={"type": "fixed", "value": 50})}, "at most 20"),
        ({"pair_leg": _leg(ratio={"type": "beta", "n": 5})}, ">= 10"),
        ({"pair_leg": _leg(ratio={"type": "vol"})}, "ratio is"),
        ({"pair_leg": _leg(), "entry_order": {"type": "pullback"}}, "enters at market"),
        ({"pair_leg": _leg(), "scale_out": [{"type": "pct", "value": 1, "qty_pct": 50}]}, "no scale-out"),
        ({"pair_leg": _leg(), "instrument": {"type": "option"}}, "not options"),
        ({"pair_leg": {"kind": "FUT"}}, "pair_leg is"),          # {} = no pair leg, like absent
    ]:
        with pytest.raises(ValueError, match=msg):
            engine.normalize({**base, **bad})


def test_both_legs_open_and_close_together_and_the_pnl_adds_up():
    closes = [100, 101, 102, 106, 108, 110, 104, 102, 101]
    ref = [200, 200, 200, 200, 210, 220, 215, 205, 190]
    a, b = bars(closes, spread=0), bars(ref, spread=0)
    b["meta"] = {"symbol": REF}
    a["xs"] = {REF: b}
    res = engine.run(_spec(), {"NSE:TEST-EQ": a})
    assert len(res["trades"]) == 1
    x = res["trades"][0]
    assert x["entry_t"] == a["t"][4] and x["exit_t"] == a["t"][8] and x["reason"] == "SIGNAL"
    la, lb = x["legs"]
    assert la == {"symbol": "NSE:TEST-EQ", "label": "NSE:TEST-EQ", "side": "BUY", "qty": 10, "entry": 106, "exit": 102}
    # short ₹1 of REF per ₹1 of TEST: 10 x 106 / 200 -> 5 shares, in at bar 4's open, out at bar 8's
    assert lb == {"symbol": REF, "label": REF, "side": "SELL", "qty": 5, "entry": 200, "exit": 205}
    assert x["gross"] == pytest.approx((102 - 106) * 10 - (205 - 200) * 5)
    assert x["hedge"] == {"ratio": 1.0, "actual": round(5 * 200 / 1060, 3)}
    assert x["margin"] == pytest.approx(10 * 106 + 5 * 200)            # leverage 1: both legs' value
    # worst close MTM in % of both legs' value: bar 8 closes at 101 vs 190 is not reached (exit at its open)
    worst = min((c - 106) * 10 - (r - 200) * 5 for c, r in zip(closes[4:8], ref[4:8]))
    assert x["mae_pct"] == pytest.approx(round(worst / 2060 * 100, 3))


def test_the_hedge_ratio_is_the_one_known_at_the_signal_close():
    t = _hourly(60)
    a, b = _cointegrated(len(t), beta=1.2, seed=4, phi=0.8)
    A, B = _bars(t, a), _bars(t, [v * 10 for v in b])
    B["meta"] = {"symbol": REF}
    A["xs"] = {REF: B}
    spec = pairs.spec_for("NSE:A-EQ", REF, 20, 60)
    spec.update(stop_loss=None, sizing={"mode": "qty", "value": 1000}, pair_leg={"symbol": REF, "kind": "EQ", "ratio": {"type": "beta", "n": 50}})
    res = engine.run(engine.normalize(spec), {"NSE:A-EQ": A})
    beta = engine._pair("beta", A["c"], B["c"], 50)
    idx = {tt: i for i, tt in enumerate(t)}
    assert res["trades"]
    for x in res["trades"]:
        i = idx[x["entry_t"]]
        assert x["hedge"]["ratio"] == round(beta[i - 1], 4)           # the signal candle's close, not the fill's
        a_leg, b_leg = x["legs"]
        assert b_leg["qty"] == round(beta[i - 1] * a_leg["qty"] * a_leg["entry"] / b_leg["entry"])
    assert res["stats"]["skipped"]["pair_no_ratio"] >= 0


def _pair_data(days=120):
    t = _hourly(days)
    a, b = _cointegrated(len(t), beta=1.2, seed=4, phi=0.8)
    A, B = _bars(t, a), _bars(t, [v * 500 for v in b])                  # B at an index-like level
    B["meta"] = {"symbol": BANK}
    A["xs"] = {BANK: B}
    return A


def test_a_futures_hedge_rounds_to_lots_and_cuts_the_swings():
    base = pairs.spec_for("NSE:A-EQ", BANK, 20, 60)
    base.update(stop_loss=None, sizing={"mode": "pct_equity", "value": 50}, portfolio={"capital": 1_000_000, "leverage": 5})
    one = engine.run(engine.normalize(base), {"NSE:A-EQ": _pair_data()})
    hedged = engine.run(engine.normalize({**base, "pair_leg": {"symbol": BANK, "kind": "FUT", "ratio": {"type": "beta", "n": 100}}}),
                        {"NSE:A-EQ": _pair_data()})
    assert hedged["trades"]
    for x in hedged["trades"]:
        fut = x["legs"][1]
        assert fut["symbol"] == BANK and fut["label"].startswith("BANKNIFTY FUT ") and fut["qty"] % 30 == 0
        assert fut["side"] == ("SELL" if x["side"] == "BUY" else "BUY")
    sd = lambda r: statistics.pstdev(x["net"] for x in r["trades"])  # noqa: E731
    assert sd(hedged) < sd(one) / 2
    tiny = engine.run(engine.normalize({**base, "portfolio": {"capital": 20_000, "leverage": 5},
                                        "pair_leg": {"symbol": BANK, "kind": "FUT", "ratio": {"type": "beta", "n": 100}}}),
                      {"NSE:A-EQ": _pair_data()})
    assert tiny["trades"] == [] and tiny["stats"]["skipped"]["pair_too_small"] > 0     # under one lot: not traded half-hedged


def test_an_mtm_stop_is_judged_on_the_close_and_filled_at_the_next_open():
    closes = [100, 101, 102, 106, 100, 99, 98, 97, 96]
    ref = [200] * 9
    a, b = bars(closes, spread=5), bars(ref, spread=5)         # wide candles: an intrabar check would fire on bar 4
    b["meta"] = {"symbol": REF}
    a["xs"] = {REF: b}
    res = engine.run(_spec(exit_long=None, mtm={"stop": 50}), {"NSE:TEST-EQ": a})
    x = res["trades"][0]
    assert x["reason"] == "MTM_SL" and x["exit_t"] == a["t"][5] and x["legs"][0]["exit"] == a["o"][5]


def test_a_pair_strategy_cannot_be_switched_on_yet(client, isolated_db):
    spec = _spec()
    s = client.post("/api/algo/strategies", json={"name": "pair", "spec": spec}).json()
    r = client.put(f"/api/algo/strategies/{s['id']}", json={"enabled": True})
    assert r.status_code == 422 and "backtest-only" in r.json()["detail"]
    plain = client.post("/api/algo/strategies", json={"name": "plain", "spec": _cross_spec()}).json()
    assert client.put(f"/api/algo/strategies/{plain['id']}", json={"enabled": True}).status_code == 200
    r = client.put(f"/api/algo/strategies/{plain['id']}", json={"spec": spec})     # a pair leg onto a running one
    assert r.status_code == 422
    saved = {x["id"]: x for x in client.get("/api/algo/strategies").json()["strategies"]}
    assert saved[plain["id"]]["spec"].get("pair_leg") is None and saved[plain["id"]]["version"] == 1
