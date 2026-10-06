"""Technical indicators — pure Python, list in, list out.

Every function takes plain lists (oldest -> newest) and returns lists of the
same length, with None for warm-up bars. A value at index i depends only on
bars <= i, so nothing here can leak the future into a backtest. Formulas
follow TradingView's built-ins (Wilder smoothing for RSI/ATR/ADX, population
stdev for Bollinger) so a backtest here agrees with the chart the operator
checks it against.

No numpy: the box has 2 GB and a year of 5-minute bars is ~19k points — a
pure-Python pass over that is milliseconds, and nothing new gets installed.

`d` everywhere is one symbol's bars: {"t","o","h","l","c","v": lists, "tf_s": int}
with `t` the bar START in epoch seconds.
"""
from __future__ import annotations

import math
from collections import deque
from typing import Any, Callable, Optional

S = list[Optional[float]]
IST_OFFSET = 19800          # +05:30 in seconds
SESSION_OPEN_S = 33300      # 09:15 IST, seconds after IST midnight


def _first(x: S) -> int:
    for i, v in enumerate(x):
        if v is not None:
            return i
    return len(x)


def _zip(fn: Callable[..., float], *cols: S) -> S:
    """Elementwise fn over aligned series; None wherever any input is None."""
    out: S = []
    for vals in zip(*cols):
        if any(v is None for v in vals):
            out.append(None)
        else:
            try:
                out.append(fn(*vals))
            except ZeroDivisionError:
                out.append(None)
    return out


def shift(x: S, n: int) -> S:
    return ([None] * n + x[:-n]) if n > 0 else list(x)


# ---- moving averages -------------------------------------------------------

def sma(x: S, n: int) -> S:
    out: S = [None] * len(x)
    s0, s = _first(x), 0.0
    for i in range(s0, len(x)):
        s += x[i]
        if i - s0 >= n:
            s -= x[i - n]
        if i - s0 >= n - 1:
            out[i] = s / n
    return out


def ema(x: S, n: int, alpha: Optional[float] = None) -> S:
    """SMA-seeded EMA (TA-Lib convention; converges to TradingView's)."""
    out: S = [None] * len(x)
    s0 = _first(x)
    if len(x) - s0 < n:
        return out
    a = alpha if alpha is not None else 2.0 / (n + 1)
    prev = sum(x[s0:s0 + n]) / n
    out[s0 + n - 1] = prev
    for i in range(s0 + n, len(x)):
        prev = a * x[i] + (1 - a) * prev
        out[i] = prev
    return out


def rma(x: S, n: int) -> S:
    """Wilder's smoothing (TradingView ta.rma)."""
    return ema(x, n, 1.0 / n)


def wma(x: S, n: int) -> S:
    out: S = [None] * len(x)
    den = n * (n + 1) / 2
    for i in range(_first(x) + n - 1, len(x)):
        out[i] = sum(x[i - n + 1 + k] * (k + 1) for k in range(n)) / den
    return out


def hma(x: S, n: int) -> S:
    diff = _zip(lambda a, b: 2 * a - b, wma(x, max(1, n // 2)), wma(x, n))
    return wma(diff, max(1, int(math.sqrt(n))))


def dema(x: S, n: int) -> S:
    e1 = ema(x, n)
    return _zip(lambda a, b: 2 * a - b, e1, ema(e1, n))


def tema(x: S, n: int) -> S:
    e1 = ema(x, n)
    e2 = ema(e1, n)
    return _zip(lambda a, b, c: 3 * a - 3 * b + c, e1, e2, ema(e2, n))


def stdev(x: S, n: int) -> S:
    """Population stdev over n (TradingView ta.stdev default)."""
    out: S = [None] * len(x)
    s0, s, s2 = _first(x), 0.0, 0.0
    for i in range(s0, len(x)):
        s += x[i]
        s2 += x[i] * x[i]
        if i - s0 >= n:
            s -= x[i - n]
            s2 -= x[i - n] * x[i - n]
        if i - s0 >= n - 1:
            out[i] = math.sqrt(max(0.0, s2 / n - (s / n) ** 2))
    return out


def _roll(x: S, n: int, better: Callable[[float, float], bool]) -> S:
    """Rolling max/min in O(N) with a monotonic deque."""
    out: S = [None] * len(x)
    q: deque[int] = deque()
    s0 = _first(x)
    for i in range(s0, len(x)):
        while q and not better(x[q[-1]], x[i]):
            q.pop()
        q.append(i)
        if q[0] <= i - n:
            q.popleft()
        if i - s0 >= n - 1:
            out[i] = x[q[0]]
    return out


def highest(x: S, n: int) -> S:
    return _roll(x, n, lambda a, b: a > b)


def lowest(x: S, n: int) -> S:
    return _roll(x, n, lambda a, b: a < b)


def rolling_sum(x: S, n: int) -> S:
    return _zip(lambda m: m * n, sma(x, n))


# ---- the indicators --------------------------------------------------------

def source(d: dict, name: str) -> S:
    o, h, l, c, v = d["o"], d["h"], d["l"], d["c"], d["v"]
    if name == "open":
        return o
    if name == "high":
        return h
    if name == "low":
        return l
    if name == "volume":
        return [float(x) for x in v]
    if name == "hl2":
        return [(a + b) / 2 for a, b in zip(h, l)]
    if name == "hlc3":
        return [(a + b + k) / 3 for a, b, k in zip(h, l, c)]
    if name == "ohlc4":
        return [(a + b + k + m) / 4 for a, b, k, m in zip(o, h, l, c)]
    if name == "close":
        return c
    if name == "range":
        return [a - b for a, b in zip(h, l)]
    raise ValueError(f"unknown source {name!r}")


def true_range(d: dict) -> S:
    h, l, c = d["h"], d["l"], d["c"]
    return [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
                            for i in range(1, len(c))] if c else []


def atr(d: dict, period: int = 14) -> S:
    return rma(true_range(d), period)


def rsi(x: S, n: int) -> S:
    ch: S = [None] + _zip(lambda a, b: b - a, x[:-1], x[1:])
    au = rma([None if c is None else max(c, 0.0) for c in ch], n)
    ad = rma([None if c is None else max(-c, 0.0) for c in ch], n)
    return _zip(lambda u, dn: 100.0 if dn == 0 else 100 - 100 / (1 + u / dn), au, ad)


def macd(x: S, fast: int, slow: int, signal: int) -> dict[str, S]:
    line = _zip(lambda a, b: a - b, ema(x, fast), ema(x, slow))
    sig = ema(line, signal)
    return {"macd": line, "signal": sig, "hist": _zip(lambda a, b: a - b, line, sig)}


def bollinger(x: S, n: int, mult: float) -> dict[str, S]:
    mid, sd = sma(x, n), stdev(x, n)
    up = _zip(lambda m, s: m + mult * s, mid, sd)
    lo = _zip(lambda m, s: m - mult * s, mid, sd)
    return {"upper": up, "middle": mid, "lower": lo,
            "width": _zip(lambda u, lw, m: (u - lw) / m * 100, up, lo, mid),
            "percent_b": _zip(lambda p, u, lw: (p - lw) / (u - lw), x, up, lo)}


def stoch_raw(c: S, h: S, l: S, n: int) -> S:
    return _zip(lambda cc, hh, ll: 100 * (cc - ll) / (hh - ll) if hh != ll else 50.0,
                c, highest(h, n), lowest(l, n))


def adx(d: dict, n: int) -> dict[str, S]:
    h, l = d["h"], d["l"]
    pdm: S = [None]
    mdm: S = [None]
    for i in range(1, len(h)):
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
    tr = [None] + true_range(d)[1:]
    a = rma(tr, n)
    pdi = _zip(lambda p, t: 100 * p / t, rma(pdm, n), a)
    mdi = _zip(lambda m, t: 100 * m / t, rma(mdm, n), a)
    dx = _zip(lambda p, m: 100 * abs(p - m) / (p + m) if p + m else 0.0, pdi, mdi)
    return {"adx": rma(dx, n), "plus_di": pdi, "minus_di": mdi}


def supertrend(d: dict, n: int, mult: float) -> dict[str, S]:
    """TradingView ta.supertrend; direction +1 = uptrend, -1 = downtrend."""
    c, a = d["c"], atr(d, n)
    hl2 = source(d, "hl2")
    val: S = [None] * len(c)
    dirn: S = [None] * len(c)
    up = dn = None
    trend = 1
    for i in range(len(c)):
        if a[i] is None:
            continue
        bu, bd = hl2[i] - mult * a[i], hl2[i] + mult * a[i]
        if up is not None:
            bu = max(bu, up) if c[i - 1] > up else bu
            bd = min(bd, dn) if c[i - 1] < dn else bd
            if trend == -1 and c[i] > dn:
                trend = 1
            elif trend == 1 and c[i] < up:
                trend = -1
        up, dn = bu, bd
        val[i] = up if trend == 1 else dn
        dirn[i] = float(trend)
    return {"value": val, "direction": dirn}


def psar(d: dict, start: float, inc: float, mx: float) -> S:
    h, l = d["h"], d["l"]
    out: S = [None] * len(h)
    if len(h) < 3:
        return out
    up = h[1] >= h[0]
    sar = min(l[0], l[1]) if up else max(h[0], h[1])
    ep = max(h[0], h[1]) if up else min(l[0], l[1])
    af = start
    out[1] = sar
    for i in range(2, len(h)):
        sar = sar + af * (ep - sar)
        if up:
            sar = min(sar, l[i - 1], l[i - 2])
            if l[i] < sar:
                up, sar, ep, af = False, ep, l[i], start
            elif h[i] > ep:
                ep, af = h[i], min(af + inc, mx)
        else:
            sar = max(sar, h[i - 1], h[i - 2])
            if h[i] > sar:
                up, sar, ep, af = True, ep, h[i], start
            elif l[i] < ep:
                ep, af = l[i], min(af + inc, mx)
        out[i] = sar
    return out


def cci(d: dict, n: int) -> S:
    tp = source(d, "hlc3")
    m = sma(tp, n)
    out: S = [None] * len(tp)
    for i in range(n - 1, len(tp)):
        md = sum(abs(tp[k] - m[i]) for k in range(i - n + 1, i + 1)) / n
        out[i] = (tp[i] - m[i]) / (0.015 * md) if md else 0.0
    return out


def mfi(d: dict, n: int) -> S:
    tp, v = source(d, "hlc3"), d["v"]
    pos: S = [None] + [tp[i] * v[i] if tp[i] > tp[i - 1] else 0.0 for i in range(1, len(tp))]
    neg: S = [None] + [tp[i] * v[i] if tp[i] < tp[i - 1] else 0.0 for i in range(1, len(tp))]
    return _zip(lambda p, q: 100.0 if q == 0 else 100 - 100 / (1 + p / q),
                rolling_sum(pos, n), rolling_sum(neg, n))


def obv(d: dict) -> S:
    c, v = d["c"], d["v"]
    out: S = [0.0] * len(c)
    for i in range(1, len(c)):
        out[i] = out[i - 1] + (v[i] if c[i] > c[i - 1] else -v[i] if c[i] < c[i - 1] else 0)
    return out


def aroon(d: dict, n: int) -> dict[str, S]:
    h, l = d["h"], d["l"]
    up: S = [None] * len(h)
    dn: S = [None] * len(h)
    for i in range(n, len(h)):
        win_h, win_l = h[i - n:i + 1], l[i - n:i + 1]
        # 100 * (n - bars since the extreme) / n; ties take the most recent
        up[i] = 100 * max(range(n + 1), key=lambda k: (win_h[k], k)) / n
        dn[i] = 100 * max(range(n + 1), key=lambda k: (-win_l[k], k)) / n
    return {"up": up, "down": dn, "oscillator": _zip(lambda a, b: a - b, up, dn)}


def cmf(d: dict, n: int) -> S:
    h, l, c, v = d["h"], d["l"], d["c"], d["v"]
    mfv: S = [((c[i] - l[i]) - (h[i] - c[i])) / (h[i] - l[i]) * v[i] if h[i] != l[i] else 0.0
              for i in range(len(c))]
    return _zip(lambda a, b: a / b if b else 0.0, rolling_sum(mfv, n),
                rolling_sum([float(x) for x in v], n))


def ichimoku(d: dict, conv: int, base: int, span_b: int) -> dict[str, S]:
    h, l = d["h"], d["l"]
    mid = lambda n: _zip(lambda a, b: (a + b) / 2, highest(h, n), lowest(l, n))  # noqa: E731
    tenkan, kijun = mid(conv), mid(base)
    # The cloud plotted at bar i was computed base-1 bars earlier — shifting it
    # here is what keeps "price above cloud" free of lookahead.
    disp = base - 1
    return {"tenkan": tenkan, "kijun": kijun,
            "span_a": shift(_zip(lambda a, b: (a + b) / 2, tenkan, kijun), disp),
            "span_b": shift(mid(span_b), disp)}


def zscore(x: S, n: int) -> S:
    """(x - SMA) / stdev over n — how stretched price is, in sigmas."""
    return _zip(lambda v, m, sd: (v - m) / sd if sd else 0.0, x, sma(x, n), stdev(x, n))


def linreg(x: S, n: int) -> dict[str, S]:
    """Least-squares line through the last n points: its end value, and its
    slope as % of price per bar (trend strength that ignores noise)."""
    val: S = [None] * len(x)
    slope: S = [None] * len(x)
    sx = n * (n - 1) / 2
    sxx = (n - 1) * n * (2 * n - 1) / 6
    den = n * sxx - sx * sx
    for i in range(_first(x) + n - 1, len(x)):
        w = x[i - n + 1:i + 1]
        sy = sum(w)
        sxy = sum(k * y for k, y in enumerate(w))
        b = (n * sxy - sx * sy) / den if den else 0.0
        a = (sy - b * sx) / n
        val[i] = a + b * (n - 1)
        slope[i] = b / val[i] * 100 if val[i] else 0.0
    return {"value": val, "slope_pct": slope}


def hist_vol(d: dict, n: int) -> S:
    """Annualised close-to-close volatility (%) over n bars of this timeframe."""
    c = d["c"]
    rets: S = [None] + [math.log(c[i] / c[i - 1]) if c[i] > 0 and c[i - 1] > 0 else 0.0 for i in range(1, len(c))]
    per_year = 252 * (1 if d["tf_s"] >= 86400 else max(1, 22500 // d["tf_s"]))
    return _zip(lambda sd: sd * math.sqrt(per_year) * 100, stdev(rets, n))


def close_time(d: dict, i: int) -> int:
    """When bar i closed: volume/turnover candles carry it; time bars are t + tf."""
    return d["tc"][i] if "tc" in d else d["t"][i] + d["tf_s"]


# ---- order flow (see app/algo/orderflow.py for how candles get their flow) ----

def _flow(d: dict) -> tuple[S, S]:
    """(buy volume, sell volume) per bar. Candles built from minutes carry it;
    otherwise (e.g. a daily higher-timeframe candle) estimate it at bar level
    with the same Bulk Volume Classification."""
    if d.get("flow"):
        return d["buy_v"], d["sell_v"]
    o, h, l, c, v = d["o"], d["h"], d["l"], d["c"], d["v"]
    dps = [c[0] - o[0] if c else 0.0] + [c[i] - c[i - 1] for i in range(1, len(c))]
    sd = stdev(dps, 20)
    buy: S = []
    for i in range(len(c)):
        if sd[i]:
            sh = 0.5 * (1 + math.erf(dps[i] / sd[i] / math.sqrt(2)))
        else:
            sh = (c[i] - l[i]) / (h[i] - l[i]) if h[i] > l[i] else 0.5
        buy.append(v[i] * sh)
    return buy, [x - b for x, b in zip(v, buy)]


def order_delta(d: dict) -> dict[str, S]:
    buy, sell = _flow(d)
    delta = [b - s_ for b, s_ in zip(buy, sell)]
    return {"delta": delta, "delta_pct": [x / (b + s_) * 100 if b + s_ else 0.0 for x, b, s_ in zip(delta, buy, sell)],
            "buy_volume": list(buy), "sell_volume": list(sell)}


def cvd(d: dict) -> dict[str, S]:
    """Cumulative delta: per session (resets at the open) and running total."""
    delta = order_delta(d)["delta"]
    keys = _day_keys(d)
    ses: S = []
    tot: S = []
    a = b = 0.0
    for i, x in enumerate(delta):
        if i and keys[i] != keys[i - 1]:
            a = 0.0
        a += x
        b += x
        ses.append(a)
        tot.append(b)
    return {"session": ses, "total": tot}


def cvd_divergence(d: dict, n: int) -> dict[str, S]:
    """1.0 when price makes a new n-bar low but cumulative delta does not
    (bull: sellers exhausted), or a new high that delta doesn't confirm (bear)."""
    tot, h, l = cvd(d)["total"], d["h"], d["l"]
    bull: S = [0.0] * len(h)
    bear: S = [0.0] * len(h)
    for i in range(n, len(h)):
        if l[i] < min(l[i - n:i]) and tot[i] > min(tot[i - n:i]):
            bull[i] = 1.0
        if h[i] > max(h[i - n:i]) and tot[i] < max(tot[i - n:i]):
            bear[i] = 1.0
    return {"bull": bull, "bear": bear}


def _value_area(bins: dict[int, float], poc: int, lo_b: int, hi_b: int, target: float) -> tuple[int, int]:
    lo = hi = poc
    acc = bins.get(poc, 0.0)
    while acc < target and (lo > lo_b or hi < hi_b):
        up = bins.get(hi + 1, 0.0) if hi < hi_b else -1.0
        dn = bins.get(lo - 1, 0.0) if lo > lo_b else -1.0
        if up >= dn:
            hi += 1
            acc += max(up, 0.0)
        else:
            lo -= 1
            acc += max(dn, 0.0)
    return lo, hi


def volume_profile(d: dict, value_area: int = 70) -> dict[str, S]:
    """Session volume profile, developing bar by bar: POC (price with the most
    volume), value-area high/low (where `value_area`% of the volume traded),
    plus the previous session's final POC / VAH / VAL. Each bar's volume is
    spread evenly over its high-low range in bins of ~5 bps of price.

    ponytail: the value area is recomputed about every 5 minutes of bars, not
    every 1-minute bar — O(bins) each; tighten if a strategy needs it per bar.
    """
    h, l, c, v = d["h"], d["l"], d["c"], d["v"]
    n = len(c)
    names = ("poc", "vah", "val", "prev_poc", "prev_vah", "prev_val")
    out: dict[str, S] = {k: [None] * n for k in names}
    if not n:
        return out
    mid = sorted(c)[n // 2]
    step = max(0.05, round(mid * 0.0005, 2))
    keys = _day_keys(d)
    every = max(1, 300 // max(60, int(d.get("tf_s") or 300)))
    bins: dict[int, float] = {}
    poc_b = lo_b = hi_b = 0
    total = 0.0
    va = (None, None)
    prev = last = (None, None, None)
    for i in range(n):
        if i == 0 or keys[i] != keys[i - 1]:
            prev = last
            bins, total, va = {}, 0.0, (None, None)
            poc_b = lo_b = hi_b = int(c[i] // step)
        lo, hi = int(l[i] // step), int(h[i] // step)
        stride = max(1, (hi - lo) // 200)              # a huge-range bar: sample its bins
        span = range(lo, hi + 1, stride)
        share = v[i] / len(span)
        for b in span:
            bins[b] = bins.get(b, 0.0) + share
            if bins[b] > bins.get(poc_b, -1.0):
                poc_b = b
        lo_b, hi_b = min(lo_b, lo), max(hi_b, hi)
        total += v[i]
        if va[0] is None or i % every == 0 or i + 1 == n or keys[i + 1] != keys[i]:
            a, b = _value_area(bins, poc_b, lo_b, hi_b, total * value_area / 100)
            va = (a * step, (b + 1) * step)
        poc = (poc_b + 0.5) * step
        out["poc"][i], out["val"][i], out["vah"][i] = poc, va[0], va[1]
        out["prev_poc"][i], out["prev_vah"][i], out["prev_val"][i] = prev[0], prev[1], prev[2]
        last = (poc, va[1], va[0])
    return out


def rvol(d: dict, days: int) -> S:
    """Relative volume so far today: cumulative session volume up to this bar's
    close / the average cumulative volume at the same time of day over the
    previous `days` sessions. 2.0 = twice the usual activity by now."""
    import bisect

    keys = _day_keys(d)
    out: S = [None] * len(d["t"])
    hist: deque[tuple[list[int], list[float]]] = deque(maxlen=days)
    sods: list[int] = []
    cums: list[float] = []
    cum = 0.0
    for i, v in enumerate(d["v"]):
        if i and keys[i] != keys[i - 1]:
            hist.append((sods, cums))
            sods, cums, cum = [], [], 0.0
        cum += v
        sod = (close_time(d, i) + IST_OFFSET) % 86400
        sods.append(sod)
        cums.append(cum)
        refs = []
        for hs, hc in hist:
            j = bisect.bisect_right(hs, sod) - 1
            refs.append(hc[j] if j >= 0 else 0.0)
        avg = sum(refs) / len(refs) if refs else 0.0
        out[i] = cum / avg if avg > 0 else None
    return out


def _day_keys(d: dict) -> list[int]:
    return [(t + IST_OFFSET) // 86400 for t in d["t"]]


def daily_levels(d: dict) -> dict[str, S]:
    """Previous-day OHLC, classic pivots + CPR, and today's running OHLC."""
    keys, o, h, l, c = _day_keys(d), d["o"], d["h"], d["l"], d["c"]
    names = ("prev_open", "prev_high", "prev_low", "prev_close", "pivot", "r1", "r2", "r3",
             "s1", "s2", "s3", "tc", "bc", "day_open", "day_high", "day_low", "gap_pct")
    out: dict[str, S] = {k: [None] * len(c) for k in names}
    prev = None          # (o, h, l, c) of the previous session
    cur = None
    for i in range(len(c)):
        if cur is None or keys[i] != cur[4]:
            if cur is not None:
                prev = cur[:4]
            cur = [o[i], h[i], l[i], c[i], keys[i]]
        else:
            cur[1], cur[2], cur[3] = max(cur[1], h[i]), min(cur[2], l[i]), c[i]
        out["day_open"][i], out["day_high"][i], out["day_low"][i] = cur[0], cur[1], cur[2]
        if prev is None:
            continue
        po, ph, pl, pc = prev
        p = (ph + pl + pc) / 3
        bc = (ph + pl) / 2
        vals = (po, ph, pl, pc, p, 2 * p - pl, p + (ph - pl), ph + 2 * (p - pl),
                2 * p - ph, p - (ph - pl), pl - 2 * (ph - p), 2 * p - bc, bc)
        for k, val in zip(names, vals):
            out[k][i] = val
        out["gap_pct"][i] = (cur[0] - pc) / pc * 100 if pc else None
    return out


def vwap(d: dict) -> S:
    keys, tp, v = _day_keys(d), source(d, "hlc3"), d["v"]
    out: S = [None] * len(tp)
    pv = vol = 0.0
    for i in range(len(tp)):
        if i == 0 or keys[i] != keys[i - 1]:
            pv = vol = 0.0
        pv += tp[i] * v[i]
        vol += v[i]
        out[i] = pv / vol if vol else None    # indices carry no volume
    return out


def opening_range(d: dict, minutes: int) -> dict[str, S]:
    """High/low of the first `minutes` of the session; None until it closes."""
    t, h, l = d["t"], d["h"], d["l"]
    keys = _day_keys(d)
    hi: S = [None] * len(t)
    lo: S = [None] * len(t)
    rh = rl = None
    for i in range(len(t)):
        if i == 0 or keys[i] != keys[i - 1]:
            rh = rl = None
        sod = (t[i] + IST_OFFSET) % 86400
        if sod < SESSION_OPEN_S + minutes * 60:
            rh = h[i] if rh is None else max(rh, h[i])
            rl = l[i] if rl is None else min(rl, l[i])
        if (close_time(d, i) + IST_OFFSET) % 86400 >= SESSION_OPEN_S + minutes * 60 and rh is not None:
            hi[i], lo[i] = rh, rl
    return {"high": hi, "low": lo}


def heikin_ashi(d: dict) -> dict[str, S]:
    o, h, l, c = d["o"], d["h"], d["l"], d["c"]
    hc = [(a + b + k + m) / 4 for a, b, k, m in zip(o, h, l, c)]
    ho: S = [None] * len(c)
    for i in range(len(c)):
        ho[i] = (o[0] + c[0]) / 2 if i == 0 else (ho[i - 1] + hc[i - 1]) / 2
    return {"open": ho, "close": hc,
            "high": [max(a, b, k) for a, b, k in zip(h, ho, hc)],
            "low": [min(a, b, k) for a, b, k in zip(l, ho, hc)]}


def candles(d: dict) -> dict[str, S]:
    """Candlestick patterns as 1.0 / 0.0 — compare with `== 1`."""
    o, h, l, c = d["o"], d["h"], d["l"], d["c"]
    out: dict[str, S] = {k: [0.0] * len(c) for k in (
        "green", "red", "doji", "hammer", "shooting_star", "bullish_engulfing",
        "bearish_engulfing", "inside_bar", "outside_bar", "nr7")}
    for i in range(len(c)):
        body, rng = abs(c[i] - o[i]), h[i] - l[i]
        lower, upper = min(o[i], c[i]) - l[i], h[i] - max(o[i], c[i])
        out["green"][i] = float(c[i] > o[i])
        out["red"][i] = float(c[i] < o[i])
        out["doji"][i] = float(rng > 0 and body <= 0.1 * rng)
        out["hammer"][i] = float(rng > 0 and body > 0 and lower >= 2 * body and upper <= body)
        out["shooting_star"][i] = float(rng > 0 and body > 0 and upper >= 2 * body and lower <= body)
        if i:
            pg, pr = c[i - 1] > o[i - 1], c[i - 1] < o[i - 1]
            out["bullish_engulfing"][i] = float(pr and c[i] > o[i] and o[i] <= c[i - 1] and c[i] >= o[i - 1])
            out["bearish_engulfing"][i] = float(pg and c[i] < o[i] and o[i] >= c[i - 1] and c[i] <= o[i - 1])
            out["inside_bar"][i] = float(h[i] < h[i - 1] and l[i] > l[i - 1])
            out["outside_bar"][i] = float(h[i] > h[i - 1] and l[i] < l[i - 1])
        if i >= 6:   # narrowest range of the last 7 bars — volatility coiled before a move
            out["nr7"][i] = float(h[i] - l[i] <= min(h[k] - l[k] for k in range(i - 6, i)))
    return out


def clock(d: dict) -> dict[str, S]:
    """Bar START time as HHMM in IST (e.g. 930), and weekday 1=Mon..7=Sun."""
    out_t: S = []
    out_d: S = []
    for t in d["t"]:
        sod = (t + IST_OFFSET) % 86400
        out_t.append(float((sod // 3600) * 100 + (sod % 3600) // 60))
        out_d.append(float(((t + IST_OFFSET) // 86400 + 3) % 7 + 1))  # 1970-01-01 was a Thursday
    return {"hhmm": out_t, "weekday": out_d}


# ---- registry --------------------------------------------------------------
# name -> (fn(d, **params) -> list | {field: list}, default params, outputs, group)
# The UI builds its pickers from this, so adding an indicator is one line here.

def _src(fn):
    return lambda d, source="close", **p: fn(globals()["source"](d, source), **p)


REGISTRY: dict[str, tuple[Callable[..., Any], dict[str, Any], list[str], str]] = {
    "PRICE":      (lambda d, source="close": globals()["source"](d, source), {"source": "close"}, ["value"], "Price"),
    "SMA":        (_src(lambda x, period: sma(x, period)), {"period": 20, "source": "close"}, ["value"], "Moving averages"),
    "EMA":        (_src(lambda x, period: ema(x, period)), {"period": 20, "source": "close"}, ["value"], "Moving averages"),
    "WMA":        (_src(lambda x, period: wma(x, period)), {"period": 20, "source": "close"}, ["value"], "Moving averages"),
    "HMA":        (_src(lambda x, period: hma(x, period)), {"period": 20, "source": "close"}, ["value"], "Moving averages"),
    "DEMA":       (_src(lambda x, period: dema(x, period)), {"period": 20, "source": "close"}, ["value"], "Moving averages"),
    "TEMA":       (_src(lambda x, period: tema(x, period)), {"period": 20, "source": "close"}, ["value"], "Moving averages"),
    "RMA":        (_src(lambda x, period: rma(x, period)), {"period": 14, "source": "close"}, ["value"], "Moving averages"),
    "VWMA":       (lambda d, period=20: _zip(lambda a, b: a / b if b else None,
                                             sma([c * v for c, v in zip(d["c"], d["v"])], period),
                                             sma([float(v) for v in d["v"]], period)),
                   {"period": 20}, ["value"], "Moving averages"),
    "VWAP":       (lambda d: vwap(d), {}, ["value"], "Moving averages"),
    "SUPERTREND": (lambda d, period=10, multiplier=3.0: supertrend(d, period, multiplier),
                   {"period": 10, "multiplier": 3.0}, ["value", "direction"], "Trend"),
    "PSAR":       (lambda d, start=0.02, increment=0.02, maximum=0.2: psar(d, start, increment, maximum),
                   {"start": 0.02, "increment": 0.02, "maximum": 0.2}, ["value"], "Trend"),
    "ADX":        (lambda d, period=14: adx(d, period), {"period": 14}, ["adx", "plus_di", "minus_di"], "Trend"),
    "AROON":      (lambda d, period=14: aroon(d, period), {"period": 14}, ["up", "down", "oscillator"], "Trend"),
    "ICHIMOKU":   (lambda d, conversion=9, base=26, span_b=52: ichimoku(d, conversion, base, span_b),
                   {"conversion": 9, "base": 26, "span_b": 52}, ["tenkan", "kijun", "span_a", "span_b"], "Trend"),
    "MACD":       (_src(lambda x, fast, slow, signal: macd(x, fast, slow, signal)),
                   {"fast": 12, "slow": 26, "signal": 9, "source": "close"}, ["macd", "signal", "hist"], "Momentum"),
    "RSI":        (_src(lambda x, period: rsi(x, period)), {"period": 14, "source": "close"}, ["value"], "Momentum"),
    "STOCH":      (lambda d, k=14, d_period=3, smooth=3: (lambda kk: {"k": kk, "d": sma(kk, d_period)})(
                       sma(stoch_raw(d["c"], d["h"], d["l"], k), smooth)),
                   {"k": 14, "d_period": 3, "smooth": 3}, ["k", "d"], "Momentum"),
    "STOCHRSI":   (lambda d, rsi_period=14, stoch_period=14, k=3, d_period=3: (
                       lambda r: (lambda kk: {"k": kk, "d": sma(kk, d_period)})(
                           sma(stoch_raw(r, r, r, stoch_period), k)))(rsi(d["c"], rsi_period)),
                   {"rsi_period": 14, "stoch_period": 14, "k": 3, "d_period": 3}, ["k", "d"], "Momentum"),
    "CCI":        (lambda d, period=20: cci(d, period), {"period": 20}, ["value"], "Momentum"),
    "WILLR":      (lambda d, period=14: _zip(lambda c, hh, ll: -100 * (hh - c) / (hh - ll) if hh != ll else -50.0,
                                             d["c"], highest(d["h"], period), lowest(d["l"], period)),
                   {"period": 14}, ["value"], "Momentum"),
    "MFI":        (lambda d, period=14: mfi(d, period), {"period": 14}, ["value"], "Momentum"),
    "ROC":        (_src(lambda x, period: _zip(lambda a, b: 100 * (a - b) / b if b else None, x, shift(x, period))),
                   {"period": 10, "source": "close"}, ["value"], "Momentum"),
    "MOMENTUM":   (_src(lambda x, period: _zip(lambda a, b: a - b, x, shift(x, period))),
                   {"period": 10, "source": "close"}, ["value"], "Momentum"),
    "TRIX":       (lambda d, period=15: (lambda e3: _zip(lambda a, b: 100 * (a - b) / b if b else None, e3, shift(e3, 1)))(
                       ema(ema(ema(d["c"], period), period), period)), {"period": 15}, ["value"], "Momentum"),
    "BBANDS":     (_src(lambda x, period, multiplier: bollinger(x, period, multiplier)),
                   {"period": 20, "multiplier": 2.0, "source": "close"},
                   ["upper", "middle", "lower", "width", "percent_b"], "Volatility"),
    "KELTNER":    (lambda d, period=20, multiplier=2.0, atr_period=10: (lambda m, a: {
                       "upper": _zip(lambda x, y: x + multiplier * y, m, a), "middle": m,
                       "lower": _zip(lambda x, y: x - multiplier * y, m, a)})(ema(d["c"], period), atr(d, atr_period)),
                   {"period": 20, "multiplier": 2.0, "atr_period": 10}, ["upper", "middle", "lower"], "Volatility"),
    "DONCHIAN":   (lambda d, period=20: (lambda hi, lo: {"upper": hi, "lower": lo,
                                                         "middle": _zip(lambda a, b: (a + b) / 2, hi, lo)})(
                       highest(d["h"], period), lowest(d["l"], period)),
                   {"period": 20}, ["upper", "lower", "middle"], "Volatility"),
    "ATR":        (lambda d, period=14: atr(d, period), {"period": 14}, ["value"], "Volatility"),
    "STDDEV":     (_src(lambda x, period: stdev(x, period)), {"period": 20, "source": "close"}, ["value"], "Volatility"),
    "ZSCORE":     (_src(lambda x, period: zscore(x, period)), {"period": 20, "source": "close"}, ["value"], "Volatility"),
    "HV":         (lambda d, period=20: hist_vol(d, period), {"period": 20}, ["value"], "Volatility"),
    "LINREG":     (_src(lambda x, period: linreg(x, period)), {"period": 20, "source": "close"},
                   ["value", "slope_pct"], "Trend"),
    "HIGHEST":    (_src(lambda x, period: highest(x, period)), {"period": 20, "source": "high"}, ["value"], "Volatility"),
    "LOWEST":     (_src(lambda x, period: lowest(x, period)), {"period": 20, "source": "low"}, ["value"], "Volatility"),
    "OBV":        (lambda d: obv(d), {}, ["value"], "Volume"),
    "CMF":        (lambda d, period=20: cmf(d, period), {"period": 20}, ["value"], "Volume"),
    "VOLUME_SMA": (lambda d, period=20: sma([float(v) for v in d["v"]], period), {"period": 20}, ["value"], "Volume"),
    "DAILY":      (lambda d: daily_levels(d), {}, ["prev_high", "prev_low", "prev_close", "prev_open",
                                                   "pivot", "r1", "r2", "r3", "s1", "s2", "s3", "tc", "bc",
                                                   "day_open", "day_high", "day_low", "gap_pct"], "Levels"),
    "ORB":        (lambda d, minutes=15: opening_range(d, minutes), {"minutes": 15}, ["high", "low"], "Levels"),
    "HEIKIN_ASHI": (lambda d: heikin_ashi(d), {}, ["close", "open", "high", "low"], "Candles"),
    "CANDLE":     (lambda d: candles(d), {}, ["green", "red", "doji", "hammer", "shooting_star",
                                              "bullish_engulfing", "bearish_engulfing", "inside_bar",
                                              "outside_bar", "nr7"], "Candles"),
    "DELTA":      (lambda d: order_delta(d), {}, ["delta", "delta_pct", "buy_volume", "sell_volume"], "Order flow"),
    "CVD":        (lambda d: cvd(d), {}, ["session", "total"], "Order flow"),
    "CVD_DIVERGENCE": (lambda d, period=20: cvd_divergence(d, period), {"period": 20}, ["bull", "bear"], "Order flow"),
    "VPROFILE":   (lambda d, value_area=70: volume_profile(d, value_area), {"value_area": 70},
                   ["poc", "vah", "val", "prev_poc", "prev_vah", "prev_val"], "Order flow"),
    "RVOL":       (lambda d, days=10: rvol(d, days), {"days": 10}, ["value"], "Volume"),
    "BAR_FLOW":   (lambda d: {"poc": d["poc"] if d.get("flow") else source(d, "hlc3"),
                              "vwap": d["bvwap"] if d.get("flow") else source(d, "hlc3")},
                   {}, ["poc", "vwap"], "Order flow"),
    "TIME":       (lambda d: clock(d), {}, ["hhmm", "weekday"], "Time"),
    # Calendar days to the instrument's expiry (options: the chosen weekly /
    # monthly; otherwise the monthly F&O expiry) — "trade only on expiry day"
    # is DTE < 1. The engine attaches `expiry_ts` before signals run.
    "DTE":        (lambda d: [(e - t) / 86400 for e, t in zip(d["expiry_ts"], d["t"])]
                   if d.get("expiry_ts") else [None] * len(d["t"]), {}, ["value"], "Time"),
}

SOURCES = ("close", "open", "high", "low", "hl2", "hlc3", "ohlc4", "volume", "range")


def compute(d: dict, name: str, params: dict[str, Any]) -> dict[str, S]:
    """One indicator over one symbol's bars -> {field: series}."""
    if name not in REGISTRY:
        raise ValueError(f"unknown indicator {name!r}")
    fn, defaults, outputs, _ = REGISTRY[name]
    p = {**defaults, **{k: v for k, v in (params or {}).items() if k in defaults}}
    for k, v in p.items():
        if k == "source":
            if v not in SOURCES:
                raise ValueError(f"{name}: unknown source {v!r}")
        elif isinstance(defaults[k], int):
            p[k] = int(v)
            if p[k] < 1:
                raise ValueError(f"{name}: {k} must be >= 1")
        else:
            p[k] = float(v)
    res = fn(d, **p)
    return res if isinstance(res, dict) else {outputs[0]: res}


def catalog() -> list[dict[str, Any]]:
    return [{"name": n, "params": dflt, "outputs": outs, "group": g}
            for n, (_, dflt, outs, g) in REGISTRY.items()]
