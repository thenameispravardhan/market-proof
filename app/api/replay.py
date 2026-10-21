"""`/api/replay` — prices for trading a future or an option during Bar Replay.

Bar replay steps through history, and the browser simulates the trades taken
there (nothing here places an order). Trading the chart's own symbol fills on
its candles; a future or option of that symbol needs the CONTRACT's price at
the replayed time:

  * a contract that is still listed (a replay inside its life) — its real
    Fyers candles;
  * one that has expired (Fyers keeps no history for those) — a model: a
    future at fair value (spot x e^(rT)), an option by Black-Scholes off the
    underlying's real candles, with India VIX (index options) or the
    underlying's realised volatility (stock options). The response says
    which, so the page can label a model price as an estimate.

The expiry and strike are picked as they would have been AT the replayed
time: the expiry calendar of that day, the strike nearest that day's price.
"""
from __future__ import annotations

import math
import time
from typing import Any, Optional

from fastapi import APIRouter, Query

from app.algo import fno
from app.logging_config import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/replay", tags=["replay"])

DAY = 86400
IST_S = 19800
VIX = "NSE:INDIAVIX-INDEX"
_CACHE: dict[tuple[str, str, int, int], tuple[float, list[list[float]]]] = {}
_CACHE_TTL_S = 60.0


def _res_seconds(res: str) -> int:
    if res == "D":
        return DAY
    if res.endswith("S"):
        return int(res[:-1])
    return int(res) * 60


def _window_s(res: str) -> int:
    """How far past the replayed bar one response reaches; the page asks for
    the next stretch as the replay gets there (`more`)."""
    s = _res_seconds(res)
    if res.endswith("S"):
        return 2 * DAY
    if s <= 180:
        return 10 * DAY
    if s <= 900:
        return 30 * DAY
    if s < DAY:
        return 90 * DAY
    return 365 * DAY


async def _candles(symbol: str, res: str, a: int, b: int) -> Optional[list[list[float]]]:
    """Fyers candles [[ts, o, h, l, c, v], ...] in [a, b]; None when the call
    failed or no Fyers account is connected. Long ranges go in chunks (Fyers
    serves at most ~100 days of intraday / a year of daily candles a call)."""
    key = (symbol, res, a, b)
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL_S:
        return hit[1]
    from app.api.market import _fyers_backend

    backend = _fyers_backend()
    if backend is None or not hasattr(backend, "get_history_range"):
        return None
    span = (360 if res == "D" else 95) * DAY
    out: list[list[float]] = []
    lo = a
    while lo <= b:
        hi = min(b, lo + span)
        rows = await backend.get_history_range(symbol, resolution=res, from_ts=lo, to_ts=hi)
        if rows is None:
            return None
        out += [list(r) for r in rows if isinstance(r, (list, tuple)) and len(r) >= 5]
        lo = hi + 1
    out.sort(key=lambda r: r[0])
    if len(_CACHE) > 64:
        _CACHE.clear()
    _CACHE[key] = (time.time(), out)
    return out


def _ist_day(ts: float) -> int:
    return int((ts + IST_S) // DAY)


def _day_start(ts: float) -> int:
    """00:00 IST of the day of `ts` (fetch windows snap to days, so the quotes
    of a replay standing on one day share the cache)."""
    return _ist_day(ts) * DAY - IST_S


def _close_of_day(ts: float) -> int:
    """15:30 IST on the day of `ts` — when a contract expires."""
    return _ist_day(ts) * DAY - IST_S + fno.EXPIRY_CLOSE_S


def _tick(x: float) -> float:
    return round(max(0.05, round(x / 0.05) * 0.05), 2)


def _last_before(rows: list[list[float]], t: float) -> Optional[list[float]]:
    """The newest candle that STARTED before `t`: the price at `t`."""
    best = None
    for r in rows:
        if r[0] < t:
            best = r
        else:
            break
    return best


def _hv_by_day(daily: list[list[float]], default: float) -> dict[int, float]:
    """Annualised volatility of the ~20 daily returns BEFORE each day (no
    lookahead into the day being replayed)."""
    closes: list[tuple[int, float]] = []
    for r in daily:
        k = _ist_day(r[0])
        if closes and closes[-1][0] == k:
            closes[-1] = (k, r[4])
        else:
            closes.append((k, r[4]))
    rets = [math.log(b[1] / a[1]) for a, b in zip(closes, closes[1:]) if a[1] > 0 and b[1] > 0]
    out: dict[int, float] = {}
    for j, (k, _) in enumerate(closes):
        w = rets[max(0, j - 21):max(0, j - 1)]
        if len(w) >= 5:
            m = sum(w) / len(w)
            out[k] = max(0.05, math.sqrt(sum((x - m) ** 2 for x in w) / (len(w) - 1)) * math.sqrt(252))
    return out


def _listed_future(name: str, exp: int) -> Optional[tuple[str, int]]:
    d = _ist_day(exp)
    for e, sym in fno.master()["futures"].get(name, []):
        if abs(_ist_day(e) - d) <= 3:
            return sym, e
    return None


def _listed_grid(name: str, exp: int, right: str) -> tuple[Optional[int], dict[float, str]]:
    """A listed expiry within 3 days of the calendar's (a holiday moves it
    back) and that expiry's strikes for one right: {strike: symbol}."""
    from app.services.instrument_master import get_master

    d = _ist_day(exp)
    rows = [r for r in get_master().options_of(name) if r[2] == right and abs(_ist_day(r[3]) - d) <= 3]
    if not rows:
        return None, {}
    listed = min({r[3] for r in rows}, key=lambda e: abs(_ist_day(e) - d))
    return listed, {r[1]: r[0] for r in rows if _ist_day(r[3]) == _ist_day(listed)}


def _pick_on_grid(spot: float, grid: list[float], right: str, mode: str, steps: int) -> float:
    i = min(range(len(grid)), key=lambda j: abs(grid[j] - spot))
    n = int(steps or 0) if mode in ("ITM", "OTM") else 0
    itm_dir = -1 if right == "CE" else 1               # CE ITM = lower strikes
    sign = itm_dir if mode == "ITM" else -itm_dir
    return grid[max(0, min(len(grid) - 1, i + sign * n))]


@router.get("/contract")
async def contract(
    underlying: str,
    kind: str,
    t: int,
    from_ts: int = Query(..., alias="from"),
    to_ts: int = Query(..., alias="to"),
    resolution: str = "5",
    expiry: str = "current",
    expiry_kind: str = "weekly",
    strike_mode: str = "ATM",
    steps: int = Query(0, ge=0, le=50),
    strike: Optional[float] = None,
    expiry_ts: Optional[int] = None,
    spot: Optional[float] = None,
) -> dict[str, Any]:
    """The contract a replay trade at time `t` (epoch, the replayed bar's
    close) would take, and its candles from `from` (the replayed bar's start)
    for the stretch of the replay ahead.

    kind: FUT | CE | PE. expiry: current | next (of that day). expiry_kind:
    weekly | monthly (options; weekly only where weeklies trade). strike:
    strike_mode ATM | ITM | OTM with `steps`, or an exact `strike`. A
    continuation passes the `strike` and `expiry_ts` the first answer gave.
    Always 200: `{ok: false, reason}` when it can't be priced."""
    from app.api.market import CHART_RESOLUTIONS
    from app.execution.symbols import resolve_fyers_symbol

    kind = (kind or "").strip().upper()
    res = (resolution or "").strip().upper()
    if kind not in ("FUT", "CE", "PE"):
        return {"ok": False, "reason": "kind is FUT, CE or PE"}
    if res not in CHART_RESOLUTIONS:
        return {"ok": False, "reason": f"unsupported resolution {resolution!r}"}
    if expiry not in ("current", "next") or expiry_kind not in ("weekly", "monthly"):
        return {"ok": False, "reason": "expiry is current / next, expiry_kind weekly / monthly"}
    if strike_mode not in ("ATM", "ITM", "OTM"):
        return {"ok": False, "reason": "strike_mode is ATM, ITM or OTM"}
    if to_ts <= from_ts:
        return {"ok": False, "reason": "empty time range"}
    sym = resolve_fyers_symbol((underlying or "").strip().upper()) or (underlying or "").strip().upper()
    if fno.is_derivative(sym):
        return {"ok": False, "reason": "open the underlying's chart (the index or stock) to trade its futures & options"}
    name, exch = fno.fno_name(sym), fno.exchange_of(sym)
    if not fno.is_fno(name):
        return {"ok": False, "reason": f"{name} has no futures & options"}

    from app.api.market import _fyers_backend

    if _fyers_backend() is None:
        return {"ok": False, "reason": "connect a Fyers account for replay prices"}
    tf = _res_seconds(res)
    day0 = _day_start(from_ts)
    end = min(to_ts, _day_start(from_ts + _window_s(res)) + DAY - 1)
    u = await _candles(sym, res, day0 - (10 if res == "D" else 4) * DAY, end)
    if u is None:
        return {"ok": False, "reason": "the Fyers history call failed — try again"}
    if not u:
        return {"ok": False, "reason": f"no {name} candles around the replayed time"}
    tdays = frozenset(_ist_day(r[0]) for r in u)
    at = _last_before(u, t)
    s0 = float(spot) if spot else float(at[4] if at else u[0][1])

    # The expiry and strike as of the replayed day.
    if expiry_ts:
        exp = int(expiry_ts)
    else:
        ek = "monthly" if kind == "FUT" or name not in fno.WEEKLY else expiry_kind
        exp = fno.expiry_after(name, exch, ek, expiry, t, tdays)
    symbol: Optional[str] = None
    k: Optional[float] = None
    if kind == "FUT":
        hit = _listed_future(name, exp)
        if hit:
            symbol = hit[0]
            if expiry_ts is None:
                exp = _close_of_day(hit[1])      # the listed date (a holiday moves it back)
    else:
        listed, grid = _listed_grid(name, exp, kind)
        if strike is not None:
            k = float(strike)
        elif grid:
            k = _pick_on_grid(s0, sorted(grid), kind, strike_mode, steps)
        else:
            k = fno.pick_strike(s0, fno.default_step(name, s0), kind, strike_mode, steps)
        if listed is not None and k in grid:
            symbol = grid[k]
            if expiry_ts is None:
                exp = _close_of_day(listed)
    lot = (fno.contract_lot(symbol) if symbol else None) or fno.known_lot(name, exp) or 1
    label = (f"{name} FUT {fno.label_date(exp)}" if kind == "FUT"
             else f"{name} {fno.label_date(exp)} {k:g} {kind}")
    out: dict[str, Any] = {"ok": True, "contract": {"kind": kind, "name": name, "strike": k, "expiry": exp,
                                                    "label": label, "lot": int(lot), "symbol": symbol},
                           "spot": round(s0, 2), "more": end < to_ts and end < exp}

    # Real candles while the contract is listed and had traded by `t`.
    if symbol:
        real = await _candles(symbol, res, day0 - 3 * DAY, end)
        if real and any(t - 4 * DAY <= r[0] < t for r in real):
            rows = [[r[0], *(round(float(x), 2) for x in r[1:5])] for r in real if r[0] < exp]
            last = _last_before(rows, t)
            return {**out, "source": "real", "note": f"{symbol} — real candles",
                    "candles": rows, "price_at_t": last[4] if last else None}

    # Model: fair-value future / Black-Scholes option off the underlying's real candles.
    bars = [r for r in u if from_ts <= r[0] < exp]
    if kind == "FUT":
        rows = [[r[0], *(round(r[j] * math.exp(fno.RISK_FREE * fno.years_to(exp, r[0])), 2) for j in (1, 2, 3, 4))]
                for r in bars]
        note = f"model: {name} at fair value (spot x e^rT) — the contract has expired, so no real candles"
        iv_t = None
    else:
        default = 0.15 if name in fno.INDEX_LOTS else 0.30
        daily = await _candles(sym, "D", day0 - 70 * DAY, end) or []
        hv = _hv_by_day(daily, default)
        vix: list[list[float]] = []
        if name in fno.INDEX_LOTS:
            vix = await _candles(VIX, res, day0 - 4 * DAY, end) or []

        def iv_at(ts: float) -> float:
            if vix:
                v = _last_before(vix, ts + 1)
                if v and v[4] > 0:
                    return v[4] / 100
            k_ = _ist_day(ts)
            return hv.get(k_) or next((hv[d] for d in sorted(hv, reverse=True) if d < k_), default)

        rows = []
        for r in bars:
            iv = iv_at(r[0])
            yo, yc = fno.years_to(exp, r[0]), fno.years_to(exp, r[0] + tf)
            po, pc = fno.bs_price(r[1], k, yo, iv, kind), fno.bs_price(r[4], k, yc, iv, kind)
            ph, pl = fno.bs_price(r[2], k, (yo + yc) / 2, iv, kind), fno.bs_price(r[3], k, (yo + yc) / 2, iv, kind)
            rows.append([r[0], _tick(po), _tick(max(po, pc, ph, pl)), _tick(min(po, pc, ph, pl)), _tick(pc)])
        iv_t = round(iv_at(t) * 100, 1)
        src = "India VIX" if vix else "realised volatility"
        note = (f"model: Black-Scholes on {name} with {src} (IV {iv_t}%) — "
                + ("the contract has expired, so no real candles" if not symbol else "no trades by the replayed time"))
    if not rows:
        return {"ok": False, "reason": f"no {name} candles after the replayed time"}
    last = _last_before(rows, t)
    return {**out, "source": "model", "note": note, "iv": iv_t, "candles": rows,
            "price_at_t": last[4] if last else rows[0][1]}

