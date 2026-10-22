"""B4: IV surface from recorded chains, the surface-vs-flat test, the engine hook, tick replay of exits."""
from __future__ import annotations

import math
import random

import pytest

from app.algo import engine, fno
from app.research import surface as sf
from app.research import tick_replay as tr

IST, DAY = 19800, 86400
DAY0 = 20000 * DAY - IST                       # an IST midnight
OPEN = 33300                                   # 09:15


def _smile(k: float, t: float) -> float:
    return 0.14 + 0.02 * math.sqrt(t * 12) - 0.35 * k + 2.5 * k * k


def _chain(t: int, s: float, iv=_smile, expiries=(3, 10), rng=None, half_pct=0.01, strikes=10):
    """A snapshot quoted off `iv`: the index, then ATM +/- strikes x 50 of each expiry (stamped 14:30, as a master may)."""
    rng = rng or random.Random(0)
    day = (t + IST) // DAY
    out = [{"key": "IDX", "kind": "IDX", "ltp": s}]
    for e in expiries:
        exp = (day + e) * DAY - IST + 14 * 3600 + 1800
        T = fno.years_to(sf.expiry_close(exp), t)
        for j in range(-strikes, strikes + 1):
            K = round(s / 50) * 50 + 50 * j
            for right in ("CE", "PE"):
                px = fno.bs_price(s, K, T, iv(math.log(K / s), T), right)
                half = max(0.05, px * half_pct)
                n = rng.gauss(0, half / 3)
                out.append({"key": f"{right}{K}-{e}", "kind": right, "strike": K, "expiry": exp,
                            "bid": round(px - half + n, 2), "ask": round(px + half + n, 2), "ltp": px, "qts": t - 5})
    return out


def _days(n_days: int, iv=_smile, every_min=15, seed=1):
    rng = random.Random(seed)
    for d in range(n_days):
        s = 24000 * math.exp(rng.gauss(0, 0.01))
        for m in range(0, 375, every_min):
            t = DAY0 + d * DAY + OPEN + m * 60
            s *= math.exp(rng.gauss(0, 0.0008))
            yield t, _chain(t, s, iv, rng=rng)


def test_expiry_close_and_implied_vol_round_trip():
    exp = DAY0 + 3 * DAY + 14 * 3600 + 1800                # 14:30 on the expiry day
    assert sf.expiry_close(exp) == DAY0 + 3 * DAY + 15 * 3600 + 1800
    for right, k in (("CE", 24500), ("PE", 23500)):
        px = fno.bs_price(24000, k, 0.03, 0.17, right)
        assert sf.implied_vol(px, 24000, k, 0.03, right) == pytest.approx(0.17, abs=1e-4)
    assert sf.implied_vol(0.01, 24000, 26000, 0.03, "CE") is None        # below any vol's price


def test_fit_recovers_the_smile_and_reprices_its_own_quotes():
    t = DAY0 + OPEN + 3600
    chain = _chain(t, 24000.0, half_pct=0.002)
    surf = sf.fit(chain, t)
    assert surf.spot == 24000 and len(surf.smiles) == 2
    near = surf.smiles[0]
    assert near["n"] == 21 and near["exp"] == sf.expiry_close(chain[1]["expiry"])
    T = fno.years_to(near["exp"], t)
    for K in (23600, 24000, 24400):
        assert surf.iv(K, 24000, t, chain[1]["expiry"]) == pytest.approx(_smile(math.log(K / 24000), T), abs=0.003)
    inside = 0
    pts = sf.points(chain, t, 24000)
    for p in pts:
        px = fno.bs_price(24000, p["strike"], p["t"], surf.iv(p["strike"], 24000, t, p["exp"]), p["kind"])
        inside += p["bid"] - 0.5 <= px <= p["ask"] + 0.5
    assert inside / len(pts) > 0.95
    # beyond the outermost fitted strike the smile is held flat
    assert surf.iv(30000, 24000, t, chain[1]["expiry"]) == surf.iv(24000 * math.exp(near["kmax"]), 24000, t, chain[1]["expiry"])


def test_only_live_out_of_the_money_quotes_are_used():
    t = DAY0 + OPEN + 3600
    chain = _chain(t, 24000.0)
    pts = sf.points(chain, t, 24000)
    assert all((p["kind"] == "CE") == (p["strike"] >= 24000) for p in pts)          # OTM side only
    stale = [{**c, "qts": t - 3600} if c.get("strike") == 24100 else c for c in chain]
    wide = [{**c, "bid": 0.05, "ask": 50.0} if c.get("strike") == 24200 else c for c in chain]
    one_sided = [{**c, "bid": 0} if c.get("strike") == 24300 else c for c in chain]
    for bad, k in ((stale, 24100), (wide, 24200), (one_sided, 24300)):
        assert all(p["strike"] != k for p in sf.points(bad, t, 24000))
    no_idx = [c for c in chain if c["kind"] != "IDX"]
    assert sf.fit(no_idx, t) is None                                             # no spot: no surface
    fut = {"key": "FUT", "kind": "FUT", "ltp": 24100.0, "expiry": DAY0 + 20 * DAY}
    s_fut = sf.fit(no_idx + [fut], t).spot                                       # ... the future, discounted, stands in
    assert s_fut == pytest.approx(24100 * math.exp(-fno.RISK_FREE * fno.years_to(sf.expiry_close(fut["expiry"]), t)))


def test_between_expiries_total_variance_is_interpolated():
    s = sf.Surface(DAY0, 24000, [{"exp": sf.expiry_close(DAY0 + 5 * DAY), "a": 0.12, "b": 0, "c": 0, "kmin": -1, "kmax": 1},
                                 {"exp": sf.expiry_close(DAY0 + 25 * DAY), "a": 0.18, "b": 0, "c": 0, "kmin": -1, "kmax": 1}])
    t = DAY0
    mid = DAY0 + 15 * DAY
    v = s.iv(24000, 24000, t, mid)
    t1, t2, tq = (fno.years_to(x, t) for x in (sf.expiry_close(DAY0 + 5 * DAY), sf.expiry_close(DAY0 + 25 * DAY), sf.expiry_close(mid)))
    w = 0.12 ** 2 * t1 + (0.18 ** 2 * t2 - 0.12 ** 2 * t1) * (tq - t1) / (t2 - t1)
    assert v == pytest.approx(math.sqrt(w / tq), rel=1e-9)
    assert s.iv(24000, 24000, t, DAY0 + 60 * DAY) == pytest.approx(0.18)          # past the last: flat in vol
    assert sf.Surface.from_dict(s.to_dict()).iv(24000, 24000, t, mid) == pytest.approx(v, rel=1e-6)


def test_the_surface_beats_flat_iv_where_there_is_a_smile_and_not_where_there_is_none():
    smiled = sf.evaluate(_days(12), lag=1, flat=lambda t: 0.15)
    assert smiled["days"] == 12 and smiled["beats_flat"] and smiled["verdict"] == "the surface prices closer than flat IV"
    assert smiled["vs"]["flat"]["days_won"] == 12 and smiled["vs"]["flat"]["ci95"][0] > 0
    assert smiled["overall"]["surface"]["inside_pct"] > smiled["overall"]["flat_atm"]["inside_pct"]
    flat_world = sf.evaluate(_days(12, iv=lambda k, t: 0.15, seed=2), lag=1, flat=lambda t: 0.15)
    assert not flat_world["beats_flat"]                     # nothing to win: no claim
    short = sf.evaluate(_days(3), lag=1)
    assert short["verdict"] == "not enough recorded days yet" and not short["beats_flat"]


def test_each_snapshot_is_priced_with_an_earlier_fit_never_its_own():
    """IV jumps from 15% to 30% at one snapshot: priced with the fit from
    before the jump, that snapshot's error is large; with lag 0 it would not be."""
    t1, t2 = DAY0 + OPEN + 600, DAY0 + OPEN + 1200
    snaps = [(t1, _chain(t1, 24000.0, iv=lambda k, t: 0.15)), (t2, _chain(t2, 24000.0, iv=lambda k, t: 0.30))]
    res = sf.evaluate(snaps, lag=1)
    assert res["quotes"] > 0 and res["overall"]["surface"]["mape_pct"] > 20
    assert res["per_day"][0]["n"] == len(sf.points(snaps[1][1], t2, 24000))
    night = [(t1, snaps[0][1]), (t1 + DAY, _chain(t1 + DAY, 24000.0))]                # never across a night
    assert sf.evaluate(night, lag=1)["quotes"] == 0


# ---- engine: the surface prices the backtest's option legs ------------------

def _option_spec():
    return engine.normalize({
        "symbols": ["NSE:NIFTY50-INDEX"], "timeframe": 5, "direction": "long",
        "entry_long": {"logic": "AND", "conditions": [{"left": {"ind": "PRICE"}, "op": "crosses_above", "right": {"value": 24050}}]},
        "instrument": {"type": "option", "expiry": "current", "expiry_kind": "weekly", "levels_on": "instrument",
                       "iv": {"source": "surface"},
                       "legs_long": [{"right": "CE", "action": "BUY", "strike": "OTM", "steps": 4, "premium": 100, "lots": 1}]},
        "stop_loss": None, "target": None, "sizing": {"mode": "lots", "value": 1},
        "session": {"start": "09:15", "end": "15:00", "square_off": "15:15"}, "costs": {"slippage_pct": 0, "charges": False}})


def test_a_surface_prices_the_legs_on_the_candles_it_covers():
    t0 = DAY0 + OPEN
    closes = [24000, 24010, 24020, 24060, 24080, 24090, 24100, 24110]
    d = {"t": [t0 + i * 300 for i in range(len(closes))], "o": [closes[0]] + closes[:-1], "h": [c + 5 for c in closes],
         "l": [c - 5 for c in closes], "c": closes, "v": [0] * len(closes), "tf_s": 300, "tf_min": 5,
         "meta": {"symbol": "NSE:NIFTY50-INDEX", "name": "NIFTY", "exch": "NSE", "lot": 75, "step": 50.0}}
    spec = _option_spec()
    steep = sf.Surface(t0, 24000, [{"exp": 0, "a": 0.30, "b": 0.0, "c": 0.0, "kmin": -1, "kmax": 1}])
    d["surfaces"] = [None] * 3 + [steep] * (len(closes) - 3)           # recorded from the 4th candle on
    res = engine.run(spec, {"NSE:NIFTY50-INDEX": d})
    x = res["trades"][0]
    i = d["t"].index(x["entry_t"])
    assert isinstance(d["iv"][i], engine.SurfIV) and d["iv"][0] == 0.15     # no snapshot yet: the flat fallback
    leg = x["legs"][0]
    k = float(leg["label"].split()[2])
    exp = fno.expiry_after("NIFTY", "NSE", "weekly", "current", x["entry_t"])
    want = engine.snap(fno.bs_price(d["o"][i], k, fno.years_to(exp, x["entry_t"]), 0.30, "CE"), engine.TICK, 1)
    assert leg["entry"] == pytest.approx(want, abs=0.051)
    flat = engine.run(engine.normalize({**spec, "instrument": {**spec["instrument"], "iv": {"source": "fixed", "value": 15}}}),
                      {"NSE:NIFTY50-INDEX": {k_: v for k_, v in d.items() if k_ not in ("surfaces", "iv", "_prep")}})
    assert flat["trades"][0]["legs"][0]["entry"] < leg["entry"]            # 15% flat prices the same call cheaper
    with pytest.raises(ValueError, match="surface"):
        engine.normalize({**spec, "instrument": {**spec["instrument"], "iv": {"source": "smile"}}})


# ---- tick replay ---------------------------------------------------------------

def _trade(**kw):
    x = {"symbol": "NSE:SBIN-EQ", "side": "BUY", "qty": 100, "entry_t": 1000, "exit_t": 1600, "exit": 103.0,
         "reason": "TARGET", "stop": 98.0, "target": 103.0}
    x.update(kw)
    return x


def test_replay_finds_the_stop_the_candle_path_hid():
    ticks = [(1000, 100.0), (1300, 99.0), (1650, 97.9), (1700, 103.2)]       # exit candle 1600-1900: stop first
    r = tr.replay_trade(_trade(), ticks, 300)
    assert r["tick_reason"] == "SL" and r["tick_t"] == 1650 and not r["agree"] and r["both_levels_in_exit_candle"]
    assert r["pnl_diff"] == pytest.approx((98.0 - 103.0) * 100)
    ok = tr.replay_trade(_trade(), [(1000, 100.0), (1700, 103.1), (1750, 97.0)], 300)
    assert ok["agree"] and ok["tick_reason"] == "TARGET" and ok["pnl_diff"] == 0
    sig = tr.replay_trade(_trade(reason="SIGNAL", exit=101.0), [(1000, 100.0), (1200, 97.5)], 300)
    assert sig["tick_reason"] == "SL" and not sig["agree"]                   # the ticks stopped it out first
    short = tr.replay_trade(_trade(side="SELL", stop=102.0, target=97.0, reason="SL", exit=102.0), [(1000, 100), (1500, 102.5)], 300)
    assert short["agree"] and short["tick_reason"] == "SL"


def test_replay_summary_and_what_it_will_not_replay():
    spec = engine.normalize({"symbols": ["NSE:SBIN-EQ"], "timeframe": 5, "direction": "long",
                             "entry_long": {"logic": "AND", "conditions": [{"left": {"ind": "PRICE"}, "op": ">", "right": {"value": 1}}]},
                             "stop_loss": {"type": "pct", "value": 2}, "target": {"type": "pct", "value": 3}, "trailing": None})
    ticks = {1000: [(1000, 100.0), (1650, 97.9), (1700, 103.2)], 5000: [(5000, 100.0), (5400, 103.5)]}
    res = tr.replay([_trade(), _trade(entry_t=5000, exit_t=5300), _trade(entry_t=9000, exit_t=9300)], spec,
                    lambda s, a, b: ticks.get(a, []))
    assert res["replayed"] == 2 and res["no_ticks"] == 1 and res["agree"] == 1 and res["agree_pct"] == 50.0
    assert res["flipped"] == {"TARGET->SL": 1} and res["ambiguous_candles"] == 1 and res["ambiguous_right"] == 0
    trail = engine.normalize({**spec, "trailing": {"type": "pct", "value": 1}})
    assert tr.replay([], trail, lambda *a: [])["replayable"] is False
    assert "option" in tr.replay([], _option_spec(), lambda *a: [])["why"]


# ---- API ---------------------------------------------------------------------------

class _FakeStore:
    def __init__(self, snaps, prints=None):
        self.snaps, self._prints = snaps, prints or {}
        self.meta = {}
        for _, rows in snaps:
            for c in rows:
                self.meta[c["key"]] = {"chain": "NIFTY", "kind": c["kind"], "strike": c.get("strike"), "expiry": c.get("expiry")}

    def chain_keys(self, name, day):
        return self.meta if name == "NIFTY" and any((t + IST) // DAY == day for t, _ in self.snaps) else {}

    def chain_rows(self, name, start, end):
        return [(t, c["key"], c.get("qts"), c.get("ltp"), c.get("bid"), c.get("ask"), None, None, None, None)
                for t, rows in self.snaps if start <= t < end for c in rows]

    def chain_at(self, name, at):
        best = [s for s in self.snaps if s[0] <= at]
        return (best[-1][0], best[-1][1]) if best and name == "NIFTY" else (None, [])

    def keys_for(self, symbol, a, b):
        return {(a + IST) // DAY: symbol}

    def prints(self, key, a, b):
        return [(t, p, 1, 1) for t, p in self._prints.get(key, []) if a <= t < b]


def test_api_surface_and_its_evaluation(client, monkeypatch):
    from app.algo import ticks

    snaps = list(_days(2, every_min=60))
    monkeypatch.setattr(ticks, "store", lambda: _FakeStore(snaps))
    r = client.get("/api/algo/research/surface", params={"name": "nifty", "at": snaps[3][0] + 30})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["t"] == snaps[3][0] and body["name"] == "NIFTY" and len(body["expiries"]) == 2
    assert all(abs(p["iv"] - p["fit"]) < 0.01 for p in body["points"])
    assert client.get("/api/algo/research/surface", params={"name": "BANKNIFTY"}).status_code == 404
    day = (snaps[0][0] + IST) // DAY
    ymd = lambda d: __import__("datetime").date(1970, 1, 1) + __import__("datetime").timedelta(days=d)  # noqa: E731
    ev = client.post("/api/algo/research/surface/evaluate", json={"name": "NIFTY", "start": str(ymd(day)), "end": str(ymd(day + 1))}).json()
    assert ev["days"] == 2 and ev["verdict"] == "not enough recorded days yet" and ev["quotes"] > 0
    assert client.post("/api/algo/research/surface/evaluate", json={"name": "NIFTY", "start": "2025-01-01", "end": "2025-01-02",
                                                                    "lag": 0}).status_code == 422


def test_api_tick_replay_runs_the_backtest_then_the_prints(client, monkeypatch):
    from app.algo import ticks
    from app.api import algo as api
    from app.tests.test_algo import _cross_spec, _days

    d = _days(5)
    spec = _cross_spec(exit_long=None, target={"type": "pct", "value": 1}, stop_loss={"type": "pct", "value": 1})

    async def fake(spec_, start, end):
        return {s: d for s in spec_["symbols"]}, [], d["t"][0]

    monkeypatch.setattr(api.data, "assemble", fake)
    first = engine.run(spec, {"NSE:TEST-EQ": d})["trades"]
    x = first[0]
    sign = 1 if x["side"] == "BUY" else -1
    # the prints of the first trade touch its stop before anything else; the others have none
    monkeypatch.setattr(ticks, "store", lambda: _FakeStore([], {"NSE:TEST-EQ": [(x["entry_t"], x["entry"]),
                                                                                 (x["entry_t"] + 10, x["stop"] - sign * 0.1)]}))
    r = client.post("/api/algo/research/tick-replay", json={"spec": spec, "start": "2025-01-01", "end": "2025-01-06"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["replayable"] and body["trades"] == len(first) and body["replayed"] == 1 and body["no_ticks"] == len(first) - 1
    assert body["disagreements"][0]["tick_reason"] == "SL" if x["reason"] != "SL" else body["agree"] == 1
