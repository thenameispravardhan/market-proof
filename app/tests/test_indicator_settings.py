"""Indicator maths + param checks behind the algo builder's indicator settings."""
import random

import pytest

from app.algo import engine
from app.algo import indicators as ind


def _bars(n=400, seed=3, jumpy=False):
    rnd = random.Random(seed)
    o, h, l, c, v, t = [], [], [], [], [], []
    p = 100.0
    for i in range(n):
        op = p
        # jumpy: one bar in ten is a wide-range bar, the case where the
        # supertrend flip depends on using this bar's bands
        p = max(1.0, p + rnd.uniform(-2, 2) * (6 if jumpy and rnd.random() < 0.1 else 1))
        o.append(op)
        h.append(max(op, p) + rnd.random())
        l.append(min(op, p) - rnd.random())
        c.append(p)
        v.append(rnd.randint(1, 1000))
        t.append(1_700_000_000 + i * 300)
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v, "tf_s": 300}


def _tv_supertrend(d, n, mult):
    """TradingView ta.supertrend, written out the way the Pine reference does it
    (+1 = uptrend here, starting up like ours)."""
    c, a, hl2 = d["c"], ind.atr(d, n), ind.source(d, "hl2")
    val, dirn = [None] * len(c), [None] * len(c)
    lower = upper = None
    trend = 1
    for i in range(len(c)):
        if a[i] is None:
            continue
        lo, up = hl2[i] - mult * a[i], hl2[i] + mult * a[i]
        if lower is not None:
            lo = lo if lo > lower or c[i - 1] < lower else lower
            up = up if up < upper or c[i - 1] > upper else upper
            if trend == -1:
                trend = 1 if c[i] > up else -1
            else:
                trend = -1 if c[i] < lo else 1
        lower, upper = lo, up
        val[i] = lower if trend == 1 else upper
        dirn[i] = float(trend)
    return val, dirn


@pytest.mark.parametrize("seed,mult", [(s, m) for s in range(8) for m in (0.5, 1.0, 3.0)])
def test_supertrend_flips_on_the_current_bars_bands(seed, mult):
    d = _bars(seed=seed, jumpy=True)
    got = ind.supertrend(d, 10, mult)
    val, dirn = _tv_supertrend(d, 10, mult)
    assert got["direction"] == dirn
    assert got["value"] == pytest.approx(val)


def test_shift_keeps_the_length_when_offset_exceeds_the_series():
    assert ind.shift([1.0, 2.0, 3.0], 5) == [None, None, None]
    assert ind.shift([1.0, 2.0, 3.0], 1) == [None, 1.0, 2.0]
    assert ind.shift([1.0, 2.0, 3.0], 0) == [1.0, 2.0, 3.0]


def test_long_offset_on_a_short_series_does_not_misalign():
    d = _bars(n=5)
    out = ind.compute(d, "ROC", {"period": 10})["value"]
    assert out == [None] * 5


@pytest.mark.parametrize("name,params,msg", [
    ("SMA", {"period": 0}, "between 1"),
    ("SMA", {"period": "abc"}, "must be a number"),
    ("SMA", {"period": float("nan")}, "finite"),
    ("SMA", {"period": True}, "must be a number"),
    ("SMA", {"source": "vwap"}, "unknown source"),
    ("BBANDS", {"multiplier": 0}, "> 0"),
    ("PSAR", {"maximum": 5}, "<= 1"),
    ("VPROFILE", {"value_area": 150}, "between 1 and 100"),
])
def test_bad_params_are_rejected(name, params, msg):
    with pytest.raises(ValueError, match=msg):
        ind.normalize_params(name, params)


def test_params_are_coerced_and_unknown_keys_dropped():
    p = ind.normalize_params("BBANDS", {"period": "21", "multiplier": "2.5", "junk": 1})
    assert p == {"period": 21, "multiplier": 2.5, "source": "close"}
    assert ind.normalize_params("RSI", {"period": 14.4})["period"] == 14


def _spec(params, offset=0):
    return {"symbols": ["NSE:TEST-EQ"], "timeframe": 5, "direction": "long",
            "entry_long": {"logic": "AND", "conditions": [
                {"left": {"ind": "RSI", "params": params, "offset": offset}, "op": "<", "right": {"value": 30}}]}}


def test_strategy_with_a_bad_indicator_length_fails_on_save():
    with pytest.raises(ValueError, match="RSI: period"):
        engine.normalize(_spec({"period": 0}))


def test_strategy_params_are_stored_normalized():
    s = engine.normalize(_spec({"period": "9"}, offset="2"))
    left = s["entry_long"]["conditions"][0]["left"]
    assert left["params"] == {"period": 9}
    assert left["offset"] == 2


def test_negative_offset_is_rejected():
    with pytest.raises(ValueError, match="offset"):
        engine.normalize(_spec({"period": 14}, offset=-1))
