"""B2 pairs: rolling pair math on two symbols, the pair finder, a one-leg pair strategy."""
from __future__ import annotations

import math
import random
import statistics

import pytest

from app.algo import engine
from app.research import pairs
from app.tests.test_algo import DAY0


def _cointegrated(n: int, beta: float = 1.5, seed: int = 1, phi: float = 0.85):
    """B a random walk; A = beta x B + a mean-reverting gap (log prices)."""
    rng = random.Random(seed)
    lb, e = [0.0], [0.0]
    for _ in range(n - 1):
        lb.append(lb[-1] + rng.gauss(0, 0.01))
        e.append(phi * e[-1] + rng.gauss(0, 0.005))
    return [80 * math.exp(beta * x + y) for x, y in zip(lb, e)], [100 * math.exp(x) for x in lb]


def _random_walk(n: int, seed: int):
    rng = random.Random(seed)
    x = [0.0]
    for _ in range(n - 1):
        x.append(x[-1] + rng.gauss(0, 0.01))
    return [50 * math.exp(v) for v in x]


def test_pair_math_matches_a_plain_least_squares_fit():
    a, b = _cointegrated(1200)
    n, i = 150, 1000
    xs, ys = [math.log(v) for v in b[i - n + 1:i + 1]], [math.log(v) for v in a[i - n + 1:i + 1]]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    beta = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
    res = [y - (my - beta * mx) - beta * x for x, y in zip(xs, ys)]
    sd = math.sqrt(sum(r * r for r in res) / (n - 2))
    assert engine._pair("beta", a, b, n)[i] == pytest.approx(beta, rel=1e-9)
    assert engine._pair("spread", a, b, n)[i] == pytest.approx(res[-1] * 100, rel=1e-6)
    assert engine._pair("pair_z", a, b, n)[i] == pytest.approx(res[-1] / sd, rel=1e-6)
    lx, ly = [math.log(v) for v in b[i - n:i + 1]], [math.log(v) for v in a[i - n:i + 1]]
    rx, ry = [q - p for p, q in zip(lx, lx[1:])], [q - p for p, q in zip(ly, ly[1:])]
    assert engine._pair("corr", a, b, n)[i] == pytest.approx(statistics.correlation(rx, ry), rel=1e-9)
    assert 1.3 < statistics.mean(v for v in engine._pair("beta", a, b, n) if v is not None) < 1.7


def test_pair_math_needs_a_full_window_and_never_reaches_across_a_gap():
    a, b = _cointegrated(400)
    z = engine._pair("pair_z", a, b, 50)
    assert z[:49] == [None] * 49 and z[49] is not None
    b[200] = None                                       # the other symbol missed a bar
    z = engine._pair("pair_z", a, b, 50)
    assert z[199] is not None and z[200:250] == [None] * 50 and z[250] is not None   # refills from bar 201
    assert engine._pair("beta", [10.0] * 60, [5.0] * 60, 20)[-1] is None      # B flat: no fit


def _spec(**z):
    zop = {"math": "pair_z", "a": {"ind": "PRICE"}, "b": {"ind": "PRICE", "symbol": "NSE:B-EQ"}, **z}
    return {"symbols": ["NSE:A-EQ"], "timeframe": 60, "direction": "long",
            "entry_long": {"logic": "AND", "conditions": [{"left": zop, "op": "<", "right": {"value": -2}}]}}


def test_the_window_is_validated_and_warms_up():
    s = engine.normalize(_spec())
    assert s["entry_long"]["conditions"][0]["left"]["n"] == 100                 # default
    assert engine.warmup_bars(engine.normalize(_spec(n=200))) == 3 * 200 + 5
    with pytest.raises(ValueError, match="at least|>= 10"):
        engine.normalize(_spec(n=5))
    with pytest.raises(ValueError, match="at most 500"):
        engine.normalize(_spec(n=600))
    plain = _spec(n=50)
    plain["entry_long"]["conditions"][0]["left"]["math"] = "-"
    assert "n" not in engine.normalize(plain)["entry_long"]["conditions"][0]["left"]   # only pairs carry a window
    assert engine.ref_frames(s) == {"NSE:B-EQ": {60}}                                 # B's candles get loaded


def _bars(t, c, tf=3600):
    o = [c[0]] + c[:-1]
    return {"t": list(t), "o": o, "h": [max(p, q) * 1.001 for p, q in zip(o, c)], "l": [min(p, q) * 0.999 for p, q in zip(o, c)],
            "c": list(c), "v": [1000] * len(c), "tf_s": tf, "tf_min": tf // 60}


def _hourly(days: int):
    return [DAY0 + k * 86400 + i * 3600 for k in range(days) for i in range(6)]


def test_finder_confirms_the_real_pair_and_not_the_lucky_ones():
    t = _hourly(100)
    a, b = _cointegrated(len(t), beta=1.3, seed=2)
    closes = {"IDX": {"t": t, "c": b}, "PAIR": {"t": t, "c": a}}
    for k in range(12):
        closes[f"R{k}"] = {"t": t, "c": _random_walk(len(t), seed=10 + k)}
    res = pairs.scan(closes, "IDX")
    assert res["tested"] == 13 and res["skipped"] == 0 and res["expected_by_luck"] == 0.7
    top = res["pairs"][0]
    assert top["a"] == "PAIR" and top["b"] == "IDX" and top["cointegrated"] and top["holds_out"]
    assert top["beta"] == pytest.approx(1.3, abs=0.1) and 1 < top["half_life"] < 10 and top["window"] >= 20
    assert res["confirmed"] == 1 and all(not r["holds_out"] or not r["cointegrated"] for r in res["pairs"][1:])
    peers = pairs.scan(closes)                                # no anchor: every pair of the 14
    assert peers["tested"] == 14 * 13 // 2
    # too few common bars: skipped, not scored
    short = pairs.scan({"IDX": {"t": t, "c": b}, "NEW": {"t": t[-30:], "c": a[-30:]}}, "IDX")
    assert short["skipped"] == 1 and short["pairs"] == []


def test_a_one_leg_pair_strategy_trades_the_spread():
    t = _hourly(120)
    a, b = _cointegrated(len(t), beta=1.2, seed=4, phi=0.8)
    st = pairs.pair_stats(t, a, t, b)
    spec = engine.normalize(pairs.spec_for("NSE:A-EQ", "NSE:B-EQ", st["window"], 60))
    assert spec["symbols"] == ["NSE:A-EQ"] and spec["session"]["square_off"] is None   # carries overnight
    A, B = _bars(t, a), _bars(t, b)
    B["meta"] = {"symbol": "NSE:B-EQ"}
    A["xs"] = {"NSE:B-EQ": B}
    res = engine.run(spec, {"NSE:A-EQ": A})
    assert res["stats"]["trades"] >= 5
    z = engine._pair("pair_z", a, b, st["window"])
    idx = {tt: i for i, tt in enumerate(t)}
    for x in res["trades"]:
        i = idx[x["entry_t"]] - 1                     # signal on the bar before the fill
        assert (z[i] < -2) if x["side"] == "BUY" else (z[i] > 2)


# ---- API ----------------------------------------------------------------------

def test_api_scans_pairs_and_hands_back_a_spec(client, monkeypatch):
    from app.api import algo as api

    t = _hourly(100)
    a, b = _cointegrated(len(t), beta=1.3, seed=2)
    series = {"NIFTYBANK-INDEX": b, "HDFCBANK": a, "SBIN": _random_walk(len(t), 7)}

    async def ensure(fy, start, end):
        return {"key": api.data.store_key(fy), "note": None}

    def load(key, tf, start, end):
        if key not in series:
            return None
        return {"t": list(t), "c": series[key]}

    monkeypatch.setattr(api.data, "ensure", ensure)
    monkeypatch.setattr(api.data, "load", load)
    r = client.post("/api/algo/research/pairs", json={"symbols": ["HDFCBANK", "SBIN", "NOPE"], "anchor": "NSE:NIFTYBANK-INDEX"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tested"] == 2 and body["timeframe"] == 60 and body["confirmed"] == 1
    assert [n["symbol"] for n in body["notes"]] == ["NSE:NOPE-EQ"]
    top = body["pairs"][0]
    assert top["a"] == "NSE:HDFCBANK-EQ" and top["b"] == "NSE:NIFTYBANK-INDEX"
    assert engine.normalize(top["spec"])["entry_long"]["conditions"][0]["left"]["b"]["symbol"] == "NSE:NIFTYBANK-INDEX"
    bad = client.post("/api/algo/research/pairs", json={"symbols": ["SBIN"]})
    assert bad.status_code == 422 and "2-30 symbols" in bad.json()["detail"]
    bad = client.post("/api/algo/research/pairs", json={"symbols": ["SBIN", "HDFCBANK"], "timeframe": 1440})
    assert bad.status_code == 422 and "timeframe" in bad.json()["detail"]
    bad = client.post("/api/algo/research/pairs", json={"symbols": ["SBIN"], "anchor": "NSE:NOPE-INDEX"})
    assert bad.status_code == 422 and "NOPE-INDEX" in bad.json()["detail"]
    bad = client.post("/api/algo/research/pairs", json={"symbols": [f"S{k}" for k in range(30)], "anchor": "X", "timeframe": 1, "days": 1000})
    assert bad.status_code == 422 and "too much data" in bad.json()["detail"]
