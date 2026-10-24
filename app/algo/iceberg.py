"""Likely icebergs from the recorder's raw frames: hidden size that refills at one price.

A displayed order shows its size in the book; an iceberg shows a slice and refills
it as it trades. From every frame of a contract (ticks.RAW_COLS — kept for the
focus symbols, `raw_days`) an episode is read at one price P:

  * a SELL iceberg at P: buyers lift P (a buy-classified print at P) and, after
    the print, the best ask is STILL P — the offer wasn't used up;
  * a BUY iceberg at P: sellers hit P and the best bid is still P after it.

The episode runs while the best ask (bid) stays at P. It counts the quantity
traded there and the largest size the book ever displayed at P; it is a likely
iceberg when the traded quantity reaches `min_ratio` x that largest display
(and the size refilled at least `min_refills` times). An ordinary resting order
can't trade more than it showed — but several orders queueing at one price can,
so this says "likely", not "certainly".
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

# RAW_COLS positions (app/algo/ticks.py): recv_ts, exch_ts, ltp, vol, dv, side, bid, ask, bid_size, ask_size, ...
_EXCH, _LTP, _DV, _SIDE, _BID, _ASK, _BSZ, _ASZ = 1, 2, 4, 5, 6, 7, 8, 9


def detect(frames: Iterable[tuple], min_ratio: float = 2.0, min_refills: int = 1,
           tick: float = 1e-6) -> list[dict[str, Any]]:
    """[{start, end, price, side, traded, shown, refills, ratio}] — `side` is the
    iceberg's: SELL (resting offer absorbing buyers) or BUY (resting bid absorbing
    sellers). `frames` in exchange-time order."""
    out: list[dict[str, Any]] = []
    cur: Optional[dict[str, Any]] = None

    def close() -> None:
        nonlocal cur
        if cur and cur["traded"] > 0 and cur["shown"] > 0 and cur["refills"] >= min_refills \
                and cur["traded"] >= min_ratio * cur["shown"]:
            out.append({**{k: cur[k] for k in ("start", "end", "price", "side", "traded", "shown", "refills")},
                        "ratio": round(cur["traded"] / cur["shown"], 2)})
        cur = None

    for f in frames:
        ts, ltp, dv = int(f[_EXCH] or 0), float(f[_LTP] or 0), float(f[_DV] or 0)
        side = int(f[_SIDE] or 0)
        bid, ask = float(f[_BID] or 0), float(f[_ASK] or 0)
        bsz, asz = float(f[_BSZ] or 0), float(f[_ASZ] or 0)
        if cur is not None:
            level, size = (ask, asz) if cur["side"] == "SELL" else (bid, bsz)
            if abs(level - cur["price"]) > tick:
                close()                                    # the level moved: the episode is over
            else:
                if size > cur["last_size"] + tick:
                    cur["refills"] += 1                    # the displayed size grew back at the same price
                cur["shown"] = max(cur["shown"], size)
                cur["last_size"] = size
                cur["end"] = ts
                hit = side > 0 if cur["side"] == "SELL" else side < 0
                if dv > 0 and hit and abs(ltp - cur["price"]) <= tick:
                    cur["traded"] += dv
                continue
        # a new episode: a print at the touch that left the touch where it was
        if dv > 0 and side > 0 and ask and abs(ltp - ask) <= tick:
            cur = {"side": "SELL", "price": ask, "start": ts, "end": ts, "traded": dv, "shown": asz, "last_size": asz,
                   "refills": 0}
        elif dv > 0 and side < 0 and bid and abs(ltp - bid) <= tick:
            cur = {"side": "BUY", "price": bid, "start": ts, "end": ts, "traded": dv, "shown": bsz, "last_size": bsz,
                   "refills": 0}
    close()
    return out
