"""Real order flow from the Fyers data socket: classify every frame, keep 90 days.

Fyers keeps no tick history, so the only way to get REAL order flow is to
record it as it happens. Every SymbolUpdate frame is a snapshot: LTP,
cumulative day volume, last traded qty, best bid / ask (+ sizes), the
book's total buy / sell quantity and, for F&O, open interest. Each increase
in cumulative volume is volume that traded since the previous frame; its
side is classified with the Lee-Ready rule against the quote that was
standing BEFORE the trade:

    price >= previous ask  -> buyer-initiated
    price <= previous bid  -> seller-initiated
    otherwise              -> tick test: up-tick buy, down-tick sell,
                              zero-tick keeps the last side

Volume that can't be pinned on one print is kept apart as `unk` — counted in
the bar's volume, never in its delta or footprint:
  * the first frame of a symbol after a (re)connect or a restart: everything
    traded while the feed was down arrives as one jump in cumulative volume,
    and booking it as one aggressive print at one price drew fake spikes;
  * NSE / BSE trades outside 09:15-15:30 (the pre-open call auction and the
    closing session match at one price: there is no aggressor).

Storage, under data/ticks/ (one writer: the collector process, flowd.py):
    today/<YYYYMMDD>/flow/<KEY>.csv   minute rows, appended every few seconds (last row per minute wins)
    today/<YYYYMMDD>/fp/<KEY>.csv     footprint rows (minute, price) — last row per level wins
    today/<YYYYMMDD>/prints.csv       big prints (every key)
    today/<YYYYMMDD>/raw/<KEY>.csv    every frame — focus symbols only
    today/<YYYYMMDD>/chain.csv        option-chain snapshots (the `chains` setting)
    today/<YYYYMMDD>/meta.json        key -> contract (symbol, kind, expiry, strike; chain / long / raw flags)
    days/<YYYYMMDD>/flow.parquet, fp.parquet, prints.parquet, raw.parquet, chain.parquet, meta.json
A day is compacted after the close (or at the next start). Days are a
sliding window: with `retention_days` (default 90) collected days kept, the
oldest is deleted as each new one lands. Raw frames keep `raw_days`.

Option chains (`chains`, off by default): for each chain underlying (named
ones + those of switched-on strategies) the planner records the underlying,
its current future and ATM +/- `strikes` for the first `expiries` expiries,
and the recorder writes the standing quote of every such contract (bid / ask
/ sizes / last / OI / volume and the quote's own exchange time) on a fixed
exchange-time grid (`interval_s`). A snapshot is taken on the socket thread
by the first frame stamped at or after the grid time, before that frame is
applied, so a snapshot at T holds only quotes from before T — what a
backtest could have known at T.

The research short list (`long`, off by default): strategy symbols (and
hand-picked ones) are flagged `long` in the day's meta when recorded — with
their chains' contracts. Past `retention_days` a day is trimmed to the
flagged keys instead of deleted and kept to `long.days`; their raw frames
(the near-the-money chain contracts' too, `long.raw_strikes`) keep to
`long.raw_days`.

Keys: a stock is its ticker (SBIN), an option its contract (NIFTY26O2725000CE),
the current future CONTINUOUS (NIFTY-FUT, rolls with expiry) and the next
month NIFTY-FUT2; days/<d>/meta.json says which contract each key held.
"""
from __future__ import annotations

import calendar
import csv
import json
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from app.logging_config import get_logger

log = get_logger(__name__)

ROOT = Path(__file__).resolve().parents[2] / "data" / "ticks"
IST = 19800
DAY = 86400
OPEN_S, CLOSE_S = 9 * 3600 + 15 * 60, 15 * 3600 + 30 * 60
COMPACT_AFTER_MIN = 16 * 60 + 10           # after the closing session's last frames
RETENTION_DAYS = 90
RAW_DAYS = 5
MIN_FREE_GB = 5.0                          # the disk guard: older days go first, then recording pauses
MAX_FOCUS = 300                            # hand-picked symbols (the universe planner adds the rest)
DEFAULT_BUDGET = 4500                      # Fyers Prime: 5,000 symbols per user — room left for the app's own feed
PRINT_MULT, PRINT_WARMUP = 8.0, 30         # a print >= 8x the symbol's average print is a "big trade"
CONFIG_VERSION = 3
# By default only NIFTY and SENSEX are recorded: each index, its current future (an
# index has no trades of its own — the future's flow is the index chart's) and its
# options — ATM first, then every listed strike of every expiry while the budget lasts.
# Stocks, stock F&O and the other indices are off until switched on (Algo Lab > Data).
DEFAULT_SYMBOLS = ["NSE:NIFTY50-INDEX", "BSE:SENSEX-INDEX"]
V2_DEFAULT_SYMBOLS = ["NIFTY:FUT", "BANKNIFTY:FUT", "NSE:RELIANCE-EQ", "NSE:HDFCBANK-EQ", "NSE:ICICIBANK-EQ",
                      "NSE:SBIN-EQ", "NSE:INFY-EQ", "NSE:TCS-EQ", "NSE:AXISBANK-EQ", "NSE:KOTAKBANK-EQ",
                      "NSE:LT-EQ", "NSE:ITC-EQ", "NSE:BHARTIARTL-EQ", "NSE:BAJFINANCE-EQ"]
INDEX_NAMES = ("NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "BANKEX")
DEFAULT_UNIVERSE: dict[str, Any] = {
    "enabled": True,
    "indices": ["NIFTY", "SENSEX"],        # which of INDEX_NAMES: the index, its futures and options
    "lists": [],                           # stock universes (flow_universe.LISTS)
    "stocks": False,
    "index_futures": True,
    "stock_futures": False,
    "next_futures": "off",                 # always | roll (last roll_days before expiry) | off
    "index_options": True,
    "index_strikes": 15,                   # ATM +/- N strikes, current expiry
    "index_next_strikes": 10,              # ... next expiry
    "index_all": True,                     # then every other listed strike / expiry, nearest first, to the budget
    "stock_options": False,
    "stock_strikes": 5,                    # the most a stock gets; narrowed to fit the budget
    "stock_next_expiry": "roll",           # roll | off
    "roll_days": 5,
}
# A v2 config (the broad stock universe) moves to the index-only scope once; the
# user's own choices saved after that (version 3) stand.
V3_SCOPE = {k: DEFAULT_UNIVERSE[k] for k in ("indices", "lists", "stocks", "stock_futures", "next_futures",
                                             "stock_options", "index_all")}
# Option-chain snapshots for research (IV surface, tick replay): off unless switched on.
DEFAULT_CHAINS: dict[str, Any] = {
    "enabled": False,
    "underlyings": [],                     # F&O names (NIFTY, BANKNIFTY, SBIN ...)
    "strategies": True,                    # + the underlyings of switched-on strategies
    "strikes": 10,                         # ATM +/- N strikes
    "expiries": 2,                         # the first N listed expiries
    "interval_s": 60,                      # the snapshot grid, exchange time
}
# The research short list: kept longer than the sliding window. Off unless switched on.
DEFAULT_LONG: dict[str, Any] = {
    "enabled": False,
    "symbols": [],                         # hand-picked (as in `symbols`)
    "strategies": True,                    # + switched-on strategies' symbols
    "days": 365,                           # flow / footprint / prints / chains of the list
    "raw_days": 120,                       # every frame of the list
    "raw_strikes": 3,                      # + raw frames of chain contracts within ATM +/- N
}
META_FLAGS = ("chain", "long", "raw")
SORT_ORDER = {"flow.parquet": "key, minute", "fp.parquet": "key, minute, price", "prints.parquet": "key, ts",
              "raw.parquet": "key, exch_ts", "chain.parquet": "key, ts"}
CHAIN_COLS = ("ts", "qts", "ltp", "bid", "ask", "bid_size", "ask_size", "oi", "vol")
SNAP_MIN_S = 15
FLOW_COLS = ("minute", "buy", "sell", "unk", "trades", "ticks", "o", "h", "l", "c", "tot_buy", "tot_sell", "oi",
             "cumvol")
FLOW_TYPES = ("BIGINT", "DOUBLE", "DOUBLE", "DOUBLE", "INTEGER", "INTEGER", "DOUBLE", "DOUBLE", "DOUBLE", "DOUBLE",
              "DOUBLE", "DOUBLE", "DOUBLE", "DOUBLE")
FP_COLS = ("minute", "price", "buy", "sell")
PRINT_COLS = ("ts", "price", "qty", "side")
RAW_COLS = ("recv_ts", "exch_ts", "ltp", "vol", "dv", "side", "bid", "ask", "bid_size", "ask_size",
            "tot_buy", "tot_sell", "ltq", "oi")
_FUT = re.compile(r"^(?P<ex>[A-Z]+):(?P<name>[A-Z0-9&-]+?)\d{2}[A-Z]{3}FUT$")


def flow_key(symbol: str) -> str:
    """Storage key: NSE:SBIN-EQ -> SBIN, NSE:NIFTY26OCTFUT -> NIFTY-FUT,
    BSE:SENSEX26OCTFUT -> BSE_SENSEX-FUT, NSE:NIFTY50-INDEX -> NIFTY-FUT (an
    index has no trades of its own; its future's flow stands in), an option
    -> its contract (NIFTY26O2725000CE)."""
    s = symbol.upper()
    m = _FUT.match(s)
    if m:
        return m["name"] + "-FUT" if m["ex"] == "NSE" else f"{m['ex']}_{m['name']}-FUT"
    from app.algo import fno

    if s in fno.INDICES:
        name, _, ex = fno.INDICES[s]
        return name + "-FUT" if ex == "NSE" else f"{ex}_{name}-FUT"
    ex, _, name = s.partition(":")
    if ex == "NSE":
        return name[:-3] if name.endswith("-EQ") else name
    return f"{ex}_{name}"


def _f(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def ist_day(ts: float) -> int:
    return int((ts + IST) // DAY)


def ymd(day: int) -> str:
    return time.strftime("%Y%m%d", time.gmtime(day * DAY))


def day_of(name: str) -> Optional[int]:
    try:
        return calendar.timegm(time.strptime(name, "%Y%m%d")) // DAY
    except ValueError:
        return None


def _num(x: str) -> Optional[float]:
    if x == "" or x is None:
        return None
    try:
        return float(x)
    except ValueError:
        return None


# ---- config (shared by the app and the collector) -----------------------------

def load_config(root: Path = ROOT) -> dict[str, Any]:
    try:
        raw = json.loads((root / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    cfg: dict[str, Any] = {"version": CONFIG_VERSION, "enabled": True, "symbols": list(DEFAULT_SYMBOLS),
                           "retention_days": RETENTION_DAYS, "raw_days": RAW_DAYS, "budget": DEFAULT_BUDGET,
                           "min_free_gb": MIN_FREE_GB}
    cfg.update({k: v for k, v in raw.items() if k != "universe"})
    cfg["universe"] = {**DEFAULT_UNIVERSE, **(raw.get("universe") or {})}
    cfg["chains"] = {**DEFAULT_CHAINS, **(raw.get("chains") or {})}
    cfg["long"] = {**DEFAULT_LONG, **(raw.get("long") or {})}
    version = int(raw.get("version") or 1) if raw else CONFIG_VERSION
    if version < 2:                             # a v1 file: the 30-day default predates the 90-day window
        cfg["retention_days"] = max(int(raw.get("retention_days") or 0), RETENTION_DAYS)
    if version < 3:                             # the stock-universe era: to NIFTY / SENSEX only
        cfg["universe"].update(V3_SCOPE)
        if [str(x).upper() for x in cfg.get("symbols") or []] == V2_DEFAULT_SYMBOLS:
            cfg["symbols"] = list(DEFAULT_SYMBOLS)
    cfg["version"] = CONFIG_VERSION
    return cfg


def save_config(patch: dict[str, Any], root: Path = ROOT) -> dict[str, Any]:
    """Merge `patch` into the stored config (validated by the API)."""
    cfg = load_config(root)
    for k, v in patch.items():
        if k == "universe" and isinstance(v, dict):
            cfg["universe"] = {**cfg["universe"], **{kk: vv for kk, vv in v.items() if kk in DEFAULT_UNIVERSE}}
        elif k == "chains" and isinstance(v, dict):
            cfg["chains"] = {**cfg["chains"], **{kk: vv for kk, vv in v.items() if kk in DEFAULT_CHAINS}}
        elif k == "long" and isinstance(v, dict):
            cfg["long"] = {**cfg["long"], **{kk: vv for kk, vv in v.items() if kk in DEFAULT_LONG}}
        elif k in ("enabled", "symbols", "retention_days", "raw_days", "budget", "min_free_gb"):
            cfg[k] = v
    cfg["symbols"] = list(dict.fromkeys(str(s).strip().upper() for s in cfg["symbols"] if str(s).strip()))[:MAX_FOCUS]
    cfg["chains"]["underlyings"] = list(dict.fromkeys(
        str(s).strip().upper() for s in cfg["chains"].get("underlyings") or [] if str(s).strip()))[:MAX_FOCUS]
    cfg["long"]["symbols"] = list(dict.fromkeys(
        str(s).strip().upper() for s in cfg["long"].get("symbols") or [] if str(s).strip()))[:MAX_FOCUS]
    cfg["version"] = CONFIG_VERSION
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / "config.json.tmp"
    tmp.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    tmp.replace(root / "config.json")
    return cfg


_STRAT: dict[str, Any] = {"syms": None}


def set_strategy_symbols(symbols: Iterable[str], root: Path = ROOT) -> None:
    """Symbols of switched-on strategies (the app's runner): the collector
    records them like hand-picked ones. Written only when they change."""
    syms = sorted({s.strip().upper() for s in symbols if s and s.strip()})
    if syms == _STRAT["syms"]:
        return
    _STRAT["syms"] = syms
    if root == ROOT and os.environ.get("TESTING") == "1":
        return                                     # the test suite never writes into the real data folder
    try:
        root.mkdir(parents=True, exist_ok=True)
        tmp = root / "strategy_symbols.json.tmp"
        tmp.write_text(json.dumps(syms), encoding="utf-8")
        tmp.replace(root / "strategy_symbols.json")
    except OSError as e:
        log.warning("ticks.strategy_symbols_write_failed", error=str(e)[:200])


def strategy_symbols(root: Path = ROOT) -> list[str]:
    try:
        v = json.loads((root / "strategy_symbols.json").read_text(encoding="utf-8"))
        return [str(x) for x in v] if isinstance(v, list) else []
    except (OSError, ValueError):
        return []


def merge_meta(first: dict[str, dict], later: dict[str, dict]) -> dict[str, dict]:
    """A day's key -> contract map: a key keeps the contract it held first;
    the research flags (chain, long, raw) accumulate from either side."""
    out = {k: dict(v) for k, v in first.items()}
    for k, v in later.items():
        cur = out.get(k)
        if cur is None:
            out[k] = dict(v)
            continue
        for flag in META_FLAGS:
            if v.get(flag) and not cur.get(flag):
                cur[flag] = v[flag]
    return out


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


# ---- writer: the collector's recorder -------------------------------------------

class TickRecorder:
    """Classifies frames (on the socket thread), keeps the minutes still
    forming in memory and appends them to today's files every few seconds."""

    def __init__(self, root: Path = ROOT) -> None:
        self.root = root
        self._lock = threading.Lock()
        self._state: dict[str, dict[str, Any]] = {}            # full symbol -> classification state
        self._min: dict[str, dict[int, list]] = {}              # key -> minute -> aggregate (recent minutes)
        self._fp: dict[str, dict[int, dict[float, list[float]]]] = {}
        self._floor: dict[str, int] = {}                        # key -> oldest minute still held (older = final)
        self._dirty: set[tuple[str, int]] = set()
        self._prints: list[tuple] = []
        self._raw: dict[str, list[tuple]] = {}
        self._gen = 0                                            # bumped on every (re)connect
        self._resume_vol: dict[str, float] = {}                  # key -> cumulative volume flushed before a restart
        self.key_of: dict[str, str] = {}                         # full symbol -> key (the plan's)
        self.raw_keys: set[str] = set()                          # keys whose raw frames are kept
        self.ltp: dict[str, float] = {}                          # full symbol -> last price (the planner's spot)
        self.stats: dict[str, list[float]] = {}                  # key -> [ticks, trades, buy, sell, unk, last_ts]
        self.frames = 0
        self.lag: list[float] = []                               # recent receive - exchange delays (s)
        # option chains: written only on the socket thread, read by flush() under the lock
        self.chain_keys: dict[str, str] = {}                     # key -> chain underlying (the plan's)
        self.snap_s = int(DEFAULT_CHAINS["interval_s"])
        self._quote: dict[str, tuple] = {}                       # key -> (qts, ltp, bid, ask, bsz, asz, oi, vol)
        self._next_snap = 0                                      # exchange time of the next snapshot
        self._last_snap = 0
        self._snaps: list[tuple] = []                            # (key, ts, *quote) awaiting the next flush
        self.snapshots = 0

    # -- hot path (socket thread) ----------------------------------------------

    def set_chain(self, keys: dict[str, str], interval_s: int) -> None:
        """The plan's chain contracts (key -> underlying); quotes of keys that
        left the chain are dropped. Called from the collector's loop."""
        self.chain_keys = dict(keys)
        self.snap_s = max(SNAP_MIN_S, int(interval_s or DEFAULT_CHAINS["interval_s"]))
        # in place, not a new dict: the socket thread may be storing a chain key's quote right now
        for k in [k for k in list(self._quote) if k not in self.chain_keys]:
            self._quote.pop(k, None)

    def mark_resync(self) -> None:
        """A (re)connect: the next frame of every symbol only re-anchors its
        cumulative volume — what traded meanwhile is `unk`, not a print."""
        self._gen += 1

    def on_tick(self, full: str, msg: dict[str, Any], recv_ts: Optional[float] = None) -> None:
        ltp = _f(msg.get("ltp"))
        if ltp <= 0:
            return
        full = full.upper()
        self.ltp[full] = ltp
        recv_ts = recv_ts or time.time()
        if self.chain_keys:
            qts = int(_f(msg.get("exch_feed_time")) or _f(msg.get("last_traded_time")) or recv_ts)
            if qts >= self._next_snap:
                self._snap(qts, recv_ts)          # the standing quotes, before this frame moves any
            key = self.key_of.get(full)
            if key is not None and key in self.chain_keys:
                self._quote[key] = (qts, ltp, _f(msg.get("bid_price")) or None, _f(msg.get("ask_price")) or None,
                                    _f(msg.get("bid_size")), _f(msg.get("ask_size")),
                                    _f(msg.get("OI")) if msg.get("OI") is not None else None,
                                    _f(msg.get("vol_traded_today")) if msg.get("vol_traded_today") is not None
                                    else None)
        if msg.get("vol_traded_today") is None:
            return                                # an index: a price, no trades
        exch_ts = int(_f(msg.get("exch_feed_time")) or _f(msg.get("last_traded_time")) or recv_ts)
        if ist_day(exch_ts) != ist_day(recv_ts):
            return                                # the subscribe snapshot of an earlier session
        row = (round(recv_ts, 3), exch_ts, ltp, _f(msg.get("vol_traded_today")), _f(msg.get("bid_price")),
               _f(msg.get("ask_price")), _f(msg.get("bid_size")), _f(msg.get("ask_size")),
               _f(msg.get("tot_buy_qty")), _f(msg.get("tot_sell_qty")), _f(msg.get("last_traded_qty")),
               _f(msg.get("OI")) if msg.get("OI") is not None else None)
        with self._lock:
            self._classify(full, row)

    def _snap(self, t: int, recv_ts: float) -> None:
        """Exchange time reached the next grid point: queue every chain key's
        standing quote stamped at that point (the grid inside 09:15-15:30 IST,
        the 15:30 close always included). Quotes from an earlier session are
        left out — they say nothing about today's market."""
        if ist_day(t) != ist_day(recv_ts) or t > recv_ts + 60:
            return                                # an earlier session's snapshot, or a clock glitch
        iv = max(SNAP_MIN_S, int(self.snap_s))
        day0 = t - (t + IST) % DAY
        lo, hi = day0 + OPEN_S, day0 + CLOSE_S
        g = t - (t - lo) % iv if t >= lo else lo
        if t < lo:
            self._next_snap = lo
            return
        g = min(g, hi)
        if g <= self._last_snap:                  # this point is done (the close, or a late start's skip)
            self._next_snap = g + iv if g < hi else day0 + DAY + OPEN_S
            return
        if self._next_snap == 0:                  # a fresh start: the quotes are still arriving, skip a point
            self._last_snap = g
            self._next_snap = min(g + iv, hi) if g < hi else day0 + DAY + OPEN_S
            return
        day = ist_day(g)
        keys = self.chain_keys
        rows = [(k, g, *q) for k, q in list(self._quote.items()) if k in keys and q[0] < g and ist_day(q[0]) == day]
        if rows:
            with self._lock:
                self._snaps.extend(rows)
            self.snapshots += 1
        self._last_snap = g
        self._next_snap = min(g + iv, hi) if g < hi else day0 + DAY + OPEN_S

    def _classify(self, full: str, row: tuple, key: Optional[str] = None, exchange_hours: Optional[bool] = None) -> None:
        recv_ts, exch_ts, ltp, vol, bid, ask, bsz, asz, tbq, tsq, ltq, oi = row
        key = key or self.key_of.get(full) or flow_key(full)
        day = ist_day(exch_ts)
        st = self._state.get(full)
        if st is None or st["day"] != day:
            resume = self._resume_vol.pop(key, None) if st is None else None
            st = self._state[full] = {"day": day, "vol": resume, "bid": 0.0, "ask": 0.0, "ltp": 0.0, "side": 0,
                                      "gen": self._gen, "ema": 0.0, "n": 0}
            fresh = resume is not None            # after a restart: the gap's volume is unk
        else:
            fresh = st["gen"] != self._gen
        st["gen"] = self._gen
        dv = vol - st["vol"] if st["vol"] is not None and vol >= st["vol"] else 0.0
        tod = (exch_ts + IST) % DAY
        if exchange_hours is None:
            exchange_hours = full.startswith(("NSE:", "BSE:"))
        session = not exchange_hours or OPEN_S <= tod < CLOSE_S
        unk = dv if dv > 0 and (fresh or not session) else 0.0
        q = dv - unk
        side = 0
        if q > 0:
            if st["ask"] and ltp >= st["ask"]:
                side = 1
            elif st["bid"] and ltp <= st["bid"]:
                side = -1
            elif st["ltp"] and ltp != st["ltp"]:
                side = 1 if ltp > st["ltp"] else -1
            else:
                side = st["side"]
            st["side"] = side or st["side"]
        if st["vol"] is None or vol >= st["vol"]:
            st["vol"] = vol                       # 0 before the first trade anchors too; a glitch never moves it back
        st["bid"], st["ask"], st["ltp"] = bid or st["bid"], ask or st["ask"], ltp
        minute = max(exch_ts - exch_ts % 60, self._floor.get(key, 0))   # a late frame never reopens a written minute
        mins = self._min.setdefault(key, {})
        a = mins.get(minute)
        if a is None:
            a = mins[minute] = [0.0, 0.0, 0.0, 0, 0, None, None, None, None, 0.0, 0.0, None, 0.0]
        buy = q if side > 0 else q / 2 if side == 0 else 0.0
        a[0] += buy
        a[1] += q - buy
        a[2] += unk
        a[3] += 1 if q > 0 else 0
        a[4] += 1
        if dv > 0:
            a[5] = ltp if a[5] is None else a[5]
            a[6] = ltp if a[6] is None else max(a[6], ltp)
            a[7] = ltp if a[7] is None else min(a[7], ltp)
            a[8] = ltp
        a[9], a[10] = tbq, tsq
        if oi:
            a[11] = oi
        a[12] = vol or a[12]
        self._dirty.add((key, minute))
        if q > 0:
            lv = self._fp.setdefault(key, {}).setdefault(minute, {}).setdefault(ltp, [0.0, 0.0])
            lv[0] += buy
            lv[1] += q - buy
            st["n"] += 1
            if st["n"] > PRINT_WARMUP and q >= PRINT_MULT * st["ema"]:
                self._prints.append((key, exch_ts, ltp, q, side))
            st["ema"] = q if st["n"] == 1 else st["ema"] + 0.02 * (q - st["ema"])
        s = self.stats.get(key)
        if s is None:
            s = self.stats[key] = [0, 0, 0.0, 0.0, 0.0, 0]
        s[0] += 1
        s[1] += 1 if q > 0 else 0
        s[2] += buy
        s[3] += q - buy
        s[4] += unk
        s[5] = exch_ts
        self.frames += 1
        if q > 0 and len(self.lag) < 2000:
            # only a trade's stamp is fresh: a quote-only frame or a subscribe snapshot
            # carries the contract's last update (hours old for a quiet option)
            self.lag.append(recv_ts - exch_ts)
        if key in self.raw_keys:
            self._raw.setdefault(key, []).append((recv_ts, exch_ts, ltp, vol, dv, side, bid, ask, bsz, asz, tbq, tsq,
                                                  ltq, oi))

    # -- persistence -----------------------------------------------------------

    def today_dir(self, day: int) -> Path:
        return self.root / "today" / ymd(day)

    def flush(self) -> int:
        """Append every minute touched since the last flush (its latest
        totals) to today's per-key files, then forget minutes that can no
        longer change. Returns the number of rows written."""
        with self._lock:
            dirty, self._dirty = self._dirty, set()
            flow: dict[str, list[list]] = {}
            fps: dict[str, list[tuple]] = {}
            for key, minute in sorted(dirty):
                a = self._min.get(key, {}).get(minute)
                if a is None:
                    continue
                flow.setdefault(key, []).append([minute, *a])
                for price, (b, s) in sorted(self._fp.get(key, {}).get(minute, {}).items()):
                    fps.setdefault(key, []).append((minute, price, b, s))
            prints, self._prints = self._prints, []
            raw, self._raw = self._raw, {}
            snaps, self._snaps = self._snaps, []
            for key, mins in self._min.items():          # keep the last 3 minutes; the rest is final
                if len(mins) > 3:
                    keep = sorted(mins)[-3:]
                    for m in [m for m in mins if m < keep[0]]:
                        mins.pop(m, None)
                        self._fp.get(key, {}).pop(m, None)
                    self._floor[key] = keep[0]
        n = 0
        for key, rows in flow.items():
            n += self._append(key, rows, "flow")
        for key, rows in fps.items():
            n += self._append(key, rows, "fp")
        by_day: dict[int, list[tuple]] = {}
        for p in prints:
            by_day.setdefault(ist_day(p[1]), []).append(p)
        for day, rows in by_day.items():
            path = self.today_dir(day) / "prints.csv"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerows(rows)
        for key, rows in raw.items():
            n += self._append(key, rows, "raw", day_col=1)
        snap_days: dict[int, list[tuple]] = {}
        for s in snaps:
            snap_days.setdefault(ist_day(s[1]), []).append(s)
        for day, rows in snap_days.items():
            path = self.today_dir(day) / "chain.csv"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerows(["" if x is None else x for x in r] for r in rows)
            n += len(rows)
        return n

    def _append(self, key: str, rows: list, kind: str, day_col: int = 0) -> int:
        by_day: dict[int, list] = {}
        for r in rows:
            by_day.setdefault(ist_day(r[day_col]), []).append(r)
        for day, rs in by_day.items():
            path = self.today_dir(day) / kind / f"{key}.csv"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerows(["" if x is None else x for x in r] for r in rs)
        return len(rows)

    def resume(self, now: Optional[float] = None) -> int:
        """After a restart: re-anchor each key's cumulative volume (what traded
        while down becomes unk) and reload the minute it was in, so new frames
        add to it instead of overwriting it."""
        day = ist_day(now or time.time())
        d = self.today_dir(day)
        n = 0
        for path in (d / "flow").glob("*.csv") if (d / "flow").is_dir() else []:
            last = _tail(path, 1)
            if not last:
                continue
            r = last[-1]
            try:
                minute = int(float(r[0]))
                a = [_num(x) for x in r[1:]]
            except (ValueError, IndexError):
                continue
            if len(a) != len(FLOW_COLS) - 1:
                continue
            for i in (0, 1, 2, 9, 10, 12):
                a[i] = a[i] or 0.0
            a[3], a[4] = int(a[3] or 0), int(a[4] or 0)
            key = path.stem
            with self._lock:
                self._min.setdefault(key, {})[minute] = a
                if a[12]:
                    self._resume_vol[key] = a[12]
                fp = self._fp.setdefault(key, {}).setdefault(minute, {})
                for fr in _tail(d / "fp" / f"{key}.csv", 400):
                    try:
                        if int(float(fr[0])) == minute:
                            fp[float(fr[1])] = [float(fr[2]), float(fr[3])]
                    except (ValueError, IndexError):
                        continue
            n += 1
        if n:
            log.info("ticks.resumed", keys=n)
        return n

    def write_meta(self, meta: dict[str, dict], day: Optional[int] = None) -> None:
        """key -> contract details of the day (merged: a key keeps the first contract it held that day;
        the research flags — chain, long, raw — accumulate: once set during a day, they stay)."""
        path = self.today_dir(day if day is not None else ist_day(time.time())) / "meta.json"
        cur = read_json(path) or {}
        merged = merge_meta(cur, meta)
        if merged != cur or not path.exists():
            write_json(path, merged)

    # -- after the close ------------------------------------------------------

    def compact(self, day: int) -> bool:
        """today/<day> -> days/<day>/*.parquet (merged into an existing day),
        then the day's working files go. Returns True when anything moved."""
        src = self.today_dir(day)
        if not src.is_dir():
            return False
        out = self.root / "days" / ymd(day)
        out.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.root) as tmpd:
            tmp = Path(tmpd)
            con = _duck(spill=tmp / "spill")
            # flow: last row per (key, minute)
            n = _combine(tmp / "flow.csv", (src / "flow").glob("*.csv") if (src / "flow").is_dir() else [],
                         lambda r: r[0], len(FLOW_COLS))
            if n:
                cols = ", ".join(f"'{c}': '{t}'" for c, t in zip(("key", *FLOW_COLS), ("VARCHAR", *FLOW_TYPES)))
                _write_parquet(con, out / "flow.parquet",
                               f"SELECT * FROM read_csv('{(tmp / 'flow.csv').as_posix()}', header=false, "
                               f"columns={{{cols}}}, nullstr='')", "key, minute")
            n = _combine(tmp / "fp.csv", (src / "fp").glob("*.csv") if (src / "fp").is_dir() else [],
                         lambda r: (r[0], r[1]), len(FP_COLS))
            if n:
                _write_parquet(con, out / "fp.parquet",
                               f"SELECT * FROM read_csv('{(tmp / 'fp.csv').as_posix()}', header=false, columns="
                               "{'key': 'VARCHAR', 'minute': 'BIGINT', 'price': 'DOUBLE', 'buy': 'DOUBLE', "
                               "'sell': 'DOUBLE'})", "key, minute, price")
            if (src / "prints.csv").exists() and (src / "prints.csv").stat().st_size:
                _write_parquet(con, out / "prints.parquet",
                               f"SELECT * FROM read_csv('{(src / 'prints.csv').as_posix()}', header=false, columns="
                               "{'key': 'VARCHAR', 'ts': 'BIGINT', 'price': 'DOUBLE', 'qty': 'DOUBLE', "
                               "'side': 'INTEGER'})", "key, ts", dedupe=False)
            raws = list((src / "raw").glob("*.csv")) if (src / "raw").is_dir() else []
            if raws:
                with (tmp / "raw.csv").open("w", newline="", encoding="utf-8") as fh:
                    w = csv.writer(fh)
                    for p in raws:
                        for r in _rows(p):
                            if len(r) == len(RAW_COLS):
                                w.writerow([p.stem, *r])
                cols = ", ".join(f"'{c}': 'DOUBLE'" for c in RAW_COLS)
                _write_parquet(con, out / "raw.parquet",
                               f"SELECT * FROM read_csv('{(tmp / 'raw.csv').as_posix()}', header=false, "
                               f"columns={{'key': 'VARCHAR', {cols}}}, nullstr='')", "key, exch_ts", dedupe=False)
            chain = src / "chain.csv"
            if chain.exists() and chain.stat().st_size:
                # one row per (key, grid point) by construction: DuckDB streams it (no rows in Python's memory)
                types = ("BIGINT", "BIGINT", *("DOUBLE",) * (len(CHAIN_COLS) - 2))
                cols = ", ".join(f"'{c}': '{t}'" for c, t in zip(("key", *CHAIN_COLS), ("VARCHAR", *types)))
                _write_parquet(con, out / "chain.parquet",
                               f"SELECT * FROM read_csv('{chain.as_posix()}', header=false, columns={{{cols}}}, "
                               "nullstr='', ignore_errors=true) QUALIFY row_number() OVER (PARTITION BY key, ts) = 1",
                               "key, ts")
            con.close()
        meta = read_json(src / "meta.json") or {}
        if meta:
            write_json(out / "meta.json", merge_meta(read_json(out / "meta.json") or {}, meta))
        shutil.rmtree(src, ignore_errors=True)
        with self._lock:
            self.stats = {}
            self._state = {f: s for f, s in self._state.items() if s["day"] > day}
        log.info("ticks.compacted", day=ymd(day))
        return True

    def sweep(self, now: Optional[float] = None) -> int:
        """Compact every earlier day still in today/ (down at that day's close)."""
        today = ist_day(now or time.time())
        base = self.root / "today"
        n = 0
        for d in sorted(base.iterdir()) if base.is_dir() else []:
            day = day_of(d.name)
            if day is not None and day < today and self.compact(day):
                n += 1
        return n

    def prune(self, retention_days: int, raw_days: int, long: Optional[dict[str, Any]] = None) -> int:
        """The sliding window: keep the newest `retention_days` collected days
        (delete the oldest as each new one lands); raw frames only `raw_days`.

        With the research short list on (`long`), a day leaving the window is
        trimmed to the keys flagged `long` in its meta instead of deleted, and
        kept until it is `long.days` collected days old; raw frames of those
        keys keep to `long.raw_days`. A day with nothing flagged goes as before."""
        base = self.root / "days"
        days = sorted(d for d in base.iterdir() if d.is_dir() and day_of(d.name) is not None) if base.is_dir() else []
        keep = max(1, int(retention_days))
        raw_keep = max(0, int(raw_days))
        on = bool(long and long.get("enabled"))
        long_keep = max(keep, int(long.get("days") or 0)) if on else keep
        long_raw = max(0, int(long.get("raw_days") or 0)) if on else 0
        gone = trimmed = 0
        for i, d in enumerate(days):
            age = len(days) - 1 - i                 # 0 = the newest collected day
            if age >= long_keep:
                shutil.rmtree(d, ignore_errors=True)
                gone += 1
            elif age >= keep:                       # past the window: the short list only
                n = self._trim(d, keep_raw=age < long_raw)
                if n < 0:
                    shutil.rmtree(d, ignore_errors=True)
                    gone += 1
                else:
                    trimmed += n
            elif age >= raw_keep and (d / "raw.parquet").exists():
                if on and age < long_raw:
                    trimmed += self._trim_file(d / "raw.parquet", self._long_keys(d))
                else:
                    (d / "raw.parquet").unlink()
                    gone += 1
        if gone or trimmed:
            log.info("ticks.pruned", removed=gone, trimmed=trimmed, kept=min(len(days), long_keep))
        return gone + trimmed

    def _long_keys(self, d: Path) -> set[str]:
        return {k for k, v in (read_json(d / "meta.json") or {}).items() if v.get("long")}

    def _trim(self, d: Path, keep_raw: bool) -> int:
        """A day past the window -> only its short-list keys. Returns the files
        rewritten (0 = already trimmed), or -1 when the day held none of them."""
        keys = self._long_keys(d)
        if not keys:
            return -1
        mark = read_json(d / "trim.json") or {}
        n = 0
        if not mark:
            for name in ("flow.parquet", "fp.parquet", "prints.parquet", "chain.parquet"):
                n += self._trim_file(d / name, keys)
        if keep_raw:
            if not mark.get("raw_trimmed"):
                n += self._trim_file(d / "raw.parquet", keys)
        elif (d / "raw.parquet").exists():
            (d / "raw.parquet").unlink()
            n += 1
        raw_left = keep_raw and (d / "raw.parquet").exists()
        if not mark or n or bool(mark.get("raw_trimmed")) != raw_left:
            write_json(d / "trim.json", {"keys": len(keys), "raw_trimmed": raw_left, "at": int(time.time())})
        return n

    def _trim_file(self, path: Path, keys: set[str]) -> int:
        """Rewrite one parquet with only `keys`' rows (deleted when none are left)."""
        if not path.exists():
            return 0
        if not keys:
            path.unlink()
            return 1
        con = _duck()
        try:
            con.execute("CREATE TEMP TABLE _keep(key VARCHAR)")
            con.executemany("INSERT INTO _keep VALUES (?)", [[k] for k in sorted(keys)])
            src = f"read_parquet('{path.as_posix()}')"
            total, left = con.execute(f"SELECT count(*), count(*) FILTER (WHERE key IN (SELECT key FROM _keep)) "
                                      f"FROM {src}").fetchone()
            if left == total:
                return 0                              # holds only the list already: nothing to rewrite
            if not left:
                path.unlink()
                return 1
            tmp = path.with_suffix(".parquet.tmp")
            order = SORT_ORDER.get(path.name, "key")
            con.execute(f"COPY (SELECT * FROM {src} WHERE key IN (SELECT key FROM _keep) ORDER BY {order}) "
                        f"TO '{tmp.as_posix()}' (FORMAT parquet, ROW_GROUP_SIZE 100000)")
            tmp.replace(path)
        finally:
            con.close()
        return 1

    def drop_oldest(self, keep: int) -> Optional[str]:
        """The disk guard's lever: delete the oldest collected day while more
        than `keep` remain. Returns the day removed, if any."""
        base = self.root / "days"
        days = sorted(d for d in base.iterdir() if d.is_dir() and day_of(d.name) is not None) if base.is_dir() else []
        if len(days) <= max(0, keep):
            return None
        shutil.rmtree(days[0], ignore_errors=True)
        log.warning("ticks.dropped_for_disk", day=days[0].name, kept=len(days) - 1)
        return days[0].name

    def migrate_legacy(self) -> int:
        """The old layout (flow/<KEY>/<YYYYMM>.parquet, fp/..., raw/<KEY>/<YYYYMMDD>.parquet)
        -> days/<YYYYMMDD>/. One-time; the old files go once copied."""
        import duckdb

        replayed = self._replay_legacy_raw()
        old_flow = sorted(self.root.glob("flow/*/*.parquet"))
        old_fp = sorted(self.root.glob("fp/*/*.parquet"))
        old_raw = sorted(self.root.glob("raw/*/*.parquet"))
        if not (old_flow or old_fp or old_raw):
            return replayed
        spill = Path(tempfile.mkdtemp(dir=self.root))
        con = _duck("512MB", spill=spill)
        moved = 0

        def files(ps: list[Path]) -> str:
            return "[" + ", ".join(f"'{p.as_posix()}'" for p in ps) + "]"

        key_of = "regexp_extract(filename, '/([^/]+)/[^/]+\\.parquet$', 1)"
        if old_flow:
            src = (f"SELECT {key_of} AS key, minute, buy, sell, 0.0 AS unk, trades, ticks, NULL::DOUBLE AS o, "
                   "NULL::DOUBLE AS h, NULL::DOUBLE AS l, NULL::DOUBLE AS c, tot_buy, tot_sell, NULL::DOUBLE AS oi, "
                   f"NULL::DOUBLE AS cumvol FROM read_parquet({files(old_flow)}, filename=true, union_by_name=true)")
            moved += self._migrate_days(con, src, "minute", "flow.parquet", "key, minute")
        if old_fp:
            src = (f"SELECT {key_of} AS key, minute, price, buy, sell FROM read_parquet({files(old_fp)}, "
                   "filename=true, union_by_name=true)")
            moved += self._migrate_days(con, src, "minute", "fp.parquet", "key, minute, price")
        if old_raw:
            cols = ", ".join(f"TRY_CAST({c} AS DOUBLE) AS {c}" if c in ("ltq", "oi") else c for c in RAW_COLS)
            src = (f"SELECT {key_of} AS key, {cols} FROM read_parquet({files(old_raw)}, filename=true, "
                   "union_by_name=true)")
            try:
                moved += self._migrate_days(con, src, "exch_ts", "raw.parquet", "key, exch_ts", dedupe=False)
            except duckdb.Error:                          # an older raw schema without ltq / oi
                src = (f"SELECT {key_of} AS key, {', '.join(c for c in RAW_COLS if c not in ('ltq', 'oi'))}, "
                       f"NULL::DOUBLE AS ltq, NULL::DOUBLE AS oi FROM read_parquet({files(old_raw)}, filename=true)")
                moved += self._migrate_days(con, src, "exch_ts", "raw.parquet", "key, exch_ts", dedupe=False)
        con.close()
        shutil.rmtree(spill, ignore_errors=True)
        for p in old_flow + old_fp + old_raw:
            p.unlink(missing_ok=True)
        for sub in ("flow", "fp", "raw"):
            for d in (self.root / sub).glob("*") if (self.root / sub).is_dir() else []:
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
        log.info("ticks.migrated_legacy", days=moved)
        return moved

    def _replay_legacy_raw(self) -> int:
        """The old recorder's unfinished days (raw/<KEY>/<YYYYMMDD>.csv: it
        rebuilt the day's flow from them) go through this classifier into
        today/ — a deploy mid-session keeps the morning's flow."""
        old = sorted(self.root.glob("raw/*/*.csv"))
        for p in old:
            key = p.parent.name
            with self._lock:
                for r in _rows(p):
                    try:
                        v = [float(x) for x in r[:12]]
                    except (ValueError, IndexError):
                        continue                          # the header / a cut line
                    row = (v[0], int(v[1]), v[2], v[3], v[6], v[7], v[8], v[9], v[10], v[11], 0.0, None)
                    self._classify(f"LEGACY:{key}", row, key=key, exchange_hours=not key.startswith("MCX_"))
            self.flush()
            p.unlink(missing_ok=True)
        with self._lock:
            self._state = {f: st for f, st in self._state.items() if not f.startswith("LEGACY:")}
        if old:
            log.info("ticks.replayed_legacy_raw", files=len(old))
        return len(old)

    def _migrate_days(self, con, src: str, tcol: str, name: str, order: str, dedupe: bool = True) -> int:
        con.execute(f"CREATE OR REPLACE TEMP TABLE _m AS {src}")
        days = [r[0] for r in con.execute(f"SELECT DISTINCT CAST(({tcol} + {IST}) // {DAY} AS BIGINT) FROM _m").fetchall()]
        for day in days:
            out = self.root / "days" / ymd(int(day))
            out.mkdir(parents=True, exist_ok=True)
            _write_parquet(con, out / name, f"SELECT * FROM _m WHERE ({tcol} + {IST}) // {DAY} = {int(day)}", order,
                           dedupe=dedupe)
        return len(days)


def _rows(path: Path) -> Iterable[list[str]]:
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            yield from csv.reader(fh)
    except OSError:
        return


def _tail(path: Path, n: int) -> list[list[str]]:
    """The last `n` complete CSV rows of a file (a reader racing the writer
    may meet a half-written last line: it's dropped)."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            back = min(size, max(4096, n * 160))
            fh.seek(size - back)
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = data.split("\n")
    if back < size:
        lines = lines[1:]                     # the first line may be cut
    if lines and not data.endswith("\n"):
        lines = lines[:-1]
    out = [r for r in csv.reader(x for x in lines if x)]
    return out[-n:]


def _combine(out: Path, paths: Iterable[Path], ident, width: int) -> int:
    """Concatenate per-key CSVs into one (key as the first column), keeping
    the LAST row of each identity (a minute, or a minute + price)."""
    n = 0
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        for p in paths:
            last: dict[Any, list[str]] = {}
            for r in _rows(p):
                if len(r) == width:
                    last[ident(r)] = r
            for r in last.values():
                w.writerow([p.stem, *r])
                n += 1
    return n


def _duck(memory: str = "256MB", threads: int = 1, spill: Optional[Path] = None):
    """A DuckDB connection that stays small: the server is 1.9 GB and the
    app's service (collector included) is capped at 1.4 GB, so sorts spill
    to disk rather than grow."""
    import duckdb

    con = duckdb.connect()
    settings = [f"SET memory_limit='{memory}'", f"SET threads={int(threads)}", "SET preserve_insertion_order=false"]
    if spill is not None:
        settings.append(f"SET temp_directory='{spill.as_posix()}'")
    for sql in settings:
        try:
            con.execute(sql)
        except Exception:  # noqa: BLE001 — an older DuckDB without the setting: its default
            pass
    return con


def _write_parquet(con, path: Path, src: str, order: str, dedupe: bool = True) -> None:
    """(Over)write a day's parquet with `src`, merged into what it already
    holds — the new rows win on the same identity (`order`'s columns)."""
    if path.exists():
        prev = f"SELECT *, 0 AS _v FROM read_parquet('{path.as_posix()}')"
        src = f"{prev} UNION ALL BY NAME SELECT *, 1 AS _v FROM ({src})"
        if dedupe:
            src = f"SELECT * EXCLUDE (_v) FROM ({src}) QUALIFY row_number() OVER (PARTITION BY {order} ORDER BY _v DESC) = 1"
        else:
            src = f"SELECT * EXCLUDE (_v) FROM ({src})"
    tmp = path.with_suffix(".parquet.tmp")
    con.execute(f"COPY ({src} ORDER BY {order}) TO '{tmp.as_posix()}' (FORMAT parquet, ROW_GROUP_SIZE 100000)")
    tmp.replace(path)


# ---- reader: the app's view of what was recorded --------------------------------

class FlowStore:
    """Reads recorded order flow — finished days from parquet, today from
    the collector's working files (no process shares memory with it)."""

    def __init__(self, root: Path = ROOT) -> None:
        self.root = root

    def _days(self, start: int, end: int) -> list[int]:
        return list(range(ist_day(start), ist_day(max(start, end - 1)) + 1))

    def _parquets(self, name: str, start: int, end: int) -> list[Path]:
        return [p for d in self._days(start, end) if (p := self.root / "days" / ymd(d) / name).exists()]

    def _legacy(self, sub: str, key: str, start: int, end: int) -> list[Path]:
        months = {time.strftime("%Y%m", time.gmtime(d * DAY)) for d in self._days(start, end)}
        return [p for m in sorted(months) if (p := self.root / sub / key / f"{m}.parquet").exists()]

    def _query(self, sql: str, params: list) -> list[tuple]:
        import duckdb

        try:
            con = _duck(threads=2)
            try:
                return con.execute(sql, params).fetchall()
            finally:
                con.close()
        except duckdb.Error as e:
            log.warning("ticks.read_failed", error=str(e)[:200])
            return []

    def flow_rows(self, key: str, start: int, end: int) -> list[tuple]:
        """[(minute, buy, sell, unk, trades, ticks, o, h, l, c, tot_buy, tot_sell, oi, cumvol)] by minute."""
        rows: dict[int, tuple] = {}
        files = self._parquets("flow.parquet", start, end)
        if files:
            lst = ", ".join(f"'{p.as_posix()}'" for p in files)
            for r in self._query(f"SELECT {', '.join(FLOW_COLS)} FROM read_parquet([{lst}]) "
                                 "WHERE key = ? AND minute >= ? AND minute < ?", [key, int(start), int(end)]):
                rows[int(r[0])] = r
        legacy = self._legacy("flow", key, start, end)
        if legacy:
            lst = ", ".join(f"'{p.as_posix()}'" for p in legacy)
            for r in self._query(f"SELECT minute, buy, sell, 0.0, trades, ticks, NULL, NULL, NULL, NULL, tot_buy, "
                                 f"tot_sell, NULL, NULL FROM read_parquet([{lst}]) WHERE minute >= ? AND minute < ?",
                                 [int(start), int(end)]):
                rows.setdefault(int(r[0]), r)
        for d in self._days(start, end):
            p = self.root / "today" / ymd(d) / "flow" / f"{key}.csv"
            for r in _rows(p):
                if len(r) != len(FLOW_COLS):
                    continue
                try:
                    t = int(float(r[0]))
                except ValueError:
                    continue
                if start <= t < end:
                    v = [_num(x) for x in r[1:]]
                    rows[t] = (t, *v)
        return [rows[t] for t in sorted(rows)]

    def minute_flow(self, key: str, start: int, end: int) -> dict[int, tuple[float, float]]:
        """{minute: (buy, sell)} — what the backtests and the live runner read."""
        return {int(r[0]): (r[1] or 0.0, r[2] or 0.0) for r in self.flow_rows(key, start, end)
                if (r[1] or 0) + (r[2] or 0) > 0}

    def footprint(self, key: str, start: int, end: int) -> list[tuple[int, float, float, float]]:
        out: dict[tuple[int, float], tuple] = {}
        files = self._parquets("fp.parquet", start, end)
        if files:
            lst = ", ".join(f"'{p.as_posix()}'" for p in files)
            for r in self._query(f"SELECT minute, price, buy, sell FROM read_parquet([{lst}]) "
                                 "WHERE key = ? AND minute >= ? AND minute < ?", [key, int(start), int(end)]):
                out[(int(r[0]), float(r[1]))] = r
        legacy = self._legacy("fp", key, start, end)
        if legacy:
            lst = ", ".join(f"'{p.as_posix()}'" for p in legacy)
            for r in self._query(f"SELECT minute, price, buy, sell FROM read_parquet([{lst}]) "
                                 "WHERE minute >= ? AND minute < ?", [int(start), int(end)]):
                out.setdefault((int(r[0]), float(r[1])), r)
        for d in self._days(start, end):
            for r in _rows(self.root / "today" / ymd(d) / "fp" / f"{key}.csv"):
                if len(r) != len(FP_COLS):
                    continue
                try:
                    t, p, b, s = int(float(r[0])), float(r[1]), float(r[2]), float(r[3])
                except ValueError:
                    continue
                if start <= t < end:
                    out[(t, p)] = (t, p, b, s)
        return [out[k] for k in sorted(out)]

    def prints(self, key: str, start: int, end: int) -> list[tuple]:
        out: list[tuple] = []
        files = self._parquets("prints.parquet", start, end)
        if files:
            lst = ", ".join(f"'{p.as_posix()}'" for p in files)
            out += self._query(f"SELECT ts, price, qty, side FROM read_parquet([{lst}]) "
                               "WHERE key = ? AND ts >= ? AND ts < ? ORDER BY ts", [key, int(start), int(end)])
        for d in self._days(start, end):
            for r in _rows(self.root / "today" / ymd(d) / "prints.csv"):
                if len(r) == 5 and r[0] == key:
                    try:
                        t = int(float(r[1]))
                        if start <= t < end:
                            out.append((t, float(r[2]), float(r[3]), int(float(r[4]))))
                    except ValueError:
                        continue
        return out

    def meta(self, day: int) -> dict[str, dict]:
        return (read_json(self.root / "days" / ymd(day) / "meta.json")
                or read_json(self.root / "today" / ymd(day) / "meta.json") or {})

    def keys_for(self, symbol: str, start: int, end: int) -> dict[int, str]:
        """{day: key} holding `symbol` that day — a futures contract was NIFTY-FUT2
        before it became NIFTY-FUT. Days without a record use flow_key(). An index
        always reads its current future (NIFTY-FUT): the index's own key (IDX_NIFTY)
        holds prices only, never a trade."""
        from app.algo import fno

        sym = symbol.upper()
        if sym in fno.INDICES:
            return {d: flow_key(sym) for d in self._days(start, end)}
        out = {}
        for d in self._days(start, end):
            m = self.meta(d)
            hit = next((k for k, v in m.items() if str(v.get("symbol", "")).upper() == sym), None)
            out[d] = hit or flow_key(sym)
        return out

    # -- option chains ------------------------------------------------------------

    def chain_keys(self, name: str, day: int) -> dict[str, dict]:
        """key -> contract of `name`'s chain that day (options, its future, the underlying)."""
        n = name.upper()
        return {k: v for k, v in self.meta(day).items() if str(v.get("chain", "")).upper() == n}

    def chain_rows(self, name: str, start: int, end: int) -> list[tuple]:
        """Snapshots of `name`'s chain in [start, end): (ts, key, qts, ltp, bid, ask, bid_size, ask_size,
        oi, vol), by time then key. Join a key to its contract with chain_keys(name, day)."""
        out: dict[tuple[int, str], tuple] = {}
        for d in self._days(start, end):
            keys = sorted(self.chain_keys(name, d))
            if not keys:
                continue
            p = self.root / "days" / ymd(d) / "chain.parquet"
            if p.exists():
                marks = ", ".join("?" for _ in keys)
                for r in self._query(f"SELECT ts, key, {', '.join(CHAIN_COLS[1:])} FROM read_parquet('{p.as_posix()}') "
                                     f"WHERE key IN ({marks}) AND ts >= ? AND ts < ?", [*keys, int(start), int(end)]):
                    out[(int(r[0]), r[1])] = r
            want = set(keys)
            for r in _rows(self.root / "today" / ymd(d) / "chain.csv"):
                if len(r) != len(CHAIN_COLS) + 1 or r[0] not in want:
                    continue
                try:
                    t = int(float(r[1]))
                except ValueError:
                    continue
                if start <= t < end:
                    out[(t, r[0])] = (t, r[0], int(float(r[2])), *(_num(x) for x in r[3:]))
        return [out[k] for k in sorted(out)]

    def chain_at(self, name: str, at: int) -> tuple[Optional[int], list[dict[str, Any]]]:
        """The latest snapshot of `name`'s chain at or before `at` (same IST day):
        (its time, [{key, kind, strike, expiry, symbol, qts, ltp, bid, ask, ...}])."""
        day0 = at - (at + IST) % DAY
        rows = self.chain_rows(name, day0, at + 1)
        if not rows:
            return None, []
        t = rows[-1][0]
        meta = self.chain_keys(name, ist_day(at))
        out = []
        for r in rows:
            if r[0] != t:
                continue
            m = meta.get(r[1], {})
            out.append({"key": r[1], "symbol": m.get("symbol"), "kind": m.get("kind"), "strike": m.get("strike"),
                        "expiry": m.get("expiry"), **dict(zip(CHAIN_COLS[1:], r[2:]))})
        return t, out

    def long_days(self) -> list[str]:
        """Days past the window kept for the research short list only (trimmed)."""
        base = self.root / "days"
        return sorted(d.name for d in base.iterdir() if d.is_dir() and (d / "trim.json").exists()) \
            if base.is_dir() else []

    def collected_days(self) -> list[str]:
        base = self.root / "days"
        return sorted(d.name for d in base.iterdir() if d.is_dir() and (d / "flow.parquet").exists()) \
            if base.is_dir() else []


_STORE: Optional[FlowStore] = None


def store() -> FlowStore:
    global _STORE
    if _STORE is None:
        _STORE = FlowStore()
    return _STORE


_CAP: dict[str, Any] = {"t": 0.0, "sig": None, "v": None}
CAP_SAMPLE_DAYS = 10


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def capacity(cfg: dict[str, Any], root: Path = ROOT, max_age_s: float = 300.0) -> dict[str, Any]:
    """Will the recordings fit? The steady-state size of the window as configured,
    projected from the newest stored days' real sizes (per tier: full days, raw
    frames, short-list days, short-list raw), against the disk's free space and
    the guard's floor. A tier with no stored day yet is estimated from the
    newest full day's key counts in its meta and marked `estimate`."""
    sig = json.dumps([str(root), *(cfg.get(k) for k in ("retention_days", "raw_days", "min_free_gb", "long"))],
                     sort_keys=True, default=str)
    if _CAP["v"] is not None and _CAP["sig"] == sig and time.time() - _CAP["t"] < max_age_s:
        return _CAP["v"]
    base = root / "days"
    dirs = sorted((d for d in base.iterdir() if d.is_dir() and day_of(d.name) is not None), reverse=True) \
        if base.is_dir() else []
    full, raw, trim, trim_raw = [], [], [], []
    for d in dirs:
        rest = sum(_size(p) for p in d.glob("*.parquet") if p.name != "raw.parquet")
        r = _size(d / "raw.parquet")
        if (d / "trim.json").exists():
            if len(trim) < CAP_SAMPLE_DAYS:
                trim.append(rest)
                if r:
                    trim_raw.append(r)
        else:
            if len(full) < CAP_SAMPLE_DAYS:
                full.append(rest)
            if r and len(raw) < CAP_SAMPLE_DAYS:
                raw.append(r)
        if len(full) >= CAP_SAMPLE_DAYS and len(trim) >= CAP_SAMPLE_DAYS:
            break

    def avg(xs: list[int]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    full_b, raw_b = avg(full), avg(raw)
    basis = {"full": len(full), "raw": len(raw), "long": len(trim), "long_raw": len(trim_raw)}
    estimate: list[str] = []
    long = cfg.get("long") or {}
    on = bool(long.get("enabled"))
    keep = max(1, int(cfg.get("retention_days") or RETENTION_DAYS))
    raw_keep = min(keep, max(0, int(cfg.get("raw_days") or 0)))
    long_keep = max(keep, int(long.get("days") or 0)) if on else keep
    long_raw = min(long_keep, max(raw_keep, int(long.get("raw_days") or 0))) if on else raw_keep
    trim_b, trim_raw_b = avg(trim), avg(trim_raw)
    if on and (not trim or not trim_raw):
        newest = next((d for d in dirs if not (d / "trim.json").exists()), None)
        meta = (read_json(newest / "meta.json") or {}) if newest is not None else {}
        n_all = max(1, len(meta))
        n_long = sum(1 for v in meta.values() if v.get("long"))
        n_raw = max(1, sum(1 for v in meta.values() if v.get("cls") == "focus" or v.get("raw")))
        n_long_raw = sum(1 for v in meta.values() if v.get("long") and (v.get("cls") == "focus" or v.get("raw")))
        if not trim:
            trim_b = full_b * n_long / n_all
            estimate.append("long")
        if not trim_raw:
            trim_raw_b = raw_b * min(1.0, n_long_raw / n_raw)
            estimate.append("long_raw")
    tiers = {
        "full": full_b * keep,
        "raw": raw_b * raw_keep,
        "long": trim_b * (long_keep - keep) if on else 0.0,
        "long_raw": trim_raw_b * max(0, long_raw - raw_keep) if on else 0.0,
    }
    today_b = 0
    for dirpath, _, names in os.walk(root / "today") if (root / "today").is_dir() else []:
        today_b += sum(_size(Path(dirpath) / n) for n in names)
    projected = sum(tiers.values()) + 2 * today_b      # the day being recorded + its compaction's scratch
    now_b = 0
    for dirpath, _, names in os.walk(root) if root.is_dir() else []:
        now_b += sum(_size(Path(dirpath) / n) for n in names)
    try:
        du = shutil.disk_usage(root if root.exists() else root.parent)
        total, free = du.total, du.free
    except OSError:
        total = free = 0
    floor = float(cfg.get("min_free_gb") or MIN_FREE_GB) * 1e9
    room = free + now_b - floor                         # what the recordings may grow to before the guard acts
    gb = 1e9
    out = {
        "per_day_mb": {"full": round(full_b / 1e6, 2), "raw": round(raw_b / 1e6, 2), "long": round(trim_b / 1e6, 2),
                       "long_raw": round(trim_raw_b / 1e6, 2), "today_so_far": round(today_b / 1e6, 2)},
        "days": {"full": keep, "raw": raw_keep, "long": long_keep if on else 0, "long_raw": long_raw if on else 0},
        "tiers_gb": {k: round(v / gb, 2) for k, v in tiers.items()},
        "projected_gb": round(projected / gb, 2),
        "now_gb": round(now_b / gb, 2),
        "disk_total_gb": round(total / gb, 1),
        "free_gb": round(free / gb, 1),
        "min_free_gb": round(floor / gb, 1),
        "room_gb": round(room / gb, 2),
        "fits": bool(full) and projected <= room,
        "basis_days": basis,
        "estimate": estimate,
        "note": None if full else "no stored day yet: the projection needs one recorded session",
    }
    _CAP.update(t=time.time(), sig=sig, v=out)
    return out


_DISK: dict[str, Any] = {"t": 0.0, "mb": 0.0}


def disk_mb(root: Path = ROOT) -> float:
    """Size of the recordings (cached a minute: a walk over thousands of files)."""
    if time.time() - _DISK["t"] > 60:
        size = 0
        for dirpath, _, names in os.walk(root):
            for n in names:
                try:
                    size += os.path.getsize(os.path.join(dirpath, n))
                except OSError:
                    pass
        _DISK.update(t=time.time(), mb=round(size / 1e6, 1))
    return _DISK["mb"]
