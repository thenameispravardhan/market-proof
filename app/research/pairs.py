"""Pair finder: which two symbols' prices move together and snap back.

For each candidate pair (each symbol against an anchor such as the index, or
every pair of peers) on the candles both have:

  * fit — least squares of log A on log B over the first `fit_frac` of the
    history: the hedge ratio beta and the residual spread;
  * Engle-Granger — a Dickey-Fuller t on the fitted spread (more negative =
    snaps back harder); 5% critical value -3.34 for two series;
  * half-life — bars for a spread gap to halve, from the AR(1) of the spread;
  * held-out confirmation — the SAME beta applied to the last part of the
    history, never used to fit: its Dickey-Fuller t must still clear -2.86
    (the plain 5% value: nothing is estimated on that part).

Scanning many pairs finds some by luck: at 5%, 1 in 20 unrelated pairs looks
cointegrated in the fit. The held-out check is the guard, and `tested`
travels with the chosen pair as tries for the Deflated Sharpe.
"""
from __future__ import annotations

import itertools
import math
from typing import Any, Optional

EG_5PCT = -3.34          # Engle-Granger, 2 variables, with constant (MacKinnon)
DF_5PCT = -2.86          # Dickey-Fuller with constant: the spread on held-out bars, nothing fitted there
MAX_SYMBOLS = 30


def _ols(x: list[float], y: list[float]) -> tuple[float, float]:
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    vx = sum((a - mx) ** 2 for a in x)
    if vx <= 0:
        raise ValueError("flat series")
    beta = sum((a - mx) * (b - my) for a, b in zip(x, y)) / vx
    return my - beta * mx, beta


def dickey_fuller(e: list[float]) -> tuple[Optional[float], Optional[float]]:
    """t-stat of phi in  d(e_t) = c + phi * e_(t-1)  and phi itself."""
    if len(e) < 10:
        return None, None
    lag, de = e[:-1], [b - a for a, b in zip(e[:-1], e[1:])]
    try:
        c, phi = _ols(lag, de)
    except ValueError:
        return None, None
    resid = [d - c - phi * x for x, d in zip(lag, de)]
    n = len(de)
    s2 = sum(r * r for r in resid) / (n - 2)
    m = sum(lag) / n
    vx = sum((x - m) ** 2 for x in lag)
    se = math.sqrt(s2 / vx) if vx > 0 and s2 > 0 else 0.0
    return (phi / se if se else None), phi


def half_life(phi: Optional[float]) -> Optional[float]:
    if phi is None or not -1 < phi < 0:
        return None
    return -math.log(2) / math.log(1 + phi)


def pair_stats(ta: list[int], ca: list[float], tb: list[int], cb: list[float], *, fit_frac: float = 2 / 3
               ) -> Optional[dict[str, Any]]:
    """A against B on their common bars, or None when too few line up."""
    b_at = dict(zip(tb, cb))
    rows = [(t, a, b_at[t]) for t, a in zip(ta, ca) if t in b_at and a and a > 0 and b_at[t] and b_at[t] > 0]
    if len(rows) < 60:
        return None
    la, lb = [math.log(r[1]) for r in rows], [math.log(r[2]) for r in rows]
    k = int(len(rows) * fit_frac)
    try:
        alpha, beta = _ols(lb[:k], la[:k])
    except ValueError:
        return None
    spread = [y - alpha - beta * x for x, y in zip(lb, la)]
    fit, held = spread[:k], spread[k:]
    t_fit, phi = dickey_fuller(fit)
    t_held, _ = dickey_fuller(held)
    sd = math.sqrt(sum(v * v for v in fit) / (len(fit) - 2))
    ra = [b - a for a, b in zip(la[:-1], la[1:])]
    rb = [b - a for a, b in zip(lb[:-1], lb[1:])]
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    va, vb = sum((x - ma) ** 2 for x in ra), sum((x - mb) ** 2 for x in rb)
    corr = sum((x - ma) * (y - mb) for x, y in zip(ra, rb)) / math.sqrt(va * vb) if va > 0 and vb > 0 else None
    hl = half_life(phi)
    return {"bars": len(rows), "fit_bars": k, "from": rows[0][0], "split": rows[k][0], "to": rows[-1][0],
            "beta": round(beta, 4), "corr": None if corr is None else round(corr, 3),
            "adf_fit": None if t_fit is None else round(t_fit, 2),
            "adf_held_out": None if t_held is None else round(t_held, 2),
            "half_life": None if hl is None else round(hl, 1),
            "z_now": round(spread[-1] / sd, 2) if sd > 0 else None,
            "spread_sd_pct": round(sd * 100, 3),
            "cointegrated": t_fit is not None and t_fit < EG_5PCT,
            "holds_out": t_held is not None and t_held < DF_5PCT,
            # a rolling window long enough to fit several half-lives, within the engine's lookback
            "window": max(20, min(500, int(round(4 * hl)) if hl else 100))}


def scan(closes: dict[str, dict], anchor: Optional[str] = None, *, fit_frac: float = 2 / 3,
         top: int = 50) -> dict[str, Any]:
    """`closes`: {symbol: {"t": [...], "c": [...]}}. With `anchor`, every
    other symbol against it (stock vs index); without, every pair of peers.
    Pairs that hold out first, then by how hard the fitted spread snaps back."""
    syms = [s for s in closes if s != anchor]
    pairs = [(s, anchor) for s in syms] if anchor else list(itertools.combinations(syms, 2))
    rows, skipped = [], 0
    for a, b in pairs:
        st = pair_stats(closes[a]["t"], closes[a]["c"], closes[b]["t"], closes[b]["c"], fit_frac=fit_frac)
        if st is None:
            skipped += 1
            continue
        rows.append({"a": a, "b": b, **st})
    rows.sort(key=lambda r: (not (r["cointegrated"] and r["holds_out"]), not r["cointegrated"],
                             r["adf_fit"] if r["adf_fit"] is not None else 0.0))
    found = sum(1 for r in rows if r["cointegrated"])
    return {"tested": len(pairs), "skipped": skipped, "cointegrated": found,
            "confirmed": sum(1 for r in rows if r["cointegrated"] and r["holds_out"]),
            # pairs that would pass the fit test on luck alone if none were related
            "expected_by_luck": round(0.05 * (len(pairs) - skipped), 1),
            "pairs": rows[:top]}


def spec_for(a: str, b: str, window: int, timeframe: int, entry_z: float = 2.0, exit_z: float = 0.5
             ) -> dict[str, Any]:
    """A one-leg pair strategy: trade A only, on A's spread against B. Long A
    when it falls `entry_z` deviations below its fitted value, short when it
    rises as far above; out when the gap closes to `exit_z`, or after
    `window` bars (about four half-lives) when it hasn't — the relationship
    may have broken. Carries overnight: a spread can take days to close."""
    z = {"math": "pair_z", "a": {"ind": "PRICE"}, "b": {"ind": "PRICE", "symbol": b}, "n": int(window)}

    def cond(op: str, v: float) -> dict[str, Any]:
        return {"logic": "AND", "conditions": [{"left": dict(z), "op": op, "right": {"value": v}}]}

    return {"symbols": [a], "timeframe": int(timeframe), "direction": "both",
            "entry_long": cond("crosses_below", -entry_z), "exit_long": cond(">", -exit_z),
            "entry_short": cond("crosses_above", entry_z), "exit_short": cond("<", exit_z),
            "stop_loss": {"type": "pct", "value": 3.0}, "target": None, "max_bars": int(window),
            "session": {"start": "09:15", "end": "15:15", "square_off": None}}
