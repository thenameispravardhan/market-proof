"""Which symbols the order-flow collector records: a budgeted universe that rolls by itself.

The Fyers data socket allows 5,000 symbols per user on the Prime plan (100 on
Standard) and every symbol costs CPU on a small server, so the universe is
built in priority order and cut at a budget:

   1. focus       hand-picked symbols (Algo Lab > Data) + switched-on strategies' symbols
                  (+ the research short list's, when `long` is on)
   1b. chain      option chains (`chains`, off by default): for each chain underlying the
                  underlying, its current future and ATM +/- `strikes` for the first
                  `expiries` expiries — ahead of the broad universe, so the budget never
                  cuts a strategy's chain first
   2. index       the indices themselves (their price centres the option strikes)
   3. index_fut   index futures: current and next month
   4. index_opt   index options, current expiry, ATM +/- index_strikes
   5. stock_fut   the universe's stock futures, current month
   6. stocks      NIFTY 500 / Midcap 150 / Smallcap 250 / Microcap 250 constituents, largest first
   7. index_opt2  index options, next expiry, ATM +/- index_next_strikes
   8. fut_next    stock futures, next month (always, or in the last roll_days before expiry)
   9. stock_opt   stock options, current expiry: the widest ATM +/- k (k <= stock_strikes) that
                  fits every stock, then one more strike each way for the largest while room is left
  10. stock_opt2  stock options, next expiry, in the roll window, ATM +/- 2
  11. sticky      strikes picked earlier today that are still near the money

Expiries and strikes come from the F&O scrip master (refreshed daily), so a new
weekly listing is recorded as soon as it is the current / next expiry, an expired
contract drops out on its own and a rolled future moves to the new month.
Strikes centre on the underlying's last price and re-centre every few minutes;
those picked earlier in the day stay while they are still near the money, so
an option's flow doesn't stop the moment the market drifts a strike away.
"""
from __future__ import annotations

import csv
import io
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from app.algo import fno
from app.algo.ticks import IST, flow_key
from app.logging_config import get_logger

log = get_logger(__name__)

ROOT_DATA = Path(__file__).resolve().parents[2] / "data"
MASTER_DIR = ROOT_DATA / "scrip_master"
IST_TZ = timezone(timedelta(seconds=IST))
LIST_URL = "https://www.niftyindices.com/IndexConstituent/{file}"
LIST_MAX_AGE_S = 7 * 86400
# NSE has no "Midcap 500" / "Smallcap 500" constituent file: Midcap 150 is ranks
# 101-250, Smallcap 250 ranks 251-500 and Microcap 250 ranks 501-750 by market cap,
# so NIFTY 500 + Smallcap 250 + Microcap 250 is the top 750 (NIFTY Total Market's core).
LISTS: dict[str, dict[str, Any]] = {
    "nifty500": {"label": "NIFTY 500", "file": "ind_nifty500list.csv", "ranks": (1, 500)},
    "midcap150": {"label": "NIFTY Midcap 150", "file": "ind_niftymidcap150list.csv", "ranks": (101, 250)},
    "smallcap250": {"label": "NIFTY Smallcap 250", "file": "ind_niftysmallcap250list.csv", "ranks": (251, 500)},
    "microcap250": {"label": "NIFTY Microcap 250", "file": "ind_niftymicrocap250_list.csv", "ranks": (501, 750)},
}
INDEX_SYMBOL = {name: sym for sym, (name, _, _) in fno.INDICES.items()}     # NIFTY -> NSE:NIFTY50-INDEX
CLASSES = ("focus", "chain", "index", "index_fut", "index_opt", "stock_fut", "stocks", "index_opt2", "fut_next",
           "stock_opt", "stock_opt2", "sticky")


def _ist_date(ts: float) -> int:
    return int((ts + IST) // 86400)


# ---- the F&O contracts (scrip master) ----------------------------------------

_FO: dict[str, Any] = {"mtime": None, "data": {}}


def contracts(master_dir: Path = MASTER_DIR) -> dict[str, dict[str, Any]]:
    """{name: {"exch", "fut": [(expiry, symbol)], "opt": {expiry: {"CE": {strike: symbol}, "PE": {...}}}}}
    from the NSE / BSE F&O masters; cached until the files change."""
    files = [master_dir / f"{k}.csv" for k in ("NSE_FO", "BSE_FO") if (master_dir / f"{k}.csv").exists()]
    mtime = tuple(f.stat().st_mtime for f in files)
    if _FO["mtime"] == mtime:
        return _FO["data"]
    out: dict[str, dict[str, Any]] = {}
    home: dict[str, str] = {}
    floor = time.time() - 2 * 86400
    for f in files:
        with f.open(encoding="utf-8", errors="replace") as fh:
            for row in csv.reader(fh):
                if len(row) < 17 or ":" not in row[9]:
                    continue
                sym = row[9].strip().upper()
                name, kind = row[13].strip().upper(), row[16].strip().upper()
                exch = sym.split(":", 1)[0]
                if home.setdefault(name, exch) != exch:
                    continue                                   # BSE's thin copy of an NSE contract
                try:
                    exp = int(float(row[8]))
                except ValueError:
                    continue
                if exp < floor:
                    continue
                c = out.setdefault(name, {"exch": exch, "fut": [], "opt": {}})
                if kind in ("CE", "PE"):
                    try:
                        k = float(row[15])
                    except ValueError:
                        continue
                    c["opt"].setdefault(exp, {"CE": {}, "PE": {}})[kind][k] = sym
                elif sym.endswith("FUT"):
                    c["fut"].append((exp, sym))
    for c in out.values():
        c["fut"].sort()
    _FO.update(mtime=mtime, data=out)
    return out


_CACHE: dict[str, tuple[Any, Any]] = {}


def _cached(name: str, path: Path, load: Callable[[], Any]) -> Any:
    """`load()`'s result until `path` changes."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    hit = _CACHE.get(name)
    if hit is None or hit[0] != mtime:
        hit = _CACHE[name] = (mtime, load())
    return hit[1]


def equities(master_dir: Path = MASTER_DIR) -> set[str]:
    """NSE cash symbols the scrip master lists (empty when it isn't downloaded)."""
    p = master_dir / "NSE_CM.csv"

    def load() -> set[str]:
        out: set[str] = set()
        if not p.exists():
            return out
        with p.open(encoding="utf-8", errors="replace") as fh:
            for row in csv.reader(fh):
                if len(row) > 9 and row[9].upper().endswith("-EQ"):
                    out.add(row[9].strip().upper())
        return out

    return _cached("equities", p, load)


# ---- index constituents --------------------------------------------------------

def _mcap_rank(data_dir: Path = ROOT_DATA) -> list[str]:
    """NSE tickers by AMFI average market cap, largest first (data/mcap.csv)."""
    path = data_dir / "mcap.csv"

    def load() -> list[str]:
        rows = []
        try:
            with path.open(encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    try:
                        rows.append((float(r.get("mcap_cr") or 0), r["symbol"].strip().upper()))
                    except (KeyError, ValueError):
                        continue
        except OSError:
            return []
        rows.sort(reverse=True)
        return list(dict.fromkeys(s for _, s in rows))

    return _cached(f"mcap:{path}", path, load)


def _parse_list(text: str) -> list[str]:
    rdr = csv.DictReader(io.StringIO(text))
    out = []
    for r in rdr:
        sym = next((v for k, v in r.items() if k and k.strip().lower() == "symbol"), None)
        series = next((v for k, v in r.items() if k and k.strip().lower() == "series"), "EQ")
        if sym and (series or "EQ").strip().upper() in ("EQ", "BE", "BZ", ""):
            out.append(sym.strip().upper())
    return out


def index_list(name: str, cache_dir: Path, fetch: bool = True) -> tuple[list[str], str]:
    """(tickers, source) for one index: niftyindices.com's constituent file
    (cached a week), else the same rank band of AMFI market cap."""
    meta = LISTS[name]
    path = cache_dir / meta["file"]
    fresh = path.exists() and time.time() - path.stat().st_mtime < LIST_MAX_AGE_S
    if fetch and not fresh:
        try:
            import httpx

            r = httpx.get(LIST_URL.format(file=meta["file"]), timeout=20, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
                                   "Accept": "text/csv,*/*"})
            syms = _parse_list(r.text) if r.status_code == 200 else []
            if len(syms) >= 50:
                cache_dir.mkdir(parents=True, exist_ok=True)
                path.write_text(r.text, encoding="utf-8")
                from app.research import universe

                if name in universe.INDEXES:                 # a dated copy: the point-in-time universe's history
                    universe.record_snapshot(name, syms)
            else:
                log.warning("flow_universe.list_download_bad", index=name, status=r.status_code, rows=len(syms))
        except Exception as e:  # noqa: BLE001 — the cached file / market-cap ranks stand in
            log.warning("flow_universe.list_download_failed", index=name, error=str(e)[:200])
    if path.exists():
        syms = _parse_list(path.read_text(encoding="utf-8", errors="replace"))
        if len(syms) >= 50:
            return syms, "niftyindices"
    lo, hi = meta["ranks"]
    return _mcap_rank()[lo - 1:hi], "market-cap rank"


# ---- the plan ---------------------------------------------------------------------

def _expiries(c: dict[str, Any], today: int) -> list[int]:
    """Listed option expiries on or after today (one epoch per date)."""
    by_date: dict[int, int] = {}
    for e in c["opt"]:
        d = _ist_date(e)
        if d >= today:
            by_date.setdefault(d, e)
    return [by_date[d] for d in sorted(by_date)]


def _futs(c: dict[str, Any], today: int) -> list[tuple[int, str]]:
    return [(e, s) for e, s in c["fut"] if _ist_date(e) >= today]


def _band(strikes: list[float], spot: float, k: int) -> list[float]:
    if not strikes:
        return []
    i = min(range(len(strikes)), key=lambda j: abs(strikes[j] - spot))
    return strikes[max(0, i - k): i + k + 1]


def _fut_key(name: str, exch: str, which: int) -> str:
    base = name if exch == "NSE" else f"{exch}_{name}"
    return f"{base}-FUT" if which == 0 else f"{base}-FUT2"


class Plan:
    def __init__(self, budget: int) -> None:
        self.budget = max(0, int(budget))
        self.symbols: list[str] = []
        self.keys: dict[str, str] = {}
        self.meta: dict[str, dict[str, Any]] = {}
        self.classes: dict[str, int] = {c: 0 for c in CLASSES}
        self.wanted: dict[str, int] = {c: 0 for c in CLASSES}
        self.opt_sel: dict[str, list[float]] = {}           # "NAME|expiry" -> strikes picked (stickiness)
        self.no_price: list[str] = []
        self.lists: dict[str, dict[str, Any]] = {}
        self.chain: dict[str, str] = {}                       # key -> chain underlying
        self.chain_names: list[str] = []

    @property
    def room(self) -> int:
        return self.budget - len(self.symbols)

    def add(self, cls: str, full: str, key: str, meta: dict[str, Any]) -> bool:
        full = full.upper()
        if full in self.keys:
            return True
        self.wanted[cls] += 1
        if len(self.symbols) >= self.budget:
            return False
        self.symbols.append(full)
        self.keys[full] = key
        self.meta[key] = {"symbol": full, "cls": cls, **meta}
        self.classes[cls] += 1
        return True

    def flag(self, key: str, **flags: Any) -> None:
        m = self.meta.get(key)
        if m is not None:
            m.update({k: v for k, v in flags.items() if v})

    def summary(self) -> dict[str, Any]:
        return {"budget": self.budget, "used": len(self.symbols), "classes": self.classes, "wanted": self.wanted,
                "dropped": {c: self.wanted[c] - self.classes[c] for c in CLASSES if self.wanted[c] > self.classes[c]},
                "no_price": self.no_price[:50], "lists": self.lists,
                "chains": {"names": self.chain_names, "keys": len(self.chain)},
                "long": sum(1 for m in self.meta.values() if m.get("long")),
                "raw_flagged": sum(1 for m in self.meta.values() if m.get("raw"))}


def chain_name(symbol: str, fo: dict[str, dict[str, Any]]) -> Optional[str]:
    """The F&O underlying behind a strategy / hand-picked symbol, when it lists
    options: NSE:NIFTY50-INDEX, NIFTY:FUT, NIFTY, NSE:SBIN-EQ, NSE:SBIN26OCTFUT and
    an option contract all give their name; anything else None."""
    s = (symbol or "").strip().upper()
    if not s:
        return None
    if s in fno.INDICES:
        name: Optional[str] = fno.INDICES[s][0]
    elif s.endswith(":FUT"):
        name = s[:-4]
    elif ":" not in s:
        name = s
    else:
        name = fno.underlying_of(s) or (fno.fno_name(s) if s.endswith("-EQ") else None)
    c = fo.get(name or "")
    return name if c and c.get("opt") else None


def build(cfg: dict[str, Any], focus: list[str], spot: Callable[[str], Optional[float]], now: float,
          prev: Optional[dict[str, list[float]]] = None, fo: Optional[dict[str, dict]] = None,
          lists: Optional[dict[str, tuple[list[str], str]]] = None, eq: Optional[set[str]] = None,
          budget: Optional[int] = None, strat: Optional[list[str]] = None) -> Plan:
    """The prioritised symbol plan (see the module doc). `spot(full)` is the
    underlying's last price (None = unknown: its options wait for one).
    `strat`: the switched-on strategies' symbols (already in `focus`) — they
    pick chain underlyings and the research short list."""
    uni = cfg["universe"]
    chains = cfg.get("chains") or {}
    long_ = cfg.get("long") or {}
    plan = Plan(budget if budget is not None else cfg.get("budget", 4500))
    fo = fo if fo is not None else contracts()
    eq = eq if eq is not None else equities()
    today = _ist_date(now)
    roll_days = int(uni.get("roll_days") or 5)
    strat = [x.strip().upper() for x in strat or [] if x and x.strip()]

    # the research short list (long): its symbols are recorded as focus and flagged in the day's meta
    long_syms: list[str] = []
    if long_.get("enabled"):
        long_syms = list(dict.fromkeys([*(str(x).strip().upper() for x in long_.get("symbols") or []),
                                        *(strat if long_.get("strategies", True) else [])]))
        focus = [*focus, *long_syms]
    long_set = set(long_syms)
    long_names = {n for x in long_syms if (n := chain_name(x, fo))}
    raw_k = int(long_.get("raw_strikes") or 0) if long_.get("enabled") else -1

    def fut_meta(name: str, exp: int) -> dict[str, Any]:
        return {"kind": "FUT", "name": name, "expiry": exp}

    def options(cls: str, name: str, exp: int, k: int, underlying: str, chain: Optional[str] = None) -> int:
        c = fo[name]
        px = spot(underlying)
        if not px:
            plan.no_price.append(underlying)
            return 0
        opts = c["opt"].get(exp) or {}
        strikes = sorted(set(opts.get("CE", {})) | set(opts.get("PE", {})))
        picked = _band(strikes, px, k)
        near = set(_band(strikes, px, raw_k)) if chain and raw_k >= 0 and name in long_names else set()
        sel_key = f"{name}|{exp}"
        plan.opt_sel[sel_key] = sorted(set(plan.opt_sel.get(sel_key, [])) | set(picked))
        n = 0
        for strike in sorted(picked, key=lambda x: abs(x - px)):           # nearest the money first
            for right in ("CE", "PE"):
                sym = opts.get(right, {}).get(strike)
                if sym and plan.add(cls, sym, flow_key(sym), {"kind": right, "name": name, "expiry": exp,
                                                               "strike": strike}):
                    n += 1
                    if chain:
                        plan.chain[flow_key(sym)] = chain
                        plan.flag(flow_key(sym), chain=chain, long=name in long_names, raw=strike in near)
        return n

    # 1. focus: hand-picked + strategies. "NAME:FUT" is that future's current month; an index records its future too.
    for s in focus:
        s = s.strip().upper()
        if not s:
            continue
        is_long = s in long_set
        if s == "ALL:FUT":
            for name, c in sorted(fo.items()):
                f = _futs(c, today)
                if f:
                    plan.add("focus", f[0][1], _fut_key(name, c["exch"], 0), fut_meta(name, f[0][0]))
            continue
        if s.endswith(":FUT") or s in fno.INDICES:
            name = s.split(":")[0] if s.endswith(":FUT") else fno.INDICES[s][0]
            c = fo.get(name)
            f = _futs(c, today) if c else []
            if f:
                plan.add("focus", f[0][1], _fut_key(name, c["exch"], 0), fut_meta(name, f[0][0]))
                plan.flag(_fut_key(name, c["exch"], 0), long=is_long)
            if s in fno.INDICES:
                plan.add("focus", s, f"IDX_{name}", {"kind": "IDX", "name": name})
                plan.flag(f"IDX_{name}", long=is_long)
            continue
        if ":" in s:
            plan.add("focus", s, flow_key(s), {"kind": "EQ" if s.endswith("-EQ") else "OTHER"})
            plan.flag(plan.keys.get(s, flow_key(s)), long=is_long)

    # 1b. option chains: named underlyings + the strategies' (those that list options)
    if chains.get("enabled"):
        wanted = [str(x).strip().upper() for x in chains.get("underlyings") or []]
        if chains.get("strategies", True):
            wanted += strat
        names = list(dict.fromkeys(n for x in wanted if (n := chain_name(x, fo))))
        plan.chain_names = names
        k = max(0, int(chains.get("strikes", 10)))
        n_exp = max(1, int(chains.get("expiries", 2)))
        for name in names:
            c = fo[name]
            under = INDEX_SYMBOL.get(name) or f"{c['exch']}:{name}-EQ"
            ukey = f"IDX_{name}" if name in INDEX_SYMBOL else flow_key(under)
            is_long = name in long_names
            if plan.add("chain", under, ukey, {"kind": "IDX" if name in INDEX_SYMBOL else "EQ", "name": name}):
                plan.chain[ukey] = name
                plan.flag(ukey, chain=name, long=is_long)
            f = _futs(c, today)
            if f:
                fkey = _fut_key(name, c["exch"], 0)
                if plan.add("chain", f[0][1], fkey, fut_meta(name, f[0][0])):
                    plan.chain[fkey] = name
                    plan.flag(fkey, chain=name, long=is_long)
            for exp in _expiries(c, today)[:n_exp]:
                options("chain", name, exp, k, under, chain=name)

    if not uni.get("enabled", True):
        return plan

    idx_names = [n for n in ("NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "BANKEX") if n in fo]

    # 2-3. the indices and their futures
    for name in idx_names:
        plan.add("index", INDEX_SYMBOL[name], f"IDX_{name}", {"kind": "IDX", "name": name})
    if uni.get("index_futures", True):
        for name in idx_names:
            c = fo[name]
            f = _futs(c, today)
            for i, (exp, sym) in enumerate(f[:2] if uni.get("next_futures", "roll") != "off" else f[:1]):
                plan.add("index_fut", sym, _fut_key(name, c["exch"], i), fut_meta(name, exp))

    # 4. index options, current expiry
    if uni.get("index_options", True):
        for name in idx_names:
            exps = _expiries(fo[name], today)
            if exps:
                options("index_opt", name, exps[0], int(uni.get("index_strikes", 15)), INDEX_SYMBOL[name])

    # the stock universe, largest first
    stock_lists = lists if lists is not None else {}
    names: list[str] = []
    for lst in uni.get("lists") or []:
        if lst not in LISTS:
            continue
        syms, src = stock_lists.get(lst) or ([], "none")
        plan.lists[lst] = {"label": LISTS[lst]["label"], "n": len(syms), "source": src}
        names += syms
    rank = {s: i for i, s in enumerate(_mcap_rank())}
    names = sorted(dict.fromkeys(names), key=lambda s: rank.get(s, len(rank)))
    fno_stocks = [n for n in names if n in fo and fo[n]["fut"]]

    # 5. stock futures, current month
    if uni.get("stock_futures", True):
        for name in fno_stocks:
            f = _futs(fo[name], today)
            if f:
                plan.add("stock_fut", f[0][1], _fut_key(name, fo[name]["exch"], 0), fut_meta(name, f[0][0]))

    # 6. the stocks themselves
    if uni.get("stocks", True):
        for name in names:
            full = f"NSE:{name}-EQ"
            if not eq or full in eq:
                plan.add("stocks", full, name, {"kind": "EQ", "name": name})

    # 7. index options, next expiry
    if uni.get("index_options", True):
        for name in idx_names:
            exps = _expiries(fo[name], today)
            if len(exps) > 1:
                options("index_opt2", name, exps[1], int(uni.get("index_next_strikes", 10)), INDEX_SYMBOL[name])

    # 8. next-month stock futures
    mode = uni.get("next_futures", "roll")
    if uni.get("stock_futures", True) and mode != "off":
        for name in fno_stocks:
            f = _futs(fo[name], today)
            if len(f) > 1 and (mode == "always" or _ist_date(f[0][0]) - today <= roll_days):
                plan.add("fut_next", f[1][1], _fut_key(name, fo[name]["exch"], 1), fut_meta(name, f[1][0]))

    # 9. stock options, current expiry: the widest even band that fits, then extra strikes for the largest
    if uni.get("stock_options", True):
        stocks_px = [(n, px) for n in fno_stocks if (px := spot(f"NSE:{n}-EQ"))]
        plan.no_price += [f"NSE:{n}-EQ" for n in fno_stocks if not spot(f"NSE:{n}-EQ")]
        k_max = int(uni.get("stock_strikes", 5))
        k = -1
        for kk in range(k_max, -1, -1):
            if len(stocks_px) * 2 * (2 * kk + 1) <= plan.room:
                k = kk
                break
        if k < 0:                                   # not even the ATM pair for all: the largest get it
            for n, px in stocks_px:
                if plan.room < 2:
                    break
                exps = _expiries(fo[n], today)
                if exps:
                    options("stock_opt", n, exps[0], 0, f"NSE:{n}-EQ")
        else:
            for n, px in stocks_px:
                exps = _expiries(fo[n], today)
                if exps:
                    options("stock_opt", n, exps[0], k, f"NSE:{n}-EQ")
            ring = k + 1
            while ring <= k_max and plan.room >= 4:
                for n, px in stocks_px:
                    if plan.room < 4:
                        break
                    exps = _expiries(fo[n], today)
                    if exps:
                        options("stock_opt", n, exps[0], ring, f"NSE:{n}-EQ")
                ring += 1

    # 10. stock options, next expiry in the roll window
    if uni.get("stock_options", True) and uni.get("stock_next_expiry", "roll") != "off":
        for n in fno_stocks:
            exps = _expiries(fo[n], today)
            if len(exps) > 1 and _ist_date(exps[0]) - today <= roll_days and spot(f"NSE:{n}-EQ"):
                options("stock_opt2", n, exps[1], 2, f"NSE:{n}-EQ")

    # 11. sticky: strikes picked earlier today, still within 3 strikes of the band
    for sel_key, strikes in (prev or {}).items():
        name, _, exp_s = sel_key.partition("|")
        c = fo.get(name)
        if not c:
            continue
        exp = int(exp_s)
        chain = c["opt"].get(exp)
        if not chain or _ist_date(exp) < today:
            continue
        under = INDEX_SYMBOL.get(name) or f"NSE:{name}-EQ"
        px = spot(under)
        if not px:
            continue
        grid = sorted(set(chain.get("CE", {})) | set(chain.get("PE", {})))
        near = set(_band(grid, px, len(plan.opt_sel.get(sel_key, [])) // 2 + 3))
        for strike in strikes:
            if strike not in near:
                continue
            for right in ("CE", "PE"):
                sym = chain.get(right, {}).get(strike)
                if sym and plan.add("sticky", sym, flow_key(sym), {"kind": right, "name": name, "expiry": exp,
                                                                     "strike": strike}) and name in plan.chain_names:
                    plan.chain[flow_key(sym)] = name                   # an earlier strike of a recorded chain
                    plan.flag(flow_key(sym), chain=name, long=name in long_names)
            plan.opt_sel.setdefault(sel_key, [])
            if strike not in plan.opt_sel[sel_key]:
                plan.opt_sel[sel_key].append(strike)
    plan.no_price = list(dict.fromkeys(plan.no_price))
    return plan


def session_label(now: float) -> str:
    return datetime.fromtimestamp(now, IST_TZ).strftime("%Y-%m-%d %H:%M")
