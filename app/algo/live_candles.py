"""Candles built from the live Fyers data socket, for the Algo Lab runner.

The runner re-downloaded every strategy symbol's history at every candle close:
7 strategies x ~39 symbols x 75 five-minute closes ≈ 2,900 history calls a day,
70% of the automations' 4,000 (2026-10-27). The data socket isn't metered, so
data.recent_bars asks Fyers once (a seed, re-asked hourly) and builds the candles
after it from the ticks of the symbols the runner trades.

Only while the feed demonstrably covered the time: the symbol's ticks arrived
with no gap over GAP_S since the minute before the window, and a tick stamped
after the window's end has arrived (no late tick of it still in flight).
Anything else returns None and recent_bars asks Fyers as before.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Iterable, Optional

IST = 19800
DAY = 86400
OPEN_S = 33300          # 09:15 IST
CLOSE_S = 55800         # 15:30 IST
GAP_S = 90              # a traded symbol / index ticks every second or so: longer = the feed missed time

_lock = threading.Lock()
_want: set[str] = set()
# full symbol -> {"day", "since", "rx", "ts", "m": {minute start: [o, h, l, c, cumulative volume]}}
_sym: dict[str, dict[str, Any]] = {}


def want(symbols: Iterable[str]) -> None:
    """The symbols to build candles for (the runner's intraday strategies)."""
    global _want
    _want = {s.upper() for s in symbols}


def wanted() -> list[str]:
    return sorted(_want)


def on_tick(full: str, msg: dict[str, Any], recv: Optional[float] = None) -> None:
    """FyersStreamManager tick listener (SDK thread — keep it cheap)."""
    full = full.upper()
    if full not in _want:
        return
    ltp = float(msg.get("ltp") or 0)
    if ltp <= 0:
        return
    recv = recv or time.time()
    ts = int(float(msg.get("exch_feed_time") or msg.get("last_traded_time") or recv))
    day = (int(recv) + IST) // DAY
    if (ts + IST) // DAY != day:
        return                                   # the subscribe snapshot of an earlier session
    cum = msg.get("vol_traded_today")
    with _lock:
        s = _sym.get(full)
        if s is None or s["day"] != day:
            s = _sym[full] = {"day": day, "since": recv, "rx": recv, "ts": ts, "m": {}}
        elif recv - s["rx"] > GAP_S:
            s["since"] = recv                    # the feed missed time: trust only what follows
        s["rx"], s["ts"] = recv, max(s["ts"], ts)
        m = ts - ts % 60
        c = s["m"].get(m)
        if c is None:
            s["m"][m] = [ltp, ltp, ltp, ltp, None if cum is None else float(cum)]
        else:
            c[1], c[2], c[3] = max(c[1], ltp), min(c[2], ltp), ltp
            if cum is not None:
                c[4] = float(cum)


def bars(full: str, tf_s: int, lo: int, now: float) -> Optional[list[tuple]]:
    """Completed `tf_s` candles [t, o, h, l, c, v] starting at `lo` (on today's
    09:15 grid) through the last one that has closed — or None when the feed
    didn't cover all of it. A candle with no ticks is left out, as Fyers does."""
    with _lock:
        s = _sym.get(full.upper())
        if s is None:
            return None
        day0 = s["day"] * DAY - IST
        if lo < day0 + OPEN_S or (lo - day0 - OPEN_S) % tf_s or s["since"] > lo - 60 or now - s["rx"] > GAP_S:
            return None
        end = min(s["ts"], int(now), day0 + CLOSE_S)  # candles up to here can't change any more
        mins = s["m"]
        prev_cum = next((mins[m][4] for m in sorted(mins, reverse=True) if m < lo and mins[m][4] is not None), None)
        out = []
        b = lo
        while b + tf_s <= end:
            o = h = l = c = None
            v = 0.0
            for m in range(b, b + tf_s, 60):
                x = mins.get(m)
                if x is None:
                    continue
                if o is None:
                    o, h, l = x[0], x[1], x[2]
                h, l, c = max(h, x[1]), min(l, x[2]), x[3]
                if x[4] is not None:
                    if prev_cum is None:
                        return None              # no volume before this minute to count it from
                    v += x[4] - prev_cum
                    prev_cum = x[4]
            if o is not None:
                out.append((b, o, h, l, c, v))
            b += tf_s
        return out


def drift(fy: str, tf_s: int, fresh: dict[str, Any], now: float) -> Optional[dict[str, Any]]:
    """The tick-built candles vs Fyers' own for the same times (`fresh`: a history
    answer): how far apart they are, logged at each hourly re-ask. None when the
    feed didn't cover any of them."""
    ts = fresh["t"]
    if not ts:
        return None
    k = max(0, len(ts) - 12)
    built = bars(fy, tf_s, ts[k], now)
    if not built:
        return None
    at = {r[0]: r for r in built}
    rows = [(at[t], (t, fresh["o"][i], fresh["h"][i], fresh["l"][i], fresh["c"][i], fresh["v"][i]))
            for i, t in enumerate(ts[k:], k) if t in at]
    if not rows:
        return None
    return {"symbol": fy, "tf_s": tf_s, "compared": len(rows),
            **{f"max_d{f}": round(max(abs(a[j] - b[j]) for a, b in rows), 4)
               for j, f in ((1, "o"), (2, "h"), (3, "l"), (4, "c"), (5, "v"))}}
