"""F&O plumbing for the Algo engine: expiries, strikes, lot sizes, option pricing.

Backtests cannot replay real option premiums: Fyers serves history only for
contracts that are still listed, so an expired weekly is gone. Option
backtests therefore price each leg with Black-Scholes off the underlying's
real candles (IV from the underlying's realised volatility, India VIX, or a
fixed number) — results are ESTIMATES and the API labels them so. Futures
backtests trade the underlying's price (basis ignored). Live trading uses the
real contracts: futures from the NSE/BSE F&O scrip master, options from the
live Fyers option chain.

The F&O master (public.fyers.in/sym_details/NSE_FO.csv, ~15 MB) is reduced to
what the engine needs — lot size, futures per underlying, strikes per expiry —
and cached until the file changes.
"""
from __future__ import annotations

import csv
import math
import re
import time
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from app.logging_config import get_logger

log = get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
EXPIRY_CLOSE_S = 15 * 3600 + 30 * 60          # contracts expire at 15:30 IST
RISK_FREE = 0.065                              # ~ 91-day T-bill
YEAR_S = 365 * 86400

# Fyers index symbol -> (F&O name, strike step, exchange)
INDICES: dict[str, tuple[str, float, str]] = {
    "NSE:NIFTY50-INDEX": ("NIFTY", 50, "NSE"),
    "NSE:NIFTYBANK-INDEX": ("BANKNIFTY", 100, "NSE"),
    "NSE:FINNIFTY-INDEX": ("FINNIFTY", 50, "NSE"),
    "NSE:MIDCPNIFTY-INDEX": ("MIDCPNIFTY", 25, "NSE"),
    "NSE:NIFTYNXT50-INDEX": ("NIFTYNXT50", 100, "NSE"),
    "BSE:SENSEX-INDEX": ("SENSEX", 100, "BSE"),
    "BSE:BANKEX-INDEX": ("BANKEX", 100, "BSE"),
}
# Only these still list weeklies (SEBI, Nov 2024); everything else is monthly.
WEEKLY = {"NIFTY", "SENSEX"}
# Fallback lot sizes when the master isn't downloaded yet.
INDEX_LOTS = {"NIFTY": 65, "BANKNIFTY": 30, "FINNIFTY": 60, "MIDCPNIFTY": 120,
              "NIFTYNXT50": 25, "SENSEX": 20, "BANKEX": 30}
# Exchange freeze limits (max quantity per order) for index F&O.
# ponytail: static table the exchange revises; stock F&O limits not covered (Fyers rejects those itself)
INDEX_FREEZE = {"NIFTY": 1800, "BANKNIFTY": 900, "FINNIFTY": 1800, "MIDCPNIFTY": 2800,
                "NIFTYNXT50": 600, "SENSEX": 1000, "BANKEX": 900}

MASTER_DIR = Path(__file__).resolve().parents[2] / "data" / "scrip_master"
FO_URLS = {"NSE_FO": "https://public.fyers.in/sym_details/NSE_FO.csv",
           "BSE_FO": "https://public.fyers.in/sym_details/BSE_FO.csv"}
# Searchable / chartable on the Trade page, refreshed with the F&O masters, but not part of master()'s
# lots: MCX quotes per 10 g / per barrel against a 1 kg / 100 barrel lot, a multiplier the CSV doesn't carry.
EXTRA_URLS = {"MCX_COM": "https://public.fyers.in/sym_details/MCX_COM.csv",
              "NSE_CD": "https://public.fyers.in/sym_details/NSE_CD.csv"}


def fno_name(underlying: str) -> str:
    """NSE:NIFTY50-INDEX -> NIFTY, NSE:SBIN-EQ -> SBIN."""
    u = underlying.upper()
    if u in INDICES:
        return INDICES[u][0]
    return u.split(":", 1)[-1].rsplit("-", 1)[0]


def exchange_of(underlying: str) -> str:
    u = underlying.upper()
    return INDICES[u][2] if u in INDICES else u.split(":", 1)[0] if ":" in u else "NSE"


# ---- expiry calendar (rule-based, for backtests) ----------------------------

def _expiry_weekday(exch: str, d: date) -> int:
    """Mon=0. SEBI moved NSE expiries Thu -> Tue and BSE to Thu from 2025-09-01;
    BSE's SENSEX weekly ran Fri until 2024 and Tue during Jan-Aug 2025."""
    if exch == "BSE":
        return 4 if d < date(2025, 1, 1) else 1 if d < date(2025, 9, 1) else 3
    return 3 if d < date(2025, 9, 1) else 1


def _last_weekday(year: int, month: int, wd: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    d = nxt - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - wd) % 7)


def _close_ts(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=IST).timestamp()) + EXPIRY_CLOSE_S


@lru_cache(maxsize=4096)
def _expiries_from(name: str, exch: str, kind: str, d: date) -> tuple[date, ...]:
    """The next three expiry dates on or after `d` (day-by-day scan; cached per day)."""
    weekly = kind == "weekly" and name in WEEKLY
    out: list[date] = []
    day = d
    while len(out) < 3:
        wd = _expiry_weekday(exch, day)
        if day.weekday() == wd and (weekly or day == _last_weekday(day.year, day.month, wd)):
            out.append(day)
        day += timedelta(days=1)
    return tuple(out)


def expiry_after(name: str, exch: str, kind: str, which: str, t: float,
                 trading_days: Optional[frozenset] = None) -> int:
    """Epoch of the contract's 15:30 close. `which` = current | next.

    `trading_days` (IST day numbers the market actually traded, read off the
    candles) moves an expiry that fell on an exchange holiday back to the
    previous trading day — no holiday list to maintain. Outside the span the
    candles cover, the plain weekday rule applies.
    """
    days: list[date] = []
    for x in _expiries_from(name, exch, kind, datetime.fromtimestamp(t, IST).date()):
        if trading_days:
            k = (x - date(1970, 1, 1)).days
            lo, hi = min(trading_days), max(trading_days)
            while lo <= k <= hi and k not in trading_days:
                k -= 1
            x = date(1970, 1, 1) + timedelta(days=k)
        if _close_ts(x) >= t and x not in days:
            days.append(x)
    return _close_ts(days[0 if which == "current" else 1])


# ---- Black-Scholes -----------------------------------------------------------

def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, t_years: float, iv: float, right: str, r: float = RISK_FREE) -> float:
    """European premium; intrinsic at/after expiry. Floor 0.05 (one tick)."""
    intrinsic = max(0.0, spot - strike) if right == "CE" else max(0.0, strike - spot)
    if t_years <= 0 or iv <= 0 or spot <= 0:
        return max(intrinsic, 0.05)
    sq = iv * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + iv * iv / 2) * t_years) / sq
    d2 = d1 - sq
    disc = math.exp(-r * t_years)
    if right == "CE":
        p = spot * _ncdf(d1) - strike * disc * _ncdf(d2)
    else:
        p = strike * disc * _ncdf(-d2) - spot * _ncdf(-d1)
    return max(p, intrinsic * disc, 0.05)


def bs_greeks(spot: float, strike: float, t_years: float, iv: float, right: str,
              r: float = RISK_FREE) -> dict[str, float]:
    """The Greeks of ONE option unit:

    delta (per point), gamma (delta change per point), theta (premium per
    calendar day), vega (premium per 1 vol point), rho (premium per 1% rate),
    vanna (delta change per 1 vol point), charm (delta change per day that
    passes), vomma (vega change per 1 vol point), speed (gamma change per
    point) and prob_itm (risk-neutral chance it expires in the money).
    At expiry: delta 0 / ±1, the rest 0."""
    sgn = 1 if right == "CE" else -1
    if t_years <= 0 or iv <= 0 or spot <= 0:
        itm = spot > strike if right == "CE" else spot < strike
        return {"delta": sgn * float(itm), "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0, "vanna": 0.0,
                "charm": 0.0, "vomma": 0.0, "speed": 0.0, "prob_itm": float(itm)}
    rt = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + iv * iv / 2) * t_years) / (iv * rt)
    d2 = d1 - iv * rt
    pdf = math.exp(-d1 * d1 / 2) / math.sqrt(2 * math.pi)
    disc = strike * math.exp(-r * t_years)
    gamma = pdf / (spot * iv * rt)
    vega = spot * pdf * rt
    return {"delta": _ncdf(d1) - (right == "PE"),
            "gamma": gamma,
            "theta": (-spot * pdf * iv / (2 * rt) - sgn * r * disc * _ncdf(sgn * d2)) / 365,
            "vega": vega / 100,
            "rho": sgn * t_years * disc * _ncdf(sgn * d2) / 100,
            "vanna": -pdf * d2 / iv / 100,
            # the same for a call and a put: their deltas differ by a constant
            "charm": -pdf * (2 * r * t_years - d2 * iv * rt) / (2 * t_years * iv * rt) / 365,
            "vomma": vega * d1 * d2 / iv / 10000,
            "speed": -gamma / spot * (d1 / (iv * rt) + 1),
            "prob_itm": _ncdf(sgn * d2)}


def implied_vol(price: float, spot: float, strike: float, t_years: float, right: str) -> float:
    """The IV (decimal) at which Black-Scholes gives `price`; bisection on 1%-500%."""
    lo, hi = 0.01, 5.0
    for _ in range(50):
        mid = (lo + hi) / 2
        if bs_price(spot, strike, t_years, mid, right) < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def label_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, IST).strftime("%d%b%y").upper()


def years_to(expiry_ts: float, t: float) -> float:
    return max(expiry_ts - t, 60.0) / YEAR_S


# ---- strikes -----------------------------------------------------------------

def default_step(name: str, spot: float) -> float:
    """Strike interval: index table, then the master, then NSE's price bands."""
    for _, (n, step, _) in INDICES.items():
        if n == name:
            return step
    m = master()
    strikes = m["strikes"].get(name)
    if strikes:
        first = strikes[min(strikes)]
        diffs = [round(b - a, 2) for a, b in zip(first, first[1:]) if b > a]
        if diffs:
            return min(diffs)
    for cap, step in ((250, 2.5), (500, 5), (1000, 10), (2500, 20), (5000, 50)):
        if spot <= cap:
            return step
    return 100.0


def pick_strike(spot: float, step: float, right: str, mode: str, steps: int) -> float:
    """ATM, or N strikes in-the-money / out-of-the-money for this right."""
    atm = max(step, round(spot / step) * step)
    n = int(steps or 0) if mode in ("ITM", "OTM") else 0
    itm_dir = -1 if right == "CE" else 1          # CE ITM = lower strikes
    sign = itm_dir if mode == "ITM" else -itm_dir
    # A deep strike can't go to zero or below (a 20-OTM put on a cheap stock).
    return round(max(step, atm + sign * n * step), 2)


def strike_for_premium(spot: float, step: float, right: str, target: float, price_at) -> float:
    """The strike (ATM +/- 40 steps) whose premium is closest to `target`."""
    atm = round(spot / step) * step
    cands = [atm + k * step for k in range(-40, 41) if atm + k * step > 0]
    return min(cands, key=lambda k: abs(price_at(k) - target))


def strike_for_delta(spot: float, step: float, right: str, target: float, delta_at) -> float:
    """The strike (ATM +/- 40 steps) whose |delta| is closest to `target` (0.25 = a 25-delta option)."""
    atm = round(spot / step) * step
    cands = [atm + k * step for k in range(-40, 41) if atm + k * step > 0]
    return min(cands, key=lambda k: (abs(abs(delta_at(k)) - target), abs(k - atm)))


# ---- margin for writing options / futures -------------------------------------
#
# Selling an option needs far more money than buying one. A buyer can lose at
# most the premium, so the premium is all that is blocked. A writer's loss is
# open-ended, so the exchange blocks SPAN (the worst loss over a grid of price
# and volatility moves) plus an exposure margin on the contract value — around
# ₹1-1.5 lakh per NIFTY lot for a naked ATM sell against a few thousand for the
# same option bought. Bought wings cap the worst loss, which is why an iron
# condor needs a fraction of a strangle's margin. This is an ESTIMATE of that
# method, not NSE's risk-parameter file: the broker's margin calculator decides.

SCAN_PCT = {"index": 6.0, "stock": 13.0}       # price scan range, % of the underlying
EXPOSURE_PCT = {"index": 2.0, "stock": 3.5}    # exposure margin, % of contract value
EXPIRY_DAY_ELM = 2.0       # SEBI, Nov 2024: extra 2% on short index options on their expiry day
VOL_SCAN = 0.04            # volatility scan: IV up / down 4 points
# (fraction of the scan range, share of the loss counted): 7 price moves x 2 vols, plus the two
# extreme moves (2x the range) counted at 35% — the case that matters for deep OTM shorts.
SPAN_MOVES = [(k / 3, 1.0) for k in range(-3, 4)] + [(-2.0, 0.35), (2.0, 0.35)]


def asset_class(name: str) -> str:
    return "index" if name in INDEX_LOTS else "stock"


def leg_iv(leg: dict[str, Any], u: float, iv: Optional[float]) -> Optional[float]:
    """A backtest option leg's IV: the flat IV plus its skew — instrument.iv
    put_skew / call_skew IV points per 1% out of the money, measured at the
    current spot (sticky moneyness), never below 1%. No skew on the leg (every
    live leg) = the flat IV. Every backtest pricing path goes through here,
    SPAN margin included."""
    sk = leg.get("skew")
    if not sk or iv is None or leg.get("kind") not in ("CE", "PE"):
        return iv
    otm = (u - leg["K"]) / u * 100 if leg["kind"] == "PE" else (leg["K"] - u) / u * 100
    return max(0.01, iv + max(0.0, otm) * sk[0 if leg["kind"] == "PE" else 1] / 100)


def span_margin(legs: list[dict[str, Any]], spot: float, t: float, *, name: str, iv: Optional[float] = None,
                price_key: str = "price", qty_key: str = "qty", scan_pct: Optional[float] = None,
                exposure_pct: Optional[float] = None) -> dict[str, float]:
    """Funds blocked by one position of CE / PE / FUT legs on one underlying.

    {span, exposure, premium, total}: span = worst scenario loss of the whole
    position (legs offset each other: a bought wing's gain covers the short's
    loss); exposure on every short option and future; premium = what bought
    options cost. A bought option's own fall in value is never counted twice —
    its premium already pays for it. `iv` None = each leg's IV implied from its
    `price_key` (live quotes); a number = every leg priced at it (backtests)."""
    cls = asset_class(name)
    scan = (SCAN_PCT[cls] if scan_pct is None else scan_pct) / 100
    expo = (EXPOSURE_PCT[cls] if exposure_pct is None else exposure_pct) / 100
    day = int(t + 19800) // 86400
    priced = []
    exposure = premium = 0.0
    for lg in legs:
        q = abs(lg[qty_key])
        if lg["kind"] == "FUT":
            priced.append((lg, q, None, None))
            exposure += expo * spot * q
            continue
        yrs = years_to(lg["exp"], t)
        px = float(lg.get(price_key) or 0)
        v = leg_iv(lg, spot, iv) if iv is not None else (implied_vol(px, spot, lg["K"], yrs, lg["kind"]) if px > 0 else 0.15)
        priced.append((lg, q, yrs, v))
        if lg["act"] > 0:
            premium += px * q
        else:
            exposure += expo * spot * q
            if cls == "index" and int(lg["exp"] + 19800) // 86400 == day:
                exposure += EXPIRY_DAY_ELM / 100 * spot * q
    worst = 0.0
    if any(lg["act"] < 0 or lg["kind"] == "FUT" for lg, *_ in priced):
        for move, weight in SPAN_MOVES:
            s1 = spot * (1 + move * scan)
            for dv in (-VOL_SCAN, VOL_SCAN):
                pnl = 0.0
                for lg, q, yrs, v in priced:
                    if lg["kind"] == "FUT":
                        pnl += lg["act"] * (s1 - spot) * q
                        continue
                    now = bs_price(spot, lg["K"], yrs, v, lg["kind"])
                    chg = lg["act"] * (bs_price(s1, lg["K"], yrs, max(0.01, v + dv), lg["kind"]) - now) * q
                    pnl += chg if lg["act"] < 0 else max(0.0, chg)
                worst = max(worst, -pnl * weight)
    return {"span": round(worst, 2), "exposure": round(exposure, 2), "premium": round(premium, 2),
            "total": round(worst + exposure + premium, 2), "scan_pct": scan * 100, "exposure_pct": expo * 100}


# ---- F&O master ----------------------------------------------------------------

_EMPTY = {"lots": {}, "futures": {}, "strikes": {}, "lot_expiries": {}, "contracts": {}}
_cache: dict[str, Any] = {"mtime": None, "data": _EMPTY}


def master() -> dict[str, Any]:
    """{lots: name->lot, futures: name->[(expiry_ts, symbol)], strikes: name->{expiry_ts: [strikes]},
    lot_expiries: name->[(expiry_ts, lot)], contracts: symbol->lot}.

    `lots` is the lot of the nearest live expiry when the master loaded. NSE
    revises lot sizes a few times a year and a revision only applies to new
    expiries, so during the changeover one underlying lists contracts with two
    lots: `lot_expiries` keeps the lot per expiry and `contracts` the exact lot
    of every contract of such an underlying (only those, to keep it small)."""
    files = [MASTER_DIR / f"{k}.csv" for k in FO_URLS if (MASTER_DIR / f"{k}.csv").exists()]
    mtime = tuple(f.stat().st_mtime for f in files)
    if _cache["mtime"] == mtime:
        return _cache["data"]
    by_exp: dict[str, dict[int, int]] = {}
    sym_lot: dict[str, tuple[str, int]] = {}
    futures: dict[str, list[tuple[int, str]]] = {}
    strikes: dict[str, dict[int, set[float]]] = {}
    home: dict[str, str] = {}      # name -> its exchange; NSE_FO is read first, so SBIN stays on NSE
    for f in files:
        with f.open(encoding="utf-8", errors="replace") as fh:
            for row in csv.reader(fh):
                if len(row) < 17 or ":" not in row[9]:
                    continue
                name, kind = row[13].strip().upper(), row[16].strip().upper()
                exch = row[9].split(":", 1)[0].strip().upper()
                if home.setdefault(name, exch) != exch:
                    continue           # BSE's thin copy of an NSE contract

                try:
                    exp, lot = int(float(row[8])), int(float(row[3]))
                except ValueError:
                    continue
                if lot <= 0:
                    continue
                by_exp.setdefault(name, {}).setdefault(exp, lot)
                sym_lot[row[9].strip().upper()] = (name, lot)
                if kind in ("CE", "PE"):
                    try:
                        strikes.setdefault(name, {}).setdefault(exp, set()).add(float(row[15]))
                    except ValueError:
                        pass
                elif row[9].strip().upper().endswith("FUT"):
                    futures.setdefault(name, []).append((exp, row[9].strip().upper()))
    now = time.time()
    lot_expiries = {n: sorted(v.items()) for n, v in by_exp.items()}
    lots = {n: next((lot for e, lot in v if e >= now), v[-1][1]) for n, v in lot_expiries.items()}
    mixed = {n for n, v in lot_expiries.items() if len({lot for _, lot in v}) > 1}
    contracts = {s: lot for s, (n, lot) in sym_lot.items() if n in mixed}
    data = {"lots": lots,
            "futures": {k: sorted(v) for k, v in futures.items()},
            "strikes": {k: {e: sorted(s) for e, s in v.items()} for k, v in strikes.items()},
            "lot_expiries": lot_expiries,
            "contracts": contracts}
    _cache.update(mtime=mtime, data=data)
    log.info("fno.master_loaded", underlyings=len(lots), files=len(files), lot_changeovers=sorted(mixed))
    return data


async def ensure_master(max_age_h: float = 20.0) -> None:
    """Download the F&O masters when missing or older than a day, then reload
    the app-wide instrument master too (futures become searchable)."""
    import httpx

    urls = {**FO_URLS, **EXTRA_URLS}
    stale = [k for k in urls if not (MASTER_DIR / f"{k}.csv").exists()
             or time.time() - (MASTER_DIR / f"{k}.csv").stat().st_mtime > max_age_h * 3600]
    if not stale:
        return
    MASTER_DIR.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        for k in stale:
            try:
                r = await client.get(urls[k])
                r.raise_for_status()
                tmp = MASTER_DIR / f"{k}.csv.tmp"
                tmp.write_text(r.text, encoding="utf-8")
                tmp.replace(MASTER_DIR / f"{k}.csv")
            except Exception as e:  # noqa: BLE001 — a stale master beats none
                log.warning("fno.master_download_failed", file=k, error=str(e)[:200])
    from app.services.instrument_master import get_master

    get_master().reload()


def known_lot(name: str, now: Optional[float] = None) -> Optional[int]:
    """The F&O lot for an underlying, or None when nothing reliable is known.

    The scrip master's lot of the nearest live expiry first; the current
    exchange index lots (`INDEX_LOTS`) when the master isn't downloaded. A
    stock's lot is only ever taken from the master — stock lots differ per
    stock and change with every NSE revision, so there's no safe guess."""
    m = master()
    exps = (m.get("lot_expiries") or {}).get(name)
    if exps:
        now = now or time.time()
        return next((lot for e, lot in exps if e >= now), exps[-1][1])
    return m["lots"].get(name) or INDEX_LOTS.get(name)


def lot_size(name: str) -> int:
    """`known_lot`, or 1 when it isn't known (backtests and the builder only;
    anything that sends an order uses `contract_lot` / `order_lot`)."""
    return known_lot(name) or 1


_FNO_SUFFIX = re.compile(r"\d{2}(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC|[1-9OND]\d{2})"
                         r"(?:FUT|\d+(?:\.\d+)?(?:CE|PE))$")


def underlying_of(symbol: str) -> Optional[str]:
    """The F&O name inside a Fyers derivative symbol, or None for anything
    that isn't an NSE/BSE future or option.

    NSE:NIFTY25O1424500CE (weekly), NSE:BANKNIFTY25OCT56000PE (monthly) and
    NSE:SBIN25OCTFUT all give the underlying (NIFTY, BANKNIFTY, SBIN)."""
    s = (symbol or "").strip().upper()
    if ":" not in s:
        return None
    exch, ticker = s.split(":", 1)
    if exch not in ("NSE", "BSE"):
        return None
    m = _FNO_SUFFIX.search(ticker)
    if not m or m.start() == 0:
        return None
    # Names that end in digits (NIFTYNXT50) can also parse with a shorter
    # name; prefer the longest prefix the master or the index table knows.
    known = master()["lots"]
    for k in range(len(ticker) - 1, 0, -1):
        head = ticker[:k]
        if (head in known or head in INDEX_LOTS) and _FNO_SUFFIX.fullmatch(ticker[k:]):
            return head
    return ticker[:m.start()]


def is_derivative(symbol: str) -> bool:
    return underlying_of(symbol) is not None


def contract_lot(symbol: str, now: Optional[float] = None) -> Optional[int]:
    """Exact lot of one NSE/BSE F&O contract, or None when it isn't known.

    The contract's own row in the scrip master first (matters while a lot
    revision is rolling through the expiries), else the underlying's lot."""
    s = (symbol or "").strip().upper()
    lot = (master().get("contracts") or {}).get(s)
    if lot:
        return int(lot)
    name = underlying_of(s)
    return known_lot(name, now) if name else None


_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def contract_expiry(symbol: str) -> Optional[int]:
    """When an NSE / BSE futures contract expires: 15:30 IST of its expiry day,
    from its row in the F&O master, else its month's last expiry weekday by the
    exchange's rule (an expired contract has left the master; a holiday that
    moved its expiry isn't known then). None for anything that isn't a future."""
    s = (symbol or "").strip().upper()
    name = underlying_of(s)
    if not name or not s.endswith("FUT"):
        return None
    for e, sym in master()["futures"].get(name, []):
        if sym == s:
            return _close_ts(datetime.fromtimestamp(e, IST).date())
    m = re.search(r"(\d{2})(" + "|".join(_MONTHS) + r")FUT$", s)
    if not m:
        return None
    year, month = 2000 + int(m[1]), _MONTHS.index(m[2]) + 1
    return _close_ts(_last_weekday(year, month, _expiry_weekday(s.split(":", 1)[0], date(year, month, 28))))


def future_symbol(name: str, which: str, now: Optional[float] = None) -> Optional[str]:
    now = now or time.time()
    live = [s for e, s in master()["futures"].get(name, []) if e >= now]
    idx = 0 if which == "current" else 1
    return live[idx] if len(live) > idx else None


def is_fno(name: str) -> bool:
    m = master()
    return name in m["lots"] or name in INDEX_LOTS


def master_loaded() -> bool:
    return bool(master()["lots"])


def lot_error(symbol: str, quantity: int) -> Optional[str]:
    """Why `quantity` can't be sent for `symbol`, or None when it can.

    F&O quantities must be whole lots or the exchange rejects the order. An
    NSE/BSE derivative whose lot isn't known (a stock option before the master
    is downloaded, or a contract listed after it) is refused rather than sent
    with a guessed lot of 1. Cash, MCX and currency symbols pass."""
    name = underlying_of(symbol)
    if name is None:
        return None
    lot = contract_lot(symbol)
    if lot is None:
        from app.services.instrument_master import get_master

        inst = get_master().get((symbol or "").strip().upper())
        if (inst is not None and inst.segment == "CD") or (len(name) == 6 and name.endswith("INR")):
            return None                # currency F&O (USDINR, EURUSD…): not in the equity F&O master
        why = ("isn't in the F&O scrip master" if master_loaded()
               else "is unknown — the NSE/BSE F&O scrip master isn't downloaded yet")
        return (f"lot size for {symbol} {why}, so the order wasn't sent "
                "(a guessed lot would be rejected by the exchange). Try again in a minute.")
    if lot > 1 and int(quantity) % lot:
        lots = int(quantity) / lot
        return (f"quantity {quantity} isn't a whole number of lots for {symbol} "
                f"(lot size {lot}; that's {lots:.2f} lots) — use a multiple of {lot}")
    return None


def freeze_qty(symbol: str) -> Optional[int]:
    """Max quantity per order for an index derivative, else None."""
    try:
        return INDEX_FREEZE.get(underlying_of(symbol) or "")
    except Exception:  # noqa: BLE001 — never block an order on a lookup
        return None
