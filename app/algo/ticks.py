"""Real tick recording from the Fyers data socket -> real order flow.

Fyers keeps no tick history, so the only way to get REAL order flow is to
record it as it happens. Every SymbolUpdate frame for a recorded symbol is a
snapshot: LTP, cumulative day volume, best bid / ask (+ sizes) and the total
buy / sell quantity resting in the book. Each increase in cumulative volume is
volume that traded since the previous frame; its side is classified with the
Lee-Ready rule against the quote that was standing BEFORE the trade:

    price >= previous ask  -> buyer-initiated
    price <= previous bid  -> seller-initiated
    otherwise              -> tick test: up-tick buy, down-tick sell,
                              zero-tick keeps the last side

What gets kept, under data/ticks/:
    raw/<KEY>/<YYYYMMDD>.csv       every frame, appended as it arrives (crash-safe;
                                   replayed on restart; compacted to parquet after the close)
    raw/<KEY>/<YYYYMMDD>.parquet
    flow/<KEY>/<YYYYMM>.parquet    per minute: buy / sell volume, trades, book quantities
    fp/<KEY>/<YYYYMM>.parquet      per minute per price: buy / sell volume (footprint)
    config.json                    {"enabled": bool, "symbols": [...]}

Futures are stored under a CONTINUOUS key (NIFTY26OCTFUT -> NIFTY-FUT), so the
flow survives contract rolls — and is what an index's candles use, since an
index itself has no trades.
"""
from __future__ import annotations

import asyncio
import calendar
import csv
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

from app.logging_config import get_logger

log = get_logger(__name__)

ROOT = Path(__file__).resolve().parents[2] / "data" / "ticks"
IST = 19800
COMPACT_AFTER_MIN = 15 * 60 + 40
MAX_SYMBOLS = 80
DEFAULT_SYMBOLS = ["NIFTY:FUT", "BANKNIFTY:FUT", "NSE:RELIANCE-EQ", "NSE:HDFCBANK-EQ", "NSE:ICICIBANK-EQ",
                   "NSE:SBIN-EQ", "NSE:INFY-EQ", "NSE:TCS-EQ", "NSE:AXISBANK-EQ", "NSE:KOTAKBANK-EQ",
                   "NSE:LT-EQ", "NSE:ITC-EQ", "NSE:BHARTIARTL-EQ", "NSE:BAJFINANCE-EQ"]
RAW_COLS = ("recv_ts", "exch_ts", "ltp", "vol", "dv", "side", "bid", "ask", "bid_size", "ask_size",
            "tot_buy", "tot_sell")
_FUT = re.compile(r"^(?P<ex>[A-Z]+):(?P<name>[A-Z0-9&-]+?)\d{2}[A-Z]{3}FUT$")


def flow_key(symbol: str) -> str:
    """Storage key: NSE:SBIN-EQ -> SBIN, NSE:NIFTY26OCTFUT -> NIFTY-FUT,
    BSE:SENSEX26OCTFUT -> BSE_SENSEX-FUT, NSE:NIFTY50-INDEX -> NIFTY-FUT (an
    index has no trades of its own; its future's flow stands in)."""
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


class TickRecorder:
    def __init__(self, root: Path = ROOT) -> None:
        self.root = root
        self._lock = threading.Lock()
        self._buf: dict[str, list[tuple]] = {}
        self._state: dict[str, dict[str, Any]] = {}
        # today's flow, in memory: key -> {minute_ts: [buy, sell, trades, ticks, tot_buy, tot_sell]}
        self._minutes: dict[str, dict[int, list[float]]] = {}
        self._fp: dict[str, dict[tuple[int, float], list[float]]] = {}
        self._day: Optional[int] = None
        self._compacted_day: Optional[int] = None
        self._resolved: dict[str, str] = {}       # config entry -> full Fyers id (futures roll)
        self.stats: dict[str, dict[str, Any]] = {}
        self.extra: list[str] = []                 # symbols live strategies trade
        self.config = self._load_config()

    # -- config ------------------------------------------------------------

    def _load_config(self) -> dict[str, Any]:
        try:
            return json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"enabled": True, "symbols": list(DEFAULT_SYMBOLS)}

    def save_config(self, enabled: bool, symbols: list[str]) -> dict[str, Any]:
        syms = list(dict.fromkeys(s.strip().upper() for s in symbols if s.strip()))[:MAX_SYMBOLS]
        self.config = {"enabled": bool(enabled), "symbols": syms}
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / "config.json.tmp"
        tmp.write_text(json.dumps(self.config, indent=1), encoding="utf-8")
        tmp.replace(self.root / "config.json")
        self._resolved.clear()
        return self.config

    def symbols(self) -> list[str]:
        """Full Fyers ids to keep subscribed. "NIFTY:FUT" means the current
        month's future, re-resolved each day so it rolls with expiry."""
        if not self.config.get("enabled"):
            return []
        from app.algo import fno

        out = []
        for s in list(self.config.get("symbols", [])) + self.extra:
            full = self._resolved.get(s)
            if full is None:
                if s.endswith(":FUT"):
                    full = fno.future_symbol(s.split(":")[0], "current") or ""
                elif s in fno.INDICES:
                    name = fno.INDICES[s][0]
                    full = fno.future_symbol(name, "current") or ""
                else:
                    full = s
                self._resolved[s] = full
            if full:
                out.append(full)
        return list(dict.fromkeys(out))[:MAX_SYMBOLS]

    # -- hot path (Fyers SDK thread) ----------------------------------------

    def on_tick(self, full: str, msg: dict[str, Any], recv_ts: Optional[float] = None) -> None:
        """Classify one frame and buffer it. Cheap and lock-bounded: it runs on
        the socket thread for every frame."""
        vol = _f(msg.get("vol_traded_today"))
        ltp = _f(msg.get("ltp"))
        if ltp <= 0:
            return
        recv_ts = recv_ts or time.time()
        exch_ts = int(_f(msg.get("exch_feed_time")) or _f(msg.get("last_traded_time")) or recv_ts)
        if (exch_ts + IST) // 86400 != (recv_ts + IST) // 86400:
            return                    # the snapshot Fyers pushes on subscribe: an earlier session's last trade
        bid, ask = _f(msg.get("bid_price")), _f(msg.get("ask_price"))
        row = (round(recv_ts, 3), exch_ts, ltp, vol, 0.0, 0, bid, ask, _f(msg.get("bid_size")),
               _f(msg.get("ask_size")), _f(msg.get("tot_buy_qty")), _f(msg.get("tot_sell_qty")))
        key = flow_key(full)
        with self._lock:
            row = self._classify(key, row)
            self._buf.setdefault(key, []).append(row)

    def _classify(self, key: str, row: tuple) -> tuple:
        """Fill dv (volume traded since the previous frame) and side (+1 buy,
        -1 sell, 0 unknown -> split) and fold into today's minute flow."""
        recv_ts, exch_ts, ltp, vol, _, _, bid, ask, bsz, asz, tbq, tsq = row
        day = int((exch_ts + IST) // 86400)
        st = self._state.get(key)
        if st is None or st["day"] != day:             # vol_traded_today restarts each session
            st = self._state[key] = {"day": day, "vol": None, "bid": 0.0, "ask": 0.0, "ltp": 0.0, "side": 0}
        dv = vol - st["vol"] if st["vol"] is not None and vol >= st["vol"] else 0.0
        side = 0
        if dv > 0:
            if st["ask"] and ltp >= st["ask"]:
                side = 1
            elif st["bid"] and ltp <= st["bid"]:
                side = -1
            elif st["ltp"] and ltp != st["ltp"]:
                side = 1 if ltp > st["ltp"] else -1
            else:
                side = st["side"]
            st["side"] = side or st["side"]
        if vol:
            st["vol"] = vol
        st["bid"], st["ask"], st["ltp"] = bid or st["bid"], ask or st["ask"], ltp
        minute = exch_ts - exch_ts % 60
        m = self._minutes.setdefault(key, {}).setdefault(minute, [0.0, 0.0, 0, 0, 0.0, 0.0])
        buy = dv if side > 0 else dv / 2 if side == 0 else 0.0
        m[0] += buy
        m[1] += dv - buy
        m[2] += 1 if dv > 0 else 0
        m[3] += 1
        m[4], m[5] = tbq, tsq
        if dv > 0:
            f = self._fp.setdefault(key, {}).setdefault((minute, ltp), [0.0, 0.0])
            f[0] += buy
            f[1] += dv - buy
        s = self.stats.setdefault(key, {"ticks": 0, "trades": 0, "buy": 0.0, "sell": 0.0, "last": 0})
        s["ticks"] += 1
        s["trades"] += 1 if dv > 0 else 0
        s["buy"] += buy
        s["sell"] += dv - buy
        s["last"] = exch_ts
        return (recv_ts, exch_ts, ltp, vol, dv, side, bid, ask, bsz, asz, tbq, tsq)

    # -- background ----------------------------------------------------------

    async def run(self) -> None:
        from app.algo import fno

        await asyncio.to_thread(self.sweep)
        await asyncio.to_thread(self.replay_today)
        while True:
            await asyncio.sleep(15)
            try:
                now = time.time()
                day = int((now + IST) // 86400)
                if self._day != day:                  # new session: futures may have rolled
                    self._day = day
                    try:
                        await fno.ensure_master()
                    except Exception as e:  # noqa: BLE001
                        log.warning("ticks.fno_master_failed", error=str(e)[:200])
                    self._resolved.clear()
                await asyncio.to_thread(self.flush)
                if ((now + IST) % 86400) // 60 >= COMPACT_AFTER_MIN and self._compacted_day != day:
                    self._compacted_day = day
                    await asyncio.to_thread(self.compact, day)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — recording must never take the bot down
                log.exception("ticks.flush_failed")

    def _raw_path(self, key: str, day: int, ext: str) -> Path:
        d = time.strftime("%Y%m%d", time.gmtime(day * 86400))
        return self.root / "raw" / key / f"{d}.{ext}"

    def flush(self) -> int:
        with self._lock:
            buf, self._buf = self._buf, {}
        n = 0
        for key, rows in buf.items():
            by_day: dict[int, list[tuple]] = {}
            for r in rows:
                by_day.setdefault(int((r[1] + IST) // 86400), []).append(r)
            for day, rs in by_day.items():
                p = self._raw_path(key, day, "csv")
                p.parent.mkdir(parents=True, exist_ok=True)
                new = not p.exists()
                with p.open("a", newline="", encoding="utf-8") as fh:
                    w = csv.writer(fh)
                    if new:
                        w.writerow(RAW_COLS)
                    w.writerows(rs)
                n += len(rs)
        return n

    def sweep(self) -> None:
        """Compact the raw CSV of any EARLIER day (the bot was down at that
        day's close): replay it through the same classifier, then compact."""
        today = int((time.time() + IST) // 86400)
        raw = self.root / "raw"
        days = {calendar.timegm(time.strptime(f.stem, "%Y%m%d")) // 86400
                for f in raw.glob("*/*.csv")} if raw.is_dir() else set()
        for d in sorted(days - {today}):
            self.replay_today(d)
            self.compact(d)

    def replay_today(self, day: Optional[int] = None) -> None:
        """Rebuild today's classification state and minute flow from the raw
        CSV after a restart — the same rule, so nothing drifts."""
        day = int((time.time() + IST) // 86400) if day is None else day
        if not (self.root / "raw").is_dir():
            return
        for kdir in (self.root / "raw").iterdir():
            p = self._raw_path(kdir.name, day, "csv")
            if not p.exists():
                continue
            with p.open(encoding="utf-8") as fh:
                rdr = csv.reader(fh)
                next(rdr, None)
                with self._lock:
                    for r in rdr:
                        try:
                            row = (float(r[0]), int(float(r[1])), float(r[2]), float(r[3]), 0.0, 0,
                                   *(float(x) for x in r[6:12]))
                        except (ValueError, IndexError):
                            continue
                        self._classify(kdir.name, row)
        log.info("ticks.replayed", symbols=len(self._minutes))

    def compact(self, day: int) -> None:
        """After the close: raw CSV -> parquet, the day's minute flow and
        footprint -> monthly parquet. Then today's memory is released."""
        import duckdb

        con = duckdb.connect()
        ym = time.strftime("%Y%m", time.gmtime(day * 86400))
        with self._lock:
            minutes, fp = self._minutes, self._fp
            self._minutes, self._fp, self._state, self.stats = {}, {}, {}, {}
        for key in set(minutes) | set(fp):
            csvp = self._raw_path(key, day, "csv")
            if csvp.exists():
                pq = self._raw_path(key, day, "parquet")
                con.execute(f"COPY (SELECT * FROM read_csv('{csvp.as_posix()}', header=true)) "
                            f"TO '{pq.as_posix()}' (FORMAT parquet)")
                csvp.unlink()
            rows = [(t, *v) for t, v in sorted(minutes.get(key, {}).items()) if (t + IST) // 86400 == day]
            self._append(con, self.root / "flow" / key / f"{ym}.parquet", rows,
                         "minute BIGINT, buy DOUBLE, sell DOUBLE, trades INTEGER, ticks INTEGER, "
                         "tot_buy DOUBLE, tot_sell DOUBLE", "minute")
            fps = [(t, p, b, s) for (t, p), (b, s) in sorted(fp.get(key, {}).items()) if (t + IST) // 86400 == day]
            self._append(con, self.root / "fp" / key / f"{ym}.parquet", fps,
                         "minute BIGINT, price DOUBLE, buy DOUBLE, sell DOUBLE", "minute, price")
        log.info("ticks.compacted", day=day, symbols=len(minutes))

    @staticmethod
    def _append(con, path: Path, rows: list[tuple], schema: str, keys: str) -> None:
        if not rows:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        con.execute(f"CREATE OR REPLACE TEMP TABLE _n ({schema})")
        # bulk-load through a temp CSV: executemany runs ~2k rows/s
        import tempfile

        cols = [c.split()[0] for c in schema.split(",")]
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as fh:
            csv.writer(fh).writerows(rows)
        try:
            con.execute(f"INSERT INTO _n SELECT * FROM read_csv('{Path(fh.name).as_posix()}', header=false, "
                        f"columns={{{', '.join(repr(c) + ': ' + repr(t.split()[1]) for c, t in zip(cols, schema.split(','))) }}})")
        finally:
            os.unlink(fh.name)
        src = "SELECT * FROM _n"
        if path.exists():
            src = (f"SELECT * FROM read_parquet('{path.as_posix()}') WHERE ({keys}) NOT IN "
                   f"(SELECT {keys} FROM _n) UNION ALL SELECT * FROM _n")
        tmp = path.with_suffix(".parquet.tmp")
        con.execute(f"COPY ({src} ORDER BY {keys}) TO '{tmp.as_posix()}' (FORMAT parquet)")
        tmp.replace(path)

    # -- reads -----------------------------------------------------------------

    def minute_flow(self, key: str, start: int, end: int) -> dict[int, tuple[float, float]]:
        """{minute_ts: (buy, sell)} from stored days plus today's memory."""
        out: dict[int, tuple[float, float]] = {}
        d = self.root / "flow" / key
        if d.is_dir():
            import duckdb

            files = ", ".join(f"'{p.as_posix()}'" for p in sorted(d.glob("*.parquet")))
            if files:
                for t, b, s in duckdb.connect().execute(
                        f"SELECT minute, buy, sell FROM read_parquet([{files}]) WHERE minute >= ? AND minute < ?",
                        [int(start), int(end)]).fetchall():
                    out[int(t)] = (b, s)
        with self._lock:
            for t, m in self._minutes.get(key, {}).items():
                if start <= t < end:
                    out[t] = (m[0], m[1])
        return out

    def minutes_today(self, key: str) -> list[list[float]]:
        with self._lock:
            return [[t, *m] for t, m in sorted(self._minutes.get(key, {}).items())]

    def footprint(self, key: str, start: int, end: int) -> list[tuple]:
        out: list[tuple] = []
        d = self.root / "fp" / key
        if d.is_dir():
            import duckdb

            files = ", ".join(f"'{p.as_posix()}'" for p in sorted(d.glob("*.parquet")))
            if files:
                out = duckdb.connect().execute(
                    f"SELECT minute, price, buy, sell FROM read_parquet([{files}]) "
                    "WHERE minute >= ? AND minute < ? ORDER BY minute, price", [int(start), int(end)]).fetchall()
        with self._lock:
            out += sorted((t, p, b, s) for (t, p), (b, s) in self._fp.get(key, {}).items() if start <= t < end)
        return out

    def status(self) -> dict[str, Any]:
        days: dict[str, int] = {}
        size = 0
        raw = self.root / "raw"
        if raw.is_dir():
            for kdir in raw.iterdir():
                files = list(kdir.iterdir())
                days[kdir.name] = len({f.stem for f in files})
                size += sum(f.stat().st_size for f in files)
        with self._lock:
            stats = {k: dict(v) for k, v in self.stats.items()}
        return {"enabled": bool(self.config.get("enabled")), "config_symbols": self.config.get("symbols", []),
                "strategy_symbols": self.extra, "subscribed": self.symbols(), "today": stats,
                "days_recorded": days, "disk_mb": round(size / 1e6, 1)}


RECORDER: Optional[TickRecorder] = None


def recorder() -> TickRecorder:
    """The process-wide recorder (created on first use)."""
    global RECORDER
    if RECORDER is None:
        RECORDER = TickRecorder()
    return RECORDER


if __name__ == "__main__":    # self-check: python -m app.algo.ticks
    import tempfile

    r = TickRecorder(Path(tempfile.mkdtemp()))
    t0 = 1791100800 + 9 * 3600       # some IST morning
    frames = [(100.0, 1000, 99.9, 100.1), (100.1, 1100, 100.0, 100.1),   # +100 at the ask -> buy
              (100.0, 1150, 100.0, 100.1), (100.0, 1150, 100.0, 100.1)]  # +50 at the bid -> sell; no trade
    for k, (ltp, vol, bid, ask) in enumerate(frames):
        r.on_tick("NSE:SBIN-EQ", {"ltp": ltp, "vol_traded_today": vol, "bid_price": bid, "ask_price": ask,
                                  "exch_feed_time": t0 + k})
    (m,) = r._minutes["SBIN"].values()
    assert m[0] == 100 and m[1] == 50 and m[2] == 2 and m[3] == 4, m
    assert flow_key("NSE:NIFTY26OCTFUT") == "NIFTY-FUT" and flow_key("BSE:SENSEX26OCTFUT") == "BSE_SENSEX-FUT"
    print("ticks ok")
