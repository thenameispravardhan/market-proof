"""Fill the price/volume columns for announcements that do not have them yet.

A filing enters the warehouse the moment it is collected, with px_*/vol_*/day_*
NULL and price_status='pending'. This walks those rows and fills them from the
1-minute candle store, exactly as the historical dataset was built:

    px_pre / vol_pre     the minute before the announcement
    px_t0 / vol_t0       the minute containing it (its CLOSE, so never lookahead)
    px_1m .. px_60m      1,2,3..15,20,30,45,60 minutes after
    day_high/low/volume  for the session those prices come from
    ret_/mkt_/adj_       market-adjusted returns against NIFTY 50

Work is grouped by symbol so each candle file is opened once, and results are
written back in a single UPDATE per symbol. Peak memory is one symbol's candles
(a few MB), not the dataset.

    python -m app.services.warehouse_prices              # fill what is pending
    python -m app.services.warehouse_prices --limit 500  # a slice
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from app.logging_config import get_logger
from app.services.warehouse_store import TABLE, connect

log = get_logger(__name__)

ROOT = Path(__file__).resolve().parents[2]
CANDLES = ROOT / "AIdataset" / "stockdata"
NIFTY = CANDLES / "NIFTY50-INDEX.parquet"

OFFSETS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 20, 30, 45, 60)
HORIZONS = (5, 15, 30, 60)
_nifty_cache: pd.Series | None = None


def _series(path: Path) -> tuple[pd.Series, pd.Series] | None:
    """(close, volume) indexed by minute timestamp."""
    if not path.exists():
        return None
    d = pd.read_parquet(path, columns=["datetime", "close", "volume", "high", "low"])
    d["datetime"] = pd.to_datetime(d["datetime"]).dt.as_unit("ns")
    d = d.drop_duplicates("datetime").set_index("datetime").sort_index()
    return d


def _nifty() -> pd.Series:
    global _nifty_cache
    if _nifty_cache is None:
        d = _series(NIFTY)
        _nifty_cache = d["close"] if d is not None else pd.Series(dtype=float)
    return _nifty_cache


def fill_symbol(con, symbol: str, rows: pd.DataFrame) -> int:
    """Fill every pending row for one symbol. Returns rows updated."""
    d = _series(CANDLES / f"{symbol}.parquet")
    if d is None or d.empty:
        con.execute(f"UPDATE {TABLE} SET price_status='no_candles' WHERE uid = ANY(?)",
                    [rows.uid.tolist()])
        return 0

    close, vol, nif = d["close"], d["volume"], _nifty()
    updates = []
    for r in rows.itertuples():
        # The anchor is the minute bucket holding the announcement; px_t0 is that
        # minute's CLOSE, which lands after the filing — never lookahead.
        anchor = pd.Timestamp(r.announced_at).floor("min")
        if anchor < close.index[0] or anchor > close.index[-1]:
            continue
        rec: dict = {"uid": r.uid, "anchor_time": anchor}
        idx = close.index.get_indexer([anchor], method="nearest")[0]
        if abs((close.index[idx] - anchor).total_seconds()) > 300:
            continue
        base = float(close.iloc[idx])
        rec["px_t0"], rec["vol_t0"] = base, float(vol.iloc[idx])
        if idx > 0:
            rec["px_pre"], rec["vol_pre"] = float(close.iloc[idx - 1]), float(vol.iloc[idx - 1])
        for off in OFFSETS:
            j = idx + off
            if j < len(close) and (close.index[j] - anchor).total_seconds() <= off * 60 + 300:
                rec[f"px_{off}m"], rec[f"vol_{off}m"] = float(close.iloc[j]), float(vol.iloc[j])
        day = d[d.index.date == anchor.date()]
        if not day.empty:
            rec["day_high"] = float(day["high"].max())
            rec["day_low"] = float(day["low"].min())
            rec["day_volume"] = float(day["volume"].sum())
        # Market-adjusted returns over the identical window.
        nb = _at(nif, anchor)
        for h in HORIZONS:
            px = rec.get(f"px_{h}m")
            if px is None or not base:
                continue
            ret = (px - base) / base * 100
            mkt = 0.0
            na = _at(nif, anchor + pd.Timedelta(minutes=h))
            if nb and na:
                mkt = (na - nb) / nb * 100
            rec[f"ret_{h}m"], rec[f"mkt_{h}m"], rec[f"adj_{h}m"] = ret, mkt, ret - mkt
        a30 = rec.get("adj_30m")
        rec["usable"] = a30 is not None
        rec["mover_1_5"] = abs(a30) > 1.5 if a30 is not None else None
        rec["mover_3"] = abs(a30) > 3.0 if a30 is not None else None
        rec["price_status"] = "filled"
        rec["price_source"] = "candles"
        updates.append(rec)

    if not updates:
        con.execute(f"UPDATE {TABLE} SET price_status='no_candles' WHERE uid = ANY(?)",
                    [rows.uid.tolist()])
        return 0

    u = pd.DataFrame(updates)
    cols = [c for c in u.columns if c != "uid"]
    con.register("_upd", u)
    con.execute(f"UPDATE {TABLE} t SET {', '.join(f'{c}=u.{c}' for c in cols)} "
                f"FROM _upd u WHERE t.uid = u.uid")
    con.unregister("_upd")
    return len(u)


def fill(limit: int = 0) -> dict:
    con = connect()
    q = (f"SELECT uid, symbol, announced_at FROM {TABLE} "
         "WHERE price_status='pending' AND symbol IS NOT NULL "
         "ORDER BY symbol")
    if limit:
        q += f" LIMIT {limit}"
    pending = con.execute(q).fetch_df()
    if pending.empty:
        return {"pending": 0, "filled": 0}

    total = 0
    symbols = pending.symbol.unique()
    for i, sym in enumerate(symbols, 1):
        try:
            total += fill_symbol(con, sym, pending[pending.symbol == sym])
        except Exception as e:  # noqa: BLE001 — one bad symbol must not stop the run
            log.warning("warehouse_prices.symbol_failed", symbol=sym, error=str(e)[:120])
        if i % 200 == 0:
            print(f"   {i}/{len(symbols)} symbols, {total:,} rows filled", flush=True)
    out = {"pending": len(pending), "symbols": len(symbols), "filled": total}
    log.info("warehouse_prices.done", **out)
    return out


def _at(s: pd.Series, ts: pd.Timestamp) -> float | None:
    if s.empty:
        return None
    i = s.index.get_indexer([ts], method="nearest")[0]
    if abs((s.index[i] - ts).total_seconds()) > 300:
        return None
    return float(s.iloc[i])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    r = fill(a.limit)
    for k, v in r.items():
        print(f"  {k:<12} {v:,}" if isinstance(v, int) else f"  {k:<12} {v}")
    con = connect()
    print("\nprice_status now:",
          dict(con.execute(f"select price_status, count(*) from {TABLE} group by 1").fetchall()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
