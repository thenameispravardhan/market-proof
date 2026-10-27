"""Is app/algo/_native.so safe to use? It must give the Python engine's answers exactly.

Runs ~80 seeded strategies (every option native.cpp's run_loop covers) over
synthetic candles through engine.run twice — Python loop, then C++ loop — and
compares the whole result (stats, per-symbol, every trade) as JSON text, plus
the Monte Carlo. deploy/update.sh runs it after building the .so and deletes
the .so when it fails, so a wrong build can never answer a backtest.

    python -m app.algo.native_check      # exit 0 = identical (or no .so to check)
"""
from __future__ import annotations

import ctypes
import json
import math
import random
import struct
import sys
from array import array
from typing import Any, Optional

from app.algo import engine

DAY0 = 1735702500                 # 2025-01-01 09:15 IST


def candles(seed: int, days: int = 12, tf: int = 5) -> dict:
    """A random walk on the 0.05 grid, `days` sessions of `tf`-minute candles (weekends skipped).
    Every third session stops at ~13:00 (a data gap), so a position can be left open
    past its square-off and close EOD on the next session's first candle."""
    rnd = random.Random(seed)
    t, o, h, l, c, v = array("q"), array("d"), array("d"), array("d"), array("d"), array("q")
    px, day = 100.0 + seed * 7, 0
    for n in range(days):
        while (DAY0 // 86400 + day + 3) % 7 >= 5:                 # Saturday / Sunday
            day += 1
        px = max(5.0, px + rnd.gauss(0, 1.5))                       # an overnight gap: opens that jump a trigger
        for b in range((375 if (n + seed) % 3 else 225) // tf):
            op = max(1.0, px + rnd.gauss(0, 0.25 if b else 0.8))     # opens jump the last close a little
            path = [op] + [max(1.0, op + rnd.gauss(0, 0.35) * (k + 1)) for k in range(3)]
            cl = round(path[-1] * 20) / 20
            t.append(DAY0 + day * 86400 + b * tf * 60)
            o.append(round(op * 20) / 20)
            h.append(round(max(path + [cl]) * 20) / 20 + 0.05 * rnd.randrange(3))
            l.append(round(min(path + [cl]) * 20) / 20 - 0.05 * rnd.randrange(3))
            c.append(cl)
            v.append(rnd.randrange(1000, 50000))
            px = cl
        day += 1
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v, "tf_s": tf * 60}


def specs(n: int = 120, seed: int = 11) -> list[dict]:
    """`n` valid strategies with seeded, independent choices of every loop option."""
    rnd = random.Random(seed)
    ema = lambda p: {"ind": "EMA", "params": {"period": p}}  # noqa: E731
    up = {"logic": "AND", "conditions": [{"left": ema(5), "op": "crosses_above", "right": ema(13)}]}
    dn = {"logic": "AND", "conditions": [{"left": ema(5), "op": "crosses_below", "right": ema(13)}]}
    pick = rnd.choice
    out: list[dict] = []
    while len(out) < n:
        direction = pick(["long", "short", "both"])
        raw = {
            "symbols": ["NSE:AAA-EQ", "NSE:BBB-EQ", "NSE:CCC-EQ"], "timeframe": pick([5, 5, 5, 1440]),
            "direction": direction,
            "instrument": {**engine.DEFAULT_SPEC["instrument"],
                           **pick([{"type": "equity"}, {"type": "equity"}, {"type": "future", "expiry": "current"},
                                   {"type": "future", "expiry": "next"}])},
            "mtm": pick([{"stop": None, "target": None, "trail_start": None, "trail_gap": None},
                         {"stop": None, "target": None, "trail_start": None, "trail_gap": None},
                         {"stop": 400, "target": None, "trail_start": None, "trail_gap": None},
                         {"stop": None, "target": 600, "trail_start": None, "trail_gap": None},
                         {"stop": 500, "target": 900, "trail_start": 300, "trail_gap": 150},
                         {"stop": None, "target": None, "trail_start": 200, "trail_gap": 100}]),
            "scale_out": pick([None, None, [{"type": "pct", "value": 0.4, "qty_pct": 50}],
                               [{"type": "points", "value": 0.8, "qty_pct": 30},
                                {"type": "pct", "value": 0.9, "qty_pct": 40}]]),
            "entry_long": up, "entry_short": dn,
            "exit_long": pick([None, dn]), "exit_short": pick([None, up]),
            "stop_loss": pick([None, {"type": "pct", "value": 0.6}, {"type": "points", "value": 1.5},
                               {"type": "atr", "value": 1.5, "atr_period": 14}]),
            "target": pick([None, {"type": "pct", "value": 1.2}, {"type": "rr", "value": 2},
                            {"type": "atr", "value": 3, "atr_period": 10}]),
            "trailing": pick([None, {"type": "pct", "value": 0.4, "activate": 0.3},
                              {"type": "points", "value": 1.0, "activate": 0}]),
            "breakeven": pick([None, {"type": "pct", "value": 0.4}]),
            "entry_order": pick([{"type": "market"}, {"type": "pullback", "offset_pct": 0.2, "valid_bars": 3},
                                 {"type": "breakout", "offset_pct": 0.1, "valid_bars": 2}]),
            "session": {"start": pick(["09:20", "10:00"]), "end": pick(["15:00", "14:00"]),
                        "square_off": pick(["15:15", "15:00", None]), **pick([{}, {"days": [1, 2, 3, 4]}])},
            "max_trades_per_day": pick([1, 3, 6]), "cooldown_bars": pick([0, 3]), "max_bars": pick([None, 12]),
            "daily": pick([{"max_loss": None, "max_profit": None}, {"max_loss": 300, "max_profit": 450}]),
            "sizing": pick([{"mode": "qty", "value": 10}, {"mode": "qty", "value": 700}, {"mode": "risk", "value": 500},
                            {"mode": "pct_equity", "value": 30}, {"mode": "amount", "value": 20000}]),
            "portfolio": {"capital": 100000, "leverage": pick([1.0, 4.0]), "max_positions": pick([1, 2, 10]),
                          "compounding": pick([True, False]), "max_position_pct": pick([None, 40])},
            "costs": {"slippage_pct": pick([0, 0.05]), "charges": pick([True, False])},
        }
        try:
            s = engine.normalize(raw)
        except (ValueError, TypeError, KeyError):
            continue                                  # an invalid combination (e.g. rr target, no stop)
        out.append(s)
    # Futures held to expiry: carried, nothing else closes them, so they leave through the
    # 15:15 expiry-day exit (a full session) or settle() (a short one, past the expiry).
    for direction, expiry in (("long", "current"), ("short", "next"), ("both", "current"), ("both", "next")):
        out.append(engine.normalize({
            "symbols": ["NSE:AAA-EQ", "NSE:BBB-EQ", "NSE:CCC-EQ"], "timeframe": 5, "direction": direction,
            "entry_long": up, "entry_short": dn, "exit_long": None, "exit_short": None,
            "instrument": {**engine.DEFAULT_SPEC["instrument"], "type": "future", "expiry": expiry},
            "stop_loss": None, "target": None, "session": {"start": "09:20", "end": "15:00", "square_off": None},
            "max_trades_per_day": 1, "sizing": {"mode": "qty", "value": 2},
            "portfolio": {"capital": 10000000, "leverage": 1.0, "max_positions": 3, "compounding": False},
        }))
    return out


def flat_candles(seed: int) -> dict:
    """candles() with flat stretches and no volume in them: stdev 0 (Bollinger %B / width
    divide by zero -> None), RSI's down-average 0 (-> 100), VWAP over no volume (-> None)."""
    d = candles(seed)
    for i in range(len(d["t"])):
        if (i // 40) % 4 == 3:
            px = d["c"][i - 1] if i else d["c"][0]
            d["o"][i] = d["h"][i] = d["l"][i] = d["c"][i] = px
            d["v"][i] = 0
    return d


def _operand(rnd: random.Random, depth: int = 0) -> dict:
    kind = rnd.random()
    if kind < 0.12:
        return {"value": rnd.choice([0, 1, 50, 70, 100.5, -2])}
    if kind < 0.24 and depth < 2:
        return {"math": rnd.choice(["+", "-", "*", "/", "pct_diff"]), "a": _operand(rnd, depth + 1),
                "b": _operand(rnd, depth + 1), **rnd.choice([{}, {"mult": 2}, {"add": -1.5}])}
    name = rnd.choice(["PRICE", "SMA", "EMA", "RMA", "WMA", "RSI", "STDDEV", "HIGHEST", "LOWEST", "ROC", "MOMENTUM",
                       "VOLUME_SMA", "ATR", "VWAP", "MACD", "BBANDS"])
    params: dict = {}
    if name in ("SMA", "EMA", "RMA", "WMA", "RSI", "STDDEV", "HIGHEST", "LOWEST", "ROC", "MOMENTUM", "VOLUME_SMA", "ATR"):
        params["period"] = rnd.choice([1, 2, 3, 5, 9, 14, 21, 30])
    if name in ("PRICE", "SMA", "EMA", "RMA", "WMA", "RSI", "STDDEV", "HIGHEST", "LOWEST", "ROC", "MOMENTUM", "MACD", "BBANDS"):
        params["source"] = rnd.choice(["close", "open", "high", "low", "hl2", "hlc3", "ohlc4", "volume", "range"])
    if name == "MACD":
        params.update(fast=rnd.choice([3, 12]), slow=rnd.choice([8, 26]), signal=rnd.choice([2, 9]))
    if name == "BBANDS":
        params.update(period=rnd.choice([5, 20]), multiplier=rnd.choice([1.0, 2.0, 2.5]))
    op: dict = {"ind": name, "params": params}
    fields = engine.ind.REGISTRY[name][2]
    if len(fields) > 1:
        op["field"] = rnd.choice(fields)
    if rnd.random() < 0.3:
        op["offset"] = rnd.choice([1, 2, 5])
    if rnd.random() < 0.2:
        op.update(rnd.choice([{"mult": 1.01}, {"add": 3}, {"mult": 0.5, "add": -1}]))
    return op


def _group(rnd: random.Random, depth: int = 0) -> dict:
    conds = []
    for _ in range(rnd.choice([1, 1, 2, 3])):
        if depth < 1 and rnd.random() < 0.2:
            conds.append(_group(rnd, depth + 1))
            continue
        op = rnd.choice(engine.OPS)
        c: dict = {"left": _operand(rnd), "op": op}
        if op in ("rising", "falling"):
            c["right"] = {"value": rnd.choice([1, 2, 3])}
        else:
            c["right"] = _operand(rnd)
        if op in ("between", "outside"):
            c["right2"] = _operand(rnd)
        if rnd.random() < 0.25:
            c[rnd.choice(["for_bars", "within_bars"])] = rnd.choice([2, 3, 5])
        if rnd.random() < 0.15:
            c["negate"] = True
        conds.append(c)
    return {"logic": rnd.choice(["AND", "OR"]), "conditions": conds}


def _native_series(d: dict, op: dict) -> Optional[list[float]]:
    """One operand's series from native.cpp (NaN = None), via a one-condition program."""
    lib = engine._native_lib()
    lib.eval_series.restype = ctypes.c_int
    lib.eval_series.argtypes = [ctypes.c_void_p] * 6 + [ctypes.c_int64, ctypes.c_void_p, ctypes.c_void_p,
                                                         ctypes.c_int64, ctypes.c_int64, ctypes.c_void_p]
    spec = {"direction": "long", "entry_long": {"conditions": [{"left": op, "op": ">", "right": {"value": 0}}]},
            "exit_long": None, "entry_short": None, "exit_short": None}
    prog = engine._compile_signals(spec, None)
    if prog is None:
        return None
    ints, dbl, roots = prog
    keep = [array("q", d["t"]), array("d", d["o"]), array("d", d["h"]), array("d", d["l"]), array("d", d["c"]),
            array("d", d["v"]), array("q", ints), array("d", dbl)]
    n = len(d["c"])
    out = (ctypes.c_double * n)()
    if lib.eval_series(*(a.buffer_info()[0] for a in keep[:6]), n, keep[6].buffer_info()[0],
                       keep[7].buffer_info()[0], len(ints) // 8, ints[roots[0] * 8 + 1], out) != 0:
        return None
    return list(out)


def _bits(x: Any) -> Optional[bytes]:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else struct.pack("<d", float(x))


def _operands(g: Optional[dict]):
    for c in (g or {}).get("conditions") or []:
        if "conditions" in c:
            yield from _operands(c)
        else:
            yield from (c[k] for k in ("left", "right", "right2") if isinstance(c.get(k), dict))


def check_signals(n: int = 300, seed: int = 5) -> list[str]:
    """engine.signals() vs native.cpp's eval_signals on random condition trees — every
    supported indicator, field, source, offset, shaping, math and operator, with
    modifiers and nested groups — over normal and degenerate (flat, no-volume) candles."""
    rnd = random.Random(seed)
    bad, covered, series_checked = [], 0, [0]
    datasets = [candles(7), flat_candles(8)]
    for k in range(n):
        spec = {"direction": "both", "entry_long": _group(rnd), "exit_long": _group(rnd),
                "entry_short": _group(rnd), "exit_short": rnd.choice([None, _group(rnd)])}
        prog = engine._compile_signals(spec, None)
        if prog is None:
            bad.append(f"signals {k}: not compiled (should be covered)")
            continue
        covered += 1
        for j, d in enumerate(datasets):
            try:
                py = engine.signals(d, spec, {})
            except (ZeroDivisionError, TypeError, ValueError) as e:      # Python can't evaluate it either
                bad.append(f"signals {k}/{j}: python raised {e!r}")
                continue
            cc = engine._native_signals(d, prog)
            for side, vals in py.items():
                got = [bool(x) for x in cc[side]]
                if [bool(x) for x in vals] != got:
                    i = next(i for i, (a, b) in enumerate(zip(vals, got)) if bool(a) != b)
                    bad.append(f"signals {k}/{j} {side} differs at candle {i}: {json.dumps(spec)[:300]}")
                    break
            # every operand's values, bit for bit: a 1-ULP drift that flips no flag here would elsewhere
            for op in (o for side in ("entry_long", "exit_long", "entry_short", "exit_short")
                       for o in _operands(spec[side])):
                ser_cc = _native_series(d, op)
                ser_py = engine.series(d, op, {})
                series_checked[0] += 1
                if ser_cc is None or [_bits(x) for x in ser_py] != [_bits(x) for x in ser_cc]:
                    i = next((i for i, (a, b) in enumerate(zip(ser_py, ser_cc or [])) if _bits(a) != _bits(b)), -1)
                    bad.append(f"series {k}/{j} differs at candle {i}: {json.dumps(op)[:200]}")
                    break
    print(f"native_check signals: {covered} condition sets x {len(datasets)} datasets, "
          f"{series_checked[0]} operand series compared bit for bit, {len(bad)} mismatches")
    return bad


def _result(spec: dict, native: bool, variant: int = 0) -> str:
    """variant 1: the third symbol has no price grid (MCX: snap() leaves prices alone);
    variant 2: candles carry their own close times (`tc`, as volume / turnover candles do)."""
    engine.NATIVE = native
    try:
        names = ("NSE:AAA-EQ", "NSE:BBB-EQ", "MCX:CCC" if variant == 1 else "NSE:CCC-EQ")
        if engine.multi_day(spec["timeframe"]):          # daily candles: one per full session, closing 15:30
            data = {x: candles(seed, days=150, tf=375) for seed, x in enumerate(names)}
            for d in data.values():
                d["tf_s"] = 86400
                d["tc"] = array("q", (t - (t + engine.IST) % 86400 + 55800 for t in d["t"]))
        else:                                            # futures: long enough for two monthly expiries
            days = 45 if spec["instrument"]["type"] == "future" else 12
            data = {x: candles(seed, days=days) for seed, x in enumerate(names)}
        data["NSE:AAA-EQ"]["meta"] = {"lot": 25}        # a lot above 1: scale-outs split qty by sets, not 1:1
        if variant == 2 and not engine.multi_day(spec["timeframe"]):
            for d in data.values():
                d["tc"] = array("q", (t + d["tf_s"] - 60 * (i % 3) for i, t in enumerate(d["t"])))
        first = data["NSE:AAA-EQ"]["t"][min(75, len(data["NSE:AAA-EQ"]["t"]) // 4)]
        res = engine.run(spec, data, {}, first, None, set())
        res["monte_carlo"] = engine.monte_carlo(res["trades"], spec["portfolio"]["capital"])
    finally:
        engine.NATIVE = True
    return json.dumps(res, sort_keys=True, default=repr)


def check() -> list[str]:
    """Mismatching strategies (empty = identical). Raises when no .so is loaded."""
    if engine._native_lib() is None:
        raise RuntimeError("no app/algo/_native.so loaded")
    bad, trades = [], 0
    for n, spec in enumerate(specs()):
        if not engine._native_ok(spec):
            bad.append(f"spec {n}: not covered by the C++ loop")
            continue
        py, cc = _result(spec, False, n % 3), _result(spec, True, n % 3)
        trades += py.count('"entry_t"')
        if py != cc:
            at = next(j for j in range(min(len(py), len(cc))) if py[j] != cc[j]) if py[:1] else 0
            bad.append(f"spec {n}: differs at char {at}: py …{py[max(0, at - 80):at + 80]}… "
                       f"c++ …{cc[max(0, at - 80):at + 80]}…")
    print(f"native_check: {len(specs())} strategies, {trades} trades compared, {len(bad)} mismatches")
    return bad + check_signals()


def main() -> int:
    if engine._native_lib() is None:
        print("native_check: no app/algo/_native.so — the Python engine is used, nothing to check")
        return 0
    bad = check()
    for b in bad[:5]:
        print(b)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
