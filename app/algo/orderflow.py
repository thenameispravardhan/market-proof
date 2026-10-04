"""Volume / turnover candles and order-flow candles, built from 1-minute data.

Fyers keeps no tick history, so a true footprint (each trade tagged as hitting
the bid or lifting the ask) does not exist to backtest on. Instead each
1-minute candle's volume is split into buys and sells with Bulk Volume
Classification (Easley, López de Prado & O'Hara, 2012):

    buy share = Φ(ΔP / σ)

ΔP is the minute's close-to-close change and σ the rolling stdev of those
changes. Summed over the minutes inside a candle, that gives the candle's
buy / sell volume, delta, its point of control (price of its heaviest minute)
and its own VWAP — classified at 1-minute resolution, which is far more
faithful than classifying a 15-minute candle as one lump. The backtest and the
live runner build their candles with this same module, so a strategy sees the
same order flow in both.

Bar types:
  time      every N minutes from 09:15 (1440 = one candle per session)
  volume    a candle closes once N shares (contracts) have traded
  turnover  a candle closes once ₹N has traded — comparable across prices
A volume/turnover candle never spans two sessions: the session's leftover
closes as a smaller last candle. Granularity is one minute — a candle closes
at the end of the minute in which its threshold was crossed.
"""
from __future__ import annotations

import math
from collections import deque
from statistics import median
from typing import Any, Optional

IST = 19800
SESSION_OPEN_S = 33300           # 09:15
SESSION_CLOSE_S = 55800          # 15:30
BVC_WINDOW = 60                  # minutes of ΔP history behind σ


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def buy_share(m1: dict[str, Any], window: int = BVC_WINDOW) -> list[float]:
    """Fraction of each minute's volume that was buying (0..1).

    ΔP for a session's first minute is open->close (the overnight gap is not
    that minute's order flow). Until σ has a few observations, or on a flat
    stretch where σ is 0, it falls back to the close's location in the range.
    """
    o, h, l, c, t = m1["o"], m1["h"], m1["l"], m1["c"], m1["t"]
    hist: deque[float] = deque(maxlen=window)
    s = s2 = 0.0
    out: list[float] = []
    for i in range(len(c)):
        same_day = i and (t[i] + IST) // 86400 == (t[i - 1] + IST) // 86400
        dp = c[i] - (c[i - 1] if same_day else o[i])
        n = len(hist)
        sd = math.sqrt(max(0.0, s2 / n - (s / n) ** 2)) if n >= 10 else 0.0
        if sd > 0:
            out.append(_phi(dp / sd))
        else:
            rng = h[i] - l[i]
            out.append((c[i] - l[i]) / rng if rng > 0 else 0.5)
        if n == window:
            old = hist[0]
            s, s2 = s - old, s2 - old * old
        hist.append(dp)
        s, s2 = s + dp, s2 + dp * dp
    return out


def bar_size(m1: dict[str, Any], mode: str, per_day: int, until: Optional[int] = None) -> float:
    """Volume (or ₹ turnover) per candle so a typical session yields ~per_day
    candles: median of the last 20 sessions BEFORE `until` (no lookahead).
    Raises ValueError when the symbol trades no volume (an index)."""
    days: dict[int, float] = {}
    for t, v, h, l, c in zip(m1["t"], m1["v"], m1["h"], m1["l"], m1["c"]):
        if until is not None and t >= until:
            break
        k = (t + IST) // 86400
        days[k] = days.get(k, 0.0) + (v * (h + l + c) / 3 if mode == "turnover" else v)
    if len(days) < 3:            # too little history before the start: use what exists
        for t, v, h, l, c in zip(m1["t"], m1["v"], m1["h"], m1["l"], m1["c"]):
            k = (t + IST) // 86400
            days.setdefault(k, 0.0)
            if until is not None and t < until:
                continue
            days[k] += v * (h + l + c) / 3 if mode == "turnover" else v
            if len(days) >= 20:
                break
    vols = [x for x in list(days.values())[-20:] if x > 0]
    if not vols:
        raise ValueError("no traded volume (an index?) — volume/turnover candles and order flow "
                         "need a stock, ETF or future")
    return max(1.0, median(vols) / max(1, per_day))


def build_bars(m1: dict[str, Any], mode: str = "time", tf_min: int = 5,
               size: Optional[float] = None, real: Optional[dict[int, tuple[float, float]]] = None) -> dict[str, Any]:
    """Aggregate 1-minute candles into time / volume / turnover candles with
    order-flow fields. `last_complete` says whether the final candle closed
    (threshold, bucket end or session end seen) or is still forming.

    `real` ({minute: (buy, sell)} from the tick recorder) replaces the BVC
    estimate for every minute that was recorded: the minute's candle volume
    is split by the REAL buy/sell ratio. `real_pct` per candle says how much
    of its volume that covered."""
    share = buy_share(m1)
    real_hit = [False] * len(share)
    if real:
        for i, t in enumerate(m1["t"]):
            r = real.get(t)
            if r and r[0] + r[1] > 0:
                share[i] = r[0] / (r[0] + r[1])
                real_hit[i] = True
    keys = ("t", "tc", "o", "h", "l", "c", "v", "buy_v", "sell_v", "delta", "poc", "bvwap", "real_pct")
    out: dict[str, Any] = {k: [] for k in keys}
    cur: Optional[dict[str, Any]] = None
    tf_s = 86400 if tf_min == 1440 else int(tf_min) * 60

    def flush() -> None:
        v = cur["v"]
        out["t"].append(cur["t"])
        out["tc"].append(cur["tc"])
        out["o"].append(cur["o"])
        out["h"].append(cur["h"])
        out["l"].append(cur["l"])
        out["c"].append(cur["c"])
        out["v"].append(v)
        out["buy_v"].append(round(cur["buy"], 2))
        out["sell_v"].append(round(v - cur["buy"], 2))
        out["delta"].append(round(2 * cur["buy"] - v, 2))
        out["poc"].append(cur["poc"])
        out["bvwap"].append(cur["pv"] / v if v else cur["c"])
        out["real_pct"].append(round(cur["realv"] / v * 100, 1) if v else 0.0)

    complete = True
    for i, t in enumerate(m1["t"]):
        day_start = t - (t + IST) % 86400
        o, h, l, c, v = m1["o"][i], m1["h"][i], m1["l"][i], m1["c"][i], m1["v"][i]
        if mode == "time":
            if tf_min == 1440:
                key, tc = day_start, day_start + SESSION_CLOSE_S
            else:
                o_s = day_start + SESSION_OPEN_S
                key = o_s + ((t - o_s) // tf_s) * tf_s
                tc = min(key + tf_s, day_start + SESSION_CLOSE_S)
            if cur is not None and cur["t"] != key:
                flush()
                cur = None
        else:
            key = t
            if cur is not None and cur["day"] != day_start:
                flush()                                   # a candle never spans two sessions
                cur = None
        tp = (h + l + c) / 3
        if cur is None:
            cur = {"t": key, "day": day_start, "o": o, "h": h, "l": l, "c": c, "v": 0, "buy": 0.0,
                   "pv": 0.0, "turn": 0.0, "maxv": -1, "poc": tp, "realv": 0,
                   "tc": tc if mode == "time" else t + 60}
        cur["h"], cur["l"], cur["c"] = max(cur["h"], h), min(cur["l"], l), c
        cur["v"] += v
        cur["buy"] += v * share[i]
        cur["realv"] += v if real_hit[i] else 0
        cur["pv"] += tp * v
        cur["turn"] += tp * v
        if v > cur["maxv"]:
            cur["maxv"], cur["poc"] = v, tp
        if mode != "time":
            cur["tc"] = t + 60
            if (cur["v"] if mode == "volume" else cur["turn"]) >= (size or float("inf")):
                flush()
                cur = None
    if cur is not None:
        flush()
        # A time bucket is complete only once its close time is reached; a
        # volume/turnover candle only on its threshold (or its session's end).
        complete = mode == "time" and m1["t"] and m1["t"][-1] + 60 >= cur["tc"]
    durations = [b - a for a, b in zip(out["t"], out["tc"])]
    out["tf_s"] = tf_s if mode == "time" else int(median(durations)) if durations else 60
    out["last_complete"] = bool(complete)
    out["flow"] = True                                    # order-flow fields came from minutes
    return out


def complete_only(bars: dict[str, Any], now: float) -> dict[str, Any]:
    """Drop a still-forming last candle (live runner): a strategy must never
    act on a candle that can still change."""
    n = len(bars["t"])
    if not n:
        return bars
    session_over = (now + IST) // 86400 > (bars["t"][-1] + IST) // 86400
    if not bars.get("last_complete", True) and not session_over:
        for k, v in bars.items():
            if isinstance(v, list) and len(v) == n:
                bars[k] = v[:-1]
    return bars


if __name__ == "__main__":     # self-check: python -m app.algo.orderflow
    t0 = 1735703100            # 2025-01-01 09:15 IST
    up = {"t": [t0 + 60 * i for i in range(30)], "o": [100 + i for i in range(30)],
          "h": [101 + i for i in range(30)], "l": [99.5 + i for i in range(30)],
          "c": [100.8 + i for i in range(30)], "v": [100] * 30}
    b = build_bars(up, "time", 5)
    assert len(b["t"]) == 6 and b["v"][0] == 500 and all(x > 0 for x in b["delta"])
    vb = build_bars(up, "volume", size=250)
    assert all(v >= 250 for v in vb["v"][:-1]) and vb["tc"][0] == t0 + 180
    print("orderflow ok")
