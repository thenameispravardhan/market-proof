"""Is app/algo/_native.so safe to use? It must give the Python engine's answers exactly.

Runs ~80 seeded strategies (every option native.cpp's run_loop covers) over
synthetic candles through engine.run twice — Python loop, then C++ loop — and
compares the whole result (stats, per-symbol, every trade) as JSON text, plus
the Monte Carlo. deploy/update.sh runs it after building the .so and deletes
the .so when it fails, so a wrong build can never answer a backtest.

    python -m app.algo.native_check      # exit 0 = identical (or no .so to check)
"""
from __future__ import annotations

import json
import random
import sys
from array import array

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


def specs(n: int = 80, seed: int = 11) -> list[dict]:
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
            "symbols": ["NSE:AAA-EQ", "NSE:BBB-EQ", "NSE:CCC-EQ"], "timeframe": 5, "direction": direction,
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
                        "square_off": pick(["15:15", "15:00"]), **pick([{}, {"days": [1, 2, 3, 4]}])},
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
    return out


def _result(spec: dict, native: bool) -> str:
    engine.NATIVE = native
    try:
        data = {f"NSE:{x}-EQ": candles(seed) for seed, x in enumerate(("AAA", "BBB", "CCC"))}
        res = engine.run(spec, data, {}, data["NSE:AAA-EQ"]["t"][75], None, set())
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
        py, cc = _result(spec, False), _result(spec, True)
        trades += py.count('"entry_t"')
        if py != cc:
            at = next(j for j in range(min(len(py), len(cc))) if py[j] != cc[j]) if py[:1] else 0
            bad.append(f"spec {n}: differs at char {at}: py …{py[max(0, at - 80):at + 80]}… "
                       f"c++ …{cc[max(0, at - 80):at + 80]}…")
    print(f"native_check: {len(specs())} strategies, {trades} trades compared, {len(bad)} mismatches")
    return bad


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
