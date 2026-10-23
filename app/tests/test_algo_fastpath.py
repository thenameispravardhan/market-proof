"""The backtest's lean paths give the same answers as the plain ones: one
parquet read for every timeframe, candles as typed arrays, expiries worked out
once per day, a lazily merged event stream and per-symbol indicator caches
dropped as soon as their signals are built."""
from __future__ import annotations

import asyncio
import math
from array import array

import duckdb
import pytest

from app.algo import data, engine, fno

DAY0 = 1735689600 - 19800 + 33300   # 2025-01-01 09:15 IST


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Two symbols of 1-minute candles over 6 sessions, the second with a
    duplicate minute (a base export and an increment overlapping)."""
    from app.services import warehouse_prices

    monkeypatch.setattr(warehouse_prices, "CANDLES", tmp_path)
    con = duckdb.connect()
    for k, name in enumerate(("AAA", "BBB")):
        con.execute(f"""COPY (
            SELECT {DAY0} + d * 86400 + m * 60 AS ts, to_timestamp({DAY0} + d * 86400 + m * 60)::TIMESTAMP AS datetime,
                   100 + {k} + sin(d * 375 + m) * 3 AS open, 101 + {k} + sin(d * 375 + m) * 3 + (m % 7) * 0.1 AS high,
                   99 + {k} + sin(d * 375 + m) * 3 - (m % 5) * 0.1 AS low, 100 + {k} + cos(d * 375 + m) * 3 AS close,
                   (1000 + m * 3 + d)::BIGINT AS volume
            FROM range(6) r1(d), range(375) r2(m)) TO '{(tmp_path / f"{name}.parquet").as_posix()}' (FORMAT parquet)""")
    (tmp_path / "_inc" / "BBB").mkdir(parents=True)
    con.execute(f"""COPY (SELECT * FROM read_parquet('{(tmp_path / "BBB.parquet").as_posix()}') WHERE ts = {DAY0 + 600})
                    TO '{(tmp_path / "_inc" / "BBB" / "202501.parquet").as_posix()}' (FORMAT parquet)""")
    return tmp_path


def test_one_read_gives_every_timeframe_as_the_per_timeframe_loads_do(store):
    tfs = [5, 1, 60, 45, 1440]
    for key in ("AAA", "BBB"):
        frames = data.load_frames(key, tfs, DAY0 - 86400, DAY0 + 6 * 86400)
        assert len(frames) == len(tfs)
        for tf, got in zip(tfs, frames):
            want = data.load(key, tf, DAY0 - 86400, DAY0 + 6 * 86400)
            assert {k: list(v) if isinstance(v, array) else v for k, v in got.items()} == \
                   {k: list(v) if isinstance(v, array) else v for k, v in want.items()}
            assert got["tf_s"] == tf * 60
            assert isinstance(got["c"], array) and got["c"].typecode == "d" and got["t"].typecode == "q"
    five = data.load_frames("AAA", [5], DAY0, DAY0 + 86400)[0]
    assert len(five["t"]) == 75 and five["t"][1] - five["t"][0] == 300              # 09:15, 09:20, …
    assert data.load_frames("NOPE", [5], DAY0, DAY0 + 86400) is None
    assert len(data.load("BBB", 1, DAY0, DAY0 + 86400)["t"]) == 375                  # the duplicate minute counted once


def test_assemble_reads_through_one_database_and_closes_it(store, monkeypatch):
    opened = []
    real = data.backtest_db

    def tracked():
        con = real()
        opened.append(con)
        return con

    async def ensure(fy, a, b):
        return {"symbol": fy, "key": data.store_key(fy), "note": None, "coverage": data.coverage(data.store_key(fy))}

    monkeypatch.setattr(data, "backtest_db", tracked)
    monkeypatch.setattr(data, "ensure", ensure)
    spec = engine.normalize({"symbols": ["NSE:AAA-EQ", "NSE:BBB-EQ"], "timeframe": 5, "direction": "long",
                             "entry_long": {"logic": "AND", "conditions": [
                                 {"left": {"ind": "EMA", "params": {"period": 5}}, "op": "crosses_above",
                                  "right": {"ind": "EMA", "params": {"period": 20}, "tf": 15}}]}})
    out, notes, _ = asyncio.run(data.assemble(spec, DAY0 + 2 * 86400, DAY0 + 6 * 86400))
    assert set(out) == {"NSE:AAA-EQ", "NSE:BBB-EQ"} and not notes
    assert set(out["NSE:AAA-EQ"]["htf"]) == {15}
    assert len(opened) == 1
    with pytest.raises(duckdb.ConnectionException):
        opened[0].execute("SELECT 1")                          # closed once the backtest has its candles
    assert data._active.get() is None


def test_expiry_series_is_expiry_after_per_candle():
    days = [d for d in range(20089, 20089 + 200) if (d + 3) % 7 < 5 and d % 23]     # weekdays, a few holidays
    td = frozenset(days)
    for tf in (300, 3600, None):
        times = [d * 86400 - 19800 + (0 if tf is None else 33300 + k * tf)
                 for d in days for k in range(1 if tf is None else 22500 // tf)]
        for name, exch, kind in (("NIFTY", "NSE", "weekly"), ("SENSEX", "BSE", "weekly"), ("SBIN", "NSE", "monthly")):
            for which in ("current", "next"):
                assert fno.expiry_series(name, exch, kind, which, times, td) == \
                    [fno.expiry_after(name, exch, kind, which, t, td) for t in times]


def test_events_merge_in_sorted_order():
    data_ = {"A": {"t": [10, 20, 30, 40]}, "B": {"t": array("q", [5, 20, 35])}, "C": {"t": [30, 10, 20]}}
    syms = list(data_)
    prio = {(1, 1): 2, (0, 1): 1}
    want = sorted((t, prio.get((k, i), 0), k, i) for k, s in enumerate(syms) for i, t in enumerate(data_[s]["t"]))
    assert list(engine._events(syms, data_, prio)) == want
    assert list(engine._events(syms, data_, {})) == sorted(
        (t, 0, k, i) for k, s in enumerate(syms) for i, t in enumerate(data_[s]["t"]))


def _wave(n, phase, start=DAY0):
    closes = [100 + 5 * math.sin(i / 6 + phase) for i in range(n)]
    opens = [closes[0]] + closes[:-1]
    return {"t": [start + (i // 75) * 86400 + (i % 75) * 300 for i in range(n)], "o": opens,
            "h": [max(o, c) + 0.3 for o, c in zip(opens, closes)], "l": [min(o, c) - 0.3 for o, c in zip(opens, closes)],
            "c": closes, "v": [1000] * n, "tf_s": 300}


def test_keep_caches_frees_the_others_and_changes_no_trade():
    spec = engine.normalize({"symbols": ["A", "B", "C"], "timeframe": 5, "direction": "both",
                             "entry_long": {"logic": "AND", "conditions": [
                                 {"left": {"ind": "EMA", "params": {"period": 5}}, "op": "crosses_above", "right": {"ind": "EMA", "params": {"period": 13}}}]},
                             "entry_short": {"logic": "AND", "conditions": [
                                 {"left": {"ind": "EMA", "params": {"period": 5}}, "op": "crosses_below", "right": {"ind": "EMA", "params": {"period": 13}}}]},
                             "stop_loss": {"type": "atr", "value": 1.5, "atr_period": 14}, "target": {"type": "rr", "value": 2}})
    mk = lambda: {s: _wave(300, k) for k, s in enumerate("ABC")}   # noqa: E731
    all_caches: dict = {}
    full = engine.run(spec, mk(), all_caches)
    kept: dict = {}
    lean = engine.run(spec, mk(), kept, keep_caches={"B"})
    assert lean["trades"] == full["trades"] and full["trades"]
    assert set(all_caches) == {"A", "B", "C"} and set(kept) == {"B"} and kept["B"]
    ranked = {**spec, "rank": {"by": {"ind": "RSI", "params": {"period": 14}}, "order": "desc", "top": 1}}
    kept = {}
    assert engine.run(ranked, mk(), kept, keep_caches={"A"})["trades"] == engine.run(ranked, mk(), {})["trades"]
    assert set(kept) == {"A"}
