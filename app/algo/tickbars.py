"""Bars and footprints built from the recorder's raw frames, tick by tick.

The minute files (ticks.py) answer every chart down to one minute. Below that,
and for bars that close on price or quantity rather than the clock, the order
of trades inside the minute matters — the raw frames keep it (every frame of
the focus symbols, `raw_days`). From them:

  * trades():     one classified trade per frame that traded (the recorder's own
                  side; side 0 split half / half), volume with no aggressor kept
                  apart as unk (outside 09:15-15:30, or the first frame after a
                  silence of GAP_S inside the session — a feed gap; the quiet
                  between the pre-open and 09:15 is not one);
  * time_bars():  bars of any span in seconds, anchored at 09:15, with the flow
                  bar fields /api/algo/ticks/flow serves and a footprint;
  * tick_bars():  range bars (a bar closes when its high - low would pass
                  `size` in price), volume bars (`size` quantity) and tick bars
                  (`size` trades), with their footprints. Bar times are the open
                  trade's exchange time, made strictly increasing (a chart needs
                  one bar per second at most: a burst of bars borrows the next
                  seconds).
"""
from __future__ import annotations

from typing import Any, Iterable, NamedTuple, Optional

IST = 19800
OPEN_S, CLOSE_S = 9 * 3600 + 15 * 60, 15 * 3600 + 30 * 60
GAP_S = 120                      # in the session, a frame this long after the previous one: the feed was down (unk)

# RAW_COLS (app/algo/ticks.py): recv_ts, exch_ts, ltp, vol, dv, side, bid, ask, bid_size, ask_size,
#                               tot_buy, tot_sell, ltq, oi


class Trade(NamedTuple):
    ts: int
    price: float
    buy: float
    sell: float
    unk: float
    oi: Optional[float]
    tot_buy: float
    tot_sell: float


def trades(frames: Iterable[tuple], exchange_hours: bool = True) -> list[Trade]:
    out: list[Trade] = []
    prev: Optional[tuple[float, bool]] = None           # (receive time, in the session) of the previous frame
    for f in frames:
        recv, ts, ltp, dv, side = float(f[0] or 0), int(f[1] or 0), float(f[2] or 0), float(f[4] or 0), int(f[5] or 0)
        tod = (ts + IST) % 86400
        in_session = not exchange_hours or OPEN_S <= tod < CLOSE_S
        gap = prev is not None and prev[1] and recv - prev[0] > GAP_S
        prev = (recv, in_session)
        if dv <= 0 or ltp <= 0:
            continue
        unk = gap or not in_session
        if unk:
            buy = sell = 0.0
        else:
            buy = dv if side > 0 else dv / 2 if side == 0 else 0.0
            sell = dv - buy
        out.append(Trade(ts, ltp, buy, sell, dv if unk else 0.0, float(f[13]) if f[13] is not None else None,
                         float(f[10] or 0), float(f[11] or 0)))
    return out


def _bucket(ts: int, span: int, exchange_hours: bool) -> int:
    day0 = ts - (ts + IST) % 86400
    o_s = day0 + OPEN_S
    if exchange_hours:
        ts = min(max(ts, o_s), day0 + CLOSE_S - 1)      # the auction / closing session join the first / last bar
    return o_s + ((ts - o_s) // span) * span


def time_bars(tr: list[Trade], span: int, exchange_hours: bool = True) -> tuple[list[list], list[list]]:
    """(bars, footprint): bars [t, buy, sell, delta, trades, unk, oi, close, delta_low, delta_high,
    book_bid, book_ask] — /ticks/flow's shape — and footprint rows [t, price, buy, sell]."""
    bars: dict[int, list] = {}
    fp: dict[tuple[int, float], list[float]] = {}
    for t in tr:
        b = _bucket(t.ts, span, exchange_hours)
        x = bars.setdefault(b, [0.0, 0.0, 0.0, 0, None, None, 0.0, 0.0, 0.0, 0.0, 0.0, 0])
        x[0] += t.buy
        x[1] += t.sell
        x[2] += t.unk
        x[3] += 1 if t.buy + t.sell > 0 else 0
        x[4] = t.oi if t.oi is not None else x[4]
        x[5] = t.price
        x[6] += t.buy - t.sell
        x[7], x[8] = min(x[7], x[6]), max(x[8], x[6])
        if t.tot_buy > 0 or t.tot_sell > 0:
            x[9] += t.tot_buy
            x[10] += t.tot_sell
            x[11] += 1
        if t.buy + t.sell > 0:
            f = fp.setdefault((b, t.price), [0.0, 0.0])
            f[0] += t.buy
            f[1] += t.sell
    out = [[b, round(x[0], 2), round(x[1], 2), round(x[0] - x[1], 2), x[3], round(x[2], 2), x[4], x[5],
            round(x[7], 2), round(x[8], 2), round(x[9] / x[11], 1) if x[11] else None,
            round(x[10] / x[11], 1) if x[11] else None] for b, x in sorted(bars.items())]
    rows = [[b, p, round(v[0], 2), round(v[1], 2)] for (b, p), v in sorted(fp.items())]
    return out, rows


def tick_bars(tr: list[Trade], kind: str, size: float) -> tuple[list[dict[str, Any]], list[list]]:
    """(bars, footprint): bars {t, open, high, low, close, volume, buy, sell, delta, trades, unk,
    delta_low, delta_high} and footprint rows [t, price, buy, sell]. `kind`: range (size in price),
    volume (size in quantity) or tick (size in trades). Volume without an aggressor (unk) counts
    in volume but never closes a volume / tick bar on its own."""
    if kind not in ("range", "volume", "tick") or not size > 0:
        raise ValueError("kind is range, volume or tick, with a size above 0")
    bars: list[dict[str, Any]] = []
    fp: dict[tuple[int, float], list[float]] = {}
    cur: Optional[dict[str, Any]] = None
    last_t = -1

    def open_bar(t: Trade) -> dict[str, Any]:
        nonlocal last_t
        last_t = max(t.ts, last_t + 1)
        return {"t": last_t, "open": t.price, "high": t.price, "low": t.price, "close": t.price, "volume": 0.0,
                "buy": 0.0, "sell": 0.0, "delta": 0.0, "trades": 0, "unk": 0.0, "delta_low": 0.0, "delta_high": 0.0}

    for t in tr:
        if cur is not None and kind == "range" and max(cur["high"], t.price) - min(cur["low"], t.price) > size + 1e-9:
            bars.append(cur)
            cur = None
        if cur is None:
            cur = open_bar(t)
        cur["high"], cur["low"], cur["close"] = max(cur["high"], t.price), min(cur["low"], t.price), t.price
        q = t.buy + t.sell
        cur["volume"] += q + t.unk
        cur["buy"] += t.buy
        cur["sell"] += t.sell
        cur["unk"] += t.unk
        cur["delta"] += t.buy - t.sell
        cur["delta_low"], cur["delta_high"] = min(cur["delta_low"], cur["delta"]), max(cur["delta_high"], cur["delta"])
        if q > 0:
            cur["trades"] += 1
            f = fp.setdefault((cur["t"], t.price), [0.0, 0.0])
            f[0] += t.buy
            f[1] += t.sell
        if (kind == "volume" and cur["buy"] + cur["sell"] >= size) or (kind == "tick" and cur["trades"] >= size):
            bars.append(cur)
            cur = None
    if cur is not None:
        bars.append(cur)                                # the bar still forming
    for b in bars:
        for k in ("volume", "buy", "sell", "delta", "unk", "delta_low", "delta_high"):
            b[k] = round(b[k], 2)
    rows = [[t, p, round(v[0], 2), round(v[1], 2)] for (t, p), v in sorted(fp.items())]
    return bars, rows
