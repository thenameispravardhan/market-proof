"""IV surface from recorded option-chain snapshots (B0's chain store).

One snapshot -> one surface:

  * each out-of-the-money option with a live two-sided quote (puts below
    spot, calls above; in-the-money premiums are mostly intrinsic and say
    little about volatility) is inverted to an implied vol with the SAME
    Black-Scholes the backtest prices with (app/algo/fno.bs_price: spot,
    6.5% rate, the 15:30 expiry close) — so the surface plugs straight back
    into the engine and reproduces the quotes it was fitted on;
  * per expiry a smile in log-moneyness k = ln(K / spot): vega-weighted
    least squares of a + b·k + c·k² (c ≥ 0; a line, or flat, with too few
    points), held flat beyond the outermost fitted strike;
  * between expiries total variance σ²T is interpolated linearly in T.

`evaluate()` is the test B4 has to pass before the surface goes anywhere
near live Greeks: price every quoted contract of each snapshot with a surface
fitted `lag` snapshots EARLIER and with flat IV, against the quotes' mid.
"""
from __future__ import annotations

import math
from typing import Any, Callable, Iterable, Optional

from app.algo import fno

IST = 19800
DAY = 86400
MAX_SPREAD = 0.5             # skip quotes wider than 50% of their mid
MAX_AGE_S = 600              # ... or unchanged for more than 10 minutes
MIN_TIME_VALUE = 0.10        # ₹: premiums this close to intrinsic carry no vol information


def expiry_close(exp: float) -> int:
    """The 15:30 IST close of an expiry's day, whatever time of day the master stamps it."""
    d = (int(exp) + IST) // DAY
    return d * DAY - IST + 15 * 3600 + 30 * 60


def _vega(s: float, k: float, t: float, v: float) -> float:
    sq = v * math.sqrt(t)
    d1 = (math.log(s / k) + (fno.RISK_FREE + v * v / 2) * t) / sq
    return s * math.exp(-d1 * d1 / 2) / math.sqrt(2 * math.pi) * math.sqrt(t)


def implied_vol(price: float, s: float, k: float, t: float, right: str) -> Optional[float]:
    """Newton from 25%, bisection when a step leaves 1%-500%; None when the
    price is outside what any vol can produce."""
    lo, hi = 0.01, 5.0
    if not (fno.bs_price(s, k, t, lo, right) <= price <= fno.bs_price(s, k, t, hi, right)):
        return None
    v = 0.25
    for _ in range(40):
        p = fno.bs_price(s, k, t, v, right)
        if abs(p - price) < 1e-4:
            return v
        if p < price:
            lo = v
        else:
            hi = v
        vg = _vega(s, k, t, v)
        nv = v - (p - price) / vg if vg > 1e-8 else (lo + hi) / 2
        v = nv if lo < nv < hi else (lo + hi) / 2
    return v


def _mid(c: dict[str, Any], at: int) -> Optional[float]:
    bid, ask = c.get("bid"), c.get("ask")
    if not bid or not ask or bid <= 0 or ask < bid:
        return None
    m = (bid + ask) / 2
    if (ask - bid) / m > MAX_SPREAD or (c.get("qts") and at - int(c["qts"]) > MAX_AGE_S):
        return None
    return m


def spot_of(contracts: list[dict[str, Any]], at: int) -> Optional[float]:
    """The underlying's price in the snapshot: its own quote, else the
    future's discounted at the backtest's rate."""
    for c in contracts:
        if c.get("kind") in ("IDX", "EQ") and (c.get("ltp") or 0) > 0:
            return float(c["ltp"])
    for c in contracts:
        if c.get("kind") == "FUT" and (c.get("ltp") or 0) > 0 and c.get("expiry"):
            t = fno.years_to(expiry_close(c["expiry"]), at)
            return float(c["ltp"]) * math.exp(-fno.RISK_FREE * t)
    return None


def _fit_smile(pts: list[tuple[float, float, float]]) -> dict[str, float]:
    """(k, iv, weight) -> {a, b, c}: weighted least squares, convex or a line."""
    def solve(cols: int) -> Optional[list[float]]:
        m = [[0.0] * cols for _ in range(cols)]
        y = [0.0] * cols
        for k, v, w in pts:
            xs = [k ** j for j in range(cols)]
            for i in range(cols):
                y[i] += w * xs[i] * v
                for j in range(cols):
                    m[i][j] += w * xs[i] * xs[j]
        for i in range(cols):                         # Gauss-Jordan, tiny systems
            piv = max(range(i, cols), key=lambda r: abs(m[r][i]))
            if abs(m[piv][i]) < 1e-14:
                return None
            m[i], m[piv], y[i], y[piv] = m[piv], m[i], y[piv], y[i]
            for r in range(cols):
                if r != i:
                    f = m[r][i] / m[i][i]
                    m[r] = [a - f * b for a, b in zip(m[r], m[i])]
                    y[r] -= f * y[i]
        return [y[i] / m[i][i] for i in range(cols)]
    if len(pts) >= 5:
        q = solve(3)
        if q and q[2] >= 0:
            return {"a": q[0], "b": q[1], "c": q[2]}
    if len(pts) >= 2:
        q = solve(2)
        if q:
            return {"a": q[0], "b": q[1], "c": 0.0}
    tot = sum(w for _, _, w in pts)
    return {"a": sum(v * w for _, v, w in pts) / tot, "b": 0.0, "c": 0.0}


class Surface:
    """IV at any strike / expiry from one snapshot's smiles."""

    def __init__(self, t: int, spot: float, smiles: list[dict[str, Any]]):
        self.t, self.spot = int(t), float(spot)
        self.smiles = sorted(smiles, key=lambda s: s["exp"])

    def to_dict(self) -> dict[str, Any]:
        return {"t": self.t, "spot": round(self.spot, 2),
                "expiries": [{k: round(v, 6) if isinstance(v, float) else v for k, v in s.items()} for s in self.smiles]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Surface":
        return cls(d["t"], d["spot"], d["expiries"])

    @staticmethod
    def _smile(s: dict[str, Any], k: float) -> float:
        k = min(max(k, s["kmin"]), s["kmax"])
        return max(0.01, s["a"] + s["b"] * k + s["c"] * k * k)

    def atm(self) -> Optional[float]:
        return self._smile(self.smiles[0], 0.0) if self.smiles else None

    def iv(self, strike: float, spot: float, t: float, exp: Optional[float] = None) -> Optional[float]:
        """Sticky moneyness: k = ln(strike / spot) at the spot of the moment.
        `exp` a fitted expiry -> its smile; otherwise σ²T interpolated
        between the expiries around it (flat in vol outside them)."""
        if not self.smiles or strike <= 0 or spot <= 0:
            return None
        k = math.log(strike / spot)
        if exp is not None:
            e = expiry_close(exp)
            hit = next((s for s in self.smiles if s["exp"] == e), None)
            if hit:
                return self._smile(hit, k)
            tq = fno.years_to(e, t)
        else:
            return self._smile(self.smiles[0], k)
        ts = [(fno.years_to(s["exp"], t), s) for s in self.smiles]
        if tq <= ts[0][0]:
            return self._smile(ts[0][1], k)
        if tq >= ts[-1][0]:
            return self._smile(ts[-1][1], k)
        for (t1, s1), (t2, s2) in zip(ts, ts[1:]):
            if t1 <= tq <= t2:
                w1, w2 = self._smile(s1, k) ** 2 * t1, self._smile(s2, k) ** 2 * t2
                return math.sqrt((w1 + (w2 - w1) * (tq - t1) / (t2 - t1)) / tq)
        return self._smile(ts[-1][1], k)


def points(contracts: list[dict[str, Any]], at: int, spot: float) -> list[dict[str, Any]]:
    """Each usable OTM option of a snapshot with its mid, k and implied vol."""
    out = []
    for c in contracts:
        if c.get("kind") not in ("CE", "PE") or not c.get("strike") or not c.get("expiry"):
            continue
        k_ = float(c["strike"])
        if (c["kind"] == "CE") != (k_ >= spot):
            continue
        m = _mid(c, at)
        exp = expiry_close(c["expiry"])
        if m is None or exp <= at:
            continue
        t = fno.years_to(exp, at)
        intrinsic = max(0.0, spot - k_) if c["kind"] == "CE" else max(0.0, k_ - spot)
        if m - intrinsic < MIN_TIME_VALUE:
            continue
        v = implied_vol(m, spot, k_, t, c["kind"])
        if v is None:
            continue
        out.append({"key": c.get("key"), "kind": c["kind"], "strike": k_, "exp": exp, "t": t, "mid": m,
                    "bid": c["bid"], "ask": c["ask"], "k": math.log(k_ / spot), "iv": v,
                    "vega": _vega(spot, k_, t, v)})
    return out


def fit(contracts: list[dict[str, Any]], at: int) -> Optional[Surface]:
    """The surface of one snapshot, or None without a spot or any usable quote."""
    spot = spot_of(contracts, at)
    if not spot:
        return None
    pts = points(contracts, at, spot)
    by_exp: dict[int, list[dict]] = {}
    for p in pts:
        by_exp.setdefault(p["exp"], []).append(p)
    smiles = []
    for exp, ps in sorted(by_exp.items()):
        sm = _fit_smile([(p["k"], p["iv"], p["vega"]) for p in ps])
        ks = [p["k"] for p in ps]
        smiles.append({"exp": exp, **sm, "kmin": min(ks), "kmax": max(ks), "n": len(ps)})
    return Surface(at, spot, smiles) if smiles else None


# ---- does it beat flat IV? ---------------------------------------------------

def _price(kind: str, strike: float, spot: float, t: float, v: float) -> float:
    return fno.bs_price(spot, strike, t, v, kind)


def evaluate(snapshots: Iterable[tuple[int, list[dict[str, Any]]]], *, lag: int = 1,
             flat: Optional[Callable[[int], Optional[float]]] = None) -> dict[str, Any]:
    """Price each snapshot's quoted OTM options with the surface fitted `lag`
    snapshots earlier (what a backtest at that moment could have known) and
    with flat IV — `flat(t)` (VIX / 100 for an index, None to skip) and the
    lagged surface's own ATM IV for every strike — against the quote's mid.
    Per day: mean absolute error (₹ and % of mid) and the share priced inside
    the bid-ask, then the days the surface won and a bootstrap CI of its daily
    MAE edge over each flat method."""
    from app.research import stats as rstats

    from collections import deque

    days: dict[int, dict[str, list]] = {}
    methods = ("surface", "flat_atm") + (("flat",) if flat else ())
    back: deque = deque(maxlen=lag)          # the last `lag` fits: snapshots stream through, months never sit in memory
    for t, contracts in snapshots:
        surf = back[0] if len(back) == lag else None
        back.append(fit(contracts, t))
        if surf is None or (t + IST) // DAY != (surf.t + IST) // DAY:      # never across a night
            continue
        spot = spot_of(contracts, t)
        if not spot:
            continue
        fv = flat(t) if flat else None
        day = days.setdefault((t + IST) // DAY, {m: [] for m in methods})
        for p in points(contracts, t, spot):
            ivs = {"surface": surf.iv(p["strike"], spot, t, p["exp"]), "flat_atm": surf.atm()}
            if flat:
                ivs["flat"] = fv
            if any(ivs[m] is None for m in methods):
                continue
            for m in methods:
                px = _price(p["kind"], p["strike"], spot, p["t"], ivs[m])
                day[m].append((abs(px - p["mid"]), abs(px - p["mid"]) / p["mid"], p["bid"] <= px <= p["ask"]))
    per_day, n_all = [], 0
    for d, rows in sorted(days.items()):
        if not rows["surface"]:
            continue
        n_all += len(rows["surface"])
        per_day.append({"day": d, "n": len(rows["surface"]),
                        **{m: {"mae": round(sum(r[0] for r in rs) / len(rs), 3),
                               "mape_pct": round(sum(r[1] for r in rs) / len(rs) * 100, 2),
                               "inside_pct": round(sum(r[2] for r in rs) / len(rs) * 100, 1)}
                           for m, rs in rows.items()}})
    out: dict[str, Any] = {"days": len(per_day), "quotes": n_all, "lag": lag, "methods": list(methods),
                           "per_day": per_day}
    total = {m: {k: round(sum(x[m][k] * x["n"] for x in per_day) / n_all, 3) if n_all else None
                 for k in ("mae", "mape_pct", "inside_pct")} for m in methods}
    out["overall"] = total
    vs = {}
    for m in methods[1:]:
        edge = [x[m]["mae"] - x["surface"]["mae"] for x in per_day]          # > 0: the surface priced closer
        lo, hi = rstats.bootstrap_ci(edge)
        ci = [round(lo, 3), round(hi, 3)] if lo is not None else None
        vs[m] = {"days_won": sum(1 for e in edge if e > 0), "mean_edge": round(sum(edge) / len(edge), 3) if edge else None,
                 "ci95": ci}
    out["vs"] = vs
    MIN_DAYS = 10
    lows = [v["ci95"][0] if v["ci95"] else None for v in vs.values()]
    out["min_days"] = MIN_DAYS
    out["beats_flat"] = (len(per_day) >= MIN_DAYS and all(lo is not None and lo > 0 for lo in lows))
    out["verdict"] = ("not enough recorded days yet" if len(per_day) < MIN_DAYS else
                      "the surface prices closer than flat IV" if out["beats_flat"] else
                      "the surface does not beat flat IV")
    return out
