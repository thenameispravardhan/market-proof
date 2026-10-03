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

MASTER_DIR = Path(__file__).resolve().parents[2] / "data" / "scrip_master"
FO_URLS = {"NSE_FO": "https://public.fyers.in/sym_details/NSE_FO.csv",
           "BSE_FO": "https://public.fyers.in/sym_details/BSE_FO.csv"}


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


def expiry_after(name: str, exch: str, kind: str, which: str, t: float) -> int:
    """Epoch of the contract's 15:30 close. `which` = current | next.

    ponytail: exchange holidays are not modelled — an expiry that fell on a
    holiday really moved a day earlier. Off by one day on a handful of dates.
    """
    days = [x for x in _expiries_from(name, exch, kind, datetime.fromtimestamp(t, IST).date())
            if _close_ts(x) >= t]
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
    atm = round(spot / step) * step
    n = int(steps or 0) if mode in ("ITM", "OTM") else 0
    itm_dir = -1 if right == "CE" else 1          # CE ITM = lower strikes
    sign = itm_dir if mode == "ITM" else -itm_dir
    return round(atm + sign * n * step, 2)


def strike_for_premium(spot: float, step: float, right: str, target: float, price_at) -> float:
    """The strike (ATM +/- 40 steps) whose premium is closest to `target`."""
    atm = round(spot / step) * step
    cands = [atm + k * step for k in range(-40, 41) if atm + k * step > 0]
    return min(cands, key=lambda k: abs(price_at(k) - target))


# ---- F&O master ----------------------------------------------------------------

_cache: dict[str, Any] = {"mtime": None, "data": {"lots": {}, "futures": {}, "strikes": {}}}


def master() -> dict[str, Any]:
    """{lots: name->lot, futures: name->[(expiry_ts, symbol)], strikes: name->{expiry_ts: [strikes]}}."""
    files = [MASTER_DIR / f"{k}.csv" for k in FO_URLS if (MASTER_DIR / f"{k}.csv").exists()]
    mtime = tuple(f.stat().st_mtime for f in files)
    if _cache["mtime"] == mtime:
        return _cache["data"]
    lots: dict[str, int] = {}
    futures: dict[str, list[tuple[int, str]]] = {}
    strikes: dict[str, dict[int, set[float]]] = {}
    for f in files:
        with f.open(encoding="utf-8", errors="replace") as fh:
            for row in csv.reader(fh):
                if len(row) < 17 or ":" not in row[9]:
                    continue
                name, kind = row[13].strip().upper(), row[16].strip().upper()
                try:
                    exp, lot = int(float(row[8])), int(float(row[3]))
                except ValueError:
                    continue
                lots.setdefault(name, lot)
                if kind in ("CE", "PE"):
                    try:
                        strikes.setdefault(name, {}).setdefault(exp, set()).add(float(row[15]))
                    except ValueError:
                        pass
                elif row[9].strip().upper().endswith("FUT"):
                    futures.setdefault(name, []).append((exp, row[9].strip().upper()))
    data = {"lots": lots,
            "futures": {k: sorted(v) for k, v in futures.items()},
            "strikes": {k: {e: sorted(s) for e, s in v.items()} for k, v in strikes.items()}}
    _cache.update(mtime=mtime, data=data)
    log.info("fno.master_loaded", underlyings=len(lots), files=len(files))
    return data


async def ensure_master(max_age_h: float = 20.0) -> None:
    """Download the F&O masters when missing or older than a day, then reload
    the app-wide instrument master too (futures become searchable)."""
    import httpx

    stale = [k for k in FO_URLS if not (MASTER_DIR / f"{k}.csv").exists()
             or time.time() - (MASTER_DIR / f"{k}.csv").stat().st_mtime > max_age_h * 3600]
    if not stale:
        return
    MASTER_DIR.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        for k in stale:
            try:
                r = await client.get(FO_URLS[k])
                r.raise_for_status()
                tmp = MASTER_DIR / f"{k}.csv.tmp"
                tmp.write_text(r.text, encoding="utf-8")
                tmp.replace(MASTER_DIR / f"{k}.csv")
            except Exception as e:  # noqa: BLE001 — a stale master beats none
                log.warning("fno.master_download_failed", file=k, error=str(e)[:200])
    from app.services.instrument_master import get_master

    get_master().reload()


def lot_size(name: str) -> int:
    return master()["lots"].get(name) or INDEX_LOTS.get(name) or 1


def future_symbol(name: str, which: str, now: Optional[float] = None) -> Optional[str]:
    now = now or time.time()
    live = [s for e, s in master()["futures"].get(name, []) if e >= now]
    idx = 0 if which == "current" else 1
    return live[idx] if len(live) > idx else None


def is_fno(name: str) -> bool:
    m = master()
    return name in m["lots"] or name in INDEX_LOTS
