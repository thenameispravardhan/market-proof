"""Strategy spec -> signals -> simulated trades -> stats.

A strategy is plain JSON, built on the Algo page:

    {"symbols": ["NSE:SBIN-EQ"], "timeframe": 5, "direction": "long|short|both",
     "entry_long":  {"logic": "AND", "conditions": [
         {"left": {"ind": "EMA", "params": {"period": 9}},
          "op": "crosses_above",
          "right": {"ind": "EMA", "params": {"period": 21}}}]},
     "exit_long": {...} | null, "entry_short": ..., "exit_short": ...,
     "stop_loss": {"type": "pct|points|atr", "value": 1.0},
     "target":    {"type": "pct|points|atr|rr", "value": 2.0},
     "trailing":  {"type": "pct|points|atr", "value": 0.5},
     "session": {"start": "09:20", "end": "15:00", "square_off": "15:15"},
     "max_trades_per_day": 3, "sizing": {"mode": "qty|amount|risk", "value": 1},
     "capital": 100000, "costs": {"slippage_pct": 0.02, "charges": true}}

Operands are {"ind", "params", "field", "offset"} or a constant {"value": 70}.
Groups nest: a condition list item with "conditions" is itself a group.

Execution model, identical in the backtest and the live runner:
  * conditions are evaluated on a COMPLETED bar's close;
  * the order fills at the NEXT bar's open (live: the LTP right after close);
  * stop / target / trailing are checked inside each bar, stop first when a
    bar spans both (the conservative assumption);
  * intraday only — every position is flat by `square_off` or the session end.
"""
from __future__ import annotations

import copy
import itertools
import math
from typing import Any, Optional

from app.algo import indicators as ind

IST = ind.IST_OFFSET
# Fyers intraday resolutions (minutes). Daily is deliberately absent: the bot
# is intraday-only, and a daily-bar strategy cannot square off the same day.
TIMEFRAMES = (1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 120)
OPS = (">", "<", ">=", "<=", "==", "!=", "crosses_above", "crosses_below", "crosses",
       "rising", "falling")

DEFAULT_SPEC: dict[str, Any] = {
    "symbols": [],
    "timeframe": 5,
    "direction": "long",
    "entry_long": {"logic": "AND", "conditions": []},
    "exit_long": None,
    "entry_short": {"logic": "AND", "conditions": []},
    "exit_short": None,
    "stop_loss": {"type": "pct", "value": 1.0},
    "target": {"type": "pct", "value": 2.0},
    "trailing": None,
    "session": {"start": "09:20", "end": "15:00", "square_off": "15:15"},
    "max_trades_per_day": 3,
    "sizing": {"mode": "qty", "value": 1},
    "capital": 100000,
    "costs": {"slippage_pct": 0.02, "charges": True},
}


def _hhmm(s: str) -> int:
    hh, mm = str(s).split(":")
    v = int(hh) * 60 + int(mm)
    if not 0 <= v < 1440:
        raise ValueError(f"bad time {s!r}")
    return v


def normalize(spec: dict[str, Any]) -> dict[str, Any]:
    """Defaults filled in, everything checked. Raises ValueError (-> 422)."""
    s = copy.deepcopy(DEFAULT_SPEC)
    for k, v in (spec or {}).items():
        if k in ("session", "costs", "sizing") and isinstance(v, dict):
            s[k] = {**s[k], **v}
        elif k in s or k == "name":
            s[k] = v
    s["symbols"] = [str(x).strip().upper() for x in s["symbols"] if str(x).strip()]
    if not s["symbols"]:
        raise ValueError("add at least one symbol")
    if len(s["symbols"]) > 50:
        raise ValueError("at most 50 symbols per strategy")
    s["timeframe"] = int(s["timeframe"])
    if s["timeframe"] not in TIMEFRAMES:
        raise ValueError(f"timeframe must be one of {TIMEFRAMES} minutes")
    if s["direction"] not in ("long", "short", "both"):
        raise ValueError("direction must be long, short or both")
    sess = s["session"]
    if not _hhmm("09:15") <= _hhmm(sess["start"]) <= _hhmm(sess["end"]) <= _hhmm(sess["square_off"]) <= _hhmm("15:29"):
        raise ValueError("session must satisfy 09:15 <= start <= end <= square_off <= 15:29")
    for key in ("entry_long", "exit_long", "entry_short", "exit_short"):
        if s[key]:
            _check_group(s[key])
    needs = ["entry_long"] if s["direction"] == "long" else ["entry_short"] if s["direction"] == "short" \
        else ["entry_long", "entry_short"]
    for key in needs:
        if not (s[key] or {}).get("conditions"):
            raise ValueError(f"{key} needs at least one condition")
    for key, kinds in (("stop_loss", ("pct", "points", "atr")), ("target", ("pct", "points", "atr", "rr")),
                       ("trailing", ("pct", "points", "atr"))):
        lv = s[key]
        if lv:
            if lv.get("type") not in kinds or float(lv.get("value", 0)) <= 0:
                raise ValueError(f"{key}: type in {kinds} and value > 0")
            lv["value"] = float(lv["value"])
            lv["atr_period"] = int(lv.get("atr_period", 14))
    if s["target"] and s["target"]["type"] == "rr" and not s["stop_loss"]:
        raise ValueError("an R:R target needs a stop loss")
    sz = s["sizing"]
    if sz.get("mode") not in ("qty", "amount", "risk") or float(sz.get("value", 0)) <= 0:
        raise ValueError("sizing: mode qty|amount|risk with value > 0")
    if sz["mode"] == "risk" and not s["stop_loss"]:
        raise ValueError("risk-based sizing needs a stop loss")
    s["max_trades_per_day"] = max(1, int(s["max_trades_per_day"]))
    s["capital"] = float(s["capital"])
    return s


def _check_group(g: dict[str, Any], depth: int = 0) -> None:
    if depth > 4:
        raise ValueError("condition groups nest at most 4 deep")
    if g.get("logic", "AND") not in ("AND", "OR"):
        raise ValueError("group logic must be AND or OR")
    for c in g.get("conditions", []):
        if "conditions" in c:
            _check_group(c, depth + 1)
            continue
        if c.get("op") not in OPS:
            raise ValueError(f"unknown operator {c.get('op')!r}")
        for side in ("left", "right"):
            o = c.get(side)
            if not isinstance(o, dict) or ("ind" not in o and "value" not in o):
                if side == "right" and c["op"] in ("rising", "falling"):
                    continue
                raise ValueError(f"condition {side} operand missing")
            if "ind" in o:
                if o["ind"] not in ind.REGISTRY:
                    raise ValueError(f"unknown indicator {o['ind']!r}")
                outs = ind.REGISTRY[o["ind"]][2]
                if o.get("field") and o["field"] not in outs:
                    raise ValueError(f"{o['ind']} has no field {o['field']!r} (one of {outs})")


# ---- signals ---------------------------------------------------------------

def series(d: dict, op: dict[str, Any], cache: dict) -> ind.S:
    if "ind" not in op:
        return [float(op["value"])] * len(d["c"])
    name, params = op["ind"], op.get("params") or {}
    key = (name, tuple(sorted(params.items())))
    if key not in cache:
        cache[key] = ind.compute(d, name, params)
    outs = cache[key]
    s = outs[op.get("field") or ind.REGISTRY[name][2][0]]
    return ind.shift(s, int(op.get("offset") or 0))


def _cond(d: dict, c: dict[str, Any], cache: dict) -> list[bool]:
    op = c["op"]
    a = series(d, c["left"], cache)
    n = len(a)
    if op in ("rising", "falling"):
        k = max(1, int((c.get("right") or {}).get("value", 1)))
        sgn = 1 if op == "rising" else -1
        return [i >= k and a[i] is not None and a[i - k] is not None and sgn * (a[i] - a[i - k]) > 0
                for i in range(n)]
    b = series(d, c["right"], cache)
    if op.startswith("cross"):
        out = [False] * n
        for i in range(1, n):
            if None in (a[i], b[i], a[i - 1], b[i - 1]):
                continue
            up = a[i] > b[i] and a[i - 1] <= b[i - 1]
            dn = a[i] < b[i] and a[i - 1] >= b[i - 1]
            out[i] = up if op == "crosses_above" else dn if op == "crosses_below" else up or dn
        return out
    f = {">": lambda x, y: x > y, "<": lambda x, y: x < y, ">=": lambda x, y: x >= y,
         "<=": lambda x, y: x <= y, "==": lambda x, y: abs(x - y) < 1e-9,
         "!=": lambda x, y: abs(x - y) >= 1e-9}[op]
    return [x is not None and y is not None and f(x, y) for x, y in zip(a, b)]


def evaluate(d: dict, g: Optional[dict[str, Any]], cache: dict) -> list[bool]:
    """A group as a bool per bar. Empty / missing group never fires."""
    n = len(d["c"])
    conds = (g or {}).get("conditions") or []
    if not conds:
        return [False] * n
    parts = [evaluate(d, c, cache) if "conditions" in c else _cond(d, c, cache) for c in conds]
    if (g.get("logic") or "AND") == "OR":
        return [any(x) for x in zip(*parts)]
    return [all(x) for x in zip(*parts)]


def signals(d: dict, spec: dict[str, Any], cache: dict) -> dict[str, list[bool]]:
    n = len(d["c"])
    off = [False] * n
    dirn = spec["direction"]
    return {
        "entry_long": evaluate(d, spec["entry_long"], cache) if dirn != "short" else off,
        "exit_long": evaluate(d, spec["exit_long"], cache) if dirn != "short" else off,
        "entry_short": evaluate(d, spec["entry_short"], cache) if dirn != "long" else off,
        "exit_short": evaluate(d, spec["exit_short"], cache) if dirn != "long" else off,
    }


# ---- levels, sizing, costs (shared with the live runner) -------------------

def _dist(lv: Optional[dict], price: float, atr_val: Optional[float]) -> Optional[float]:
    if not lv:
        return None
    if lv["type"] == "pct":
        return price * lv["value"] / 100
    if lv["type"] == "points":
        return lv["value"]
    if lv["type"] == "atr":
        return atr_val * lv["value"] if atr_val else None
    return None


def entry_levels(spec: dict, side: str, fill: float, atr_vals: dict[int, Optional[float]]
                 ) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """(stop, target, trail distance) for a fill. atr_vals: period -> ATR at signal bar."""
    sgn = 1 if side == "BUY" else -1
    sl_lv, tg_lv, tr_lv = spec["stop_loss"], spec["target"], spec["trailing"]
    sl_d = _dist(sl_lv, fill, atr_vals.get(sl_lv["atr_period"]) if sl_lv else None)
    if tg_lv and tg_lv["type"] == "rr":
        tg_d = sl_d * tg_lv["value"] if sl_d else None
    else:
        tg_d = _dist(tg_lv, fill, atr_vals.get(tg_lv["atr_period"]) if tg_lv else None)
    tr_d = _dist(tr_lv, fill, atr_vals.get(tr_lv["atr_period"]) if tr_lv else None)
    return (fill - sgn * sl_d if sl_d else None, fill + sgn * tg_d if tg_d else None, tr_d)


def atr_periods(spec: dict) -> set[int]:
    return {lv["atr_period"] for lv in (spec["stop_loss"], spec["target"], spec["trailing"])
            if lv and lv["type"] == "atr"}


def size(spec: dict, price: float, stop: Optional[float]) -> int:
    sz = spec["sizing"]
    v = float(sz["value"])
    if sz["mode"] == "qty":
        return int(v)
    if sz["mode"] == "amount":
        return int(v // price) if price > 0 else 0
    risk = abs(price - stop) if stop else 0
    return int(v // risk) if risk > 0 else 0


def charges(buy_value: float, sell_value: float) -> float:
    """Indian equity INTRADAY charges for one round trip, in rupees.

    ponytail: equity-intraday rates only (Fyers brokerage min(₹20, 0.03%),
    STT 0.025% sell, NSE txn 0.00297%, SEBI ₹10/cr, stamp 0.003% buy, GST 18%).
    F&O legs are charged differently — add a segment switch if F&O lands here.
    """
    brokerage = min(20.0, 0.0003 * buy_value) + min(20.0, 0.0003 * sell_value)
    turnover = buy_value + sell_value
    exch, sebi = 0.0000297 * turnover, 0.000001 * turnover
    return round(brokerage + 0.00025 * sell_value + exch + sebi
                 + 0.18 * (brokerage + exch + sebi) + 0.00003 * buy_value, 2)


def trade_pnl(side: str, qty: int, entry: float, exit_: float, with_charges: bool) -> tuple[float, float]:
    gross = (exit_ - entry) * qty * (1 if side == "BUY" else -1)
    buy, sell = (entry, exit_) if side == "BUY" else (exit_, entry)
    ch = charges(buy * qty, sell * qty) if with_charges else 0.0
    return round(gross, 2), ch


# ---- simulation ------------------------------------------------------------

def simulate(d: dict, spec: dict, symbol: str, cache: dict) -> list[dict[str, Any]]:
    t, o, h, l, c, tf = d["t"], d["o"], d["h"], d["l"], d["c"], d["tf_s"]
    n = len(c)
    if n < 2:
        return []
    sig = signals(d, spec, cache)
    atrs = {p: series(d, {"ind": "ATR", "params": {"period": p}}, cache) for p in atr_periods(spec)}
    start, end = _hhmm(spec["session"]["start"]), _hhmm(spec["session"]["end"])
    sq = _hhmm(spec["session"]["square_off"])
    slip = float(spec["costs"].get("slippage_pct", 0)) / 100
    with_ch = bool(spec["costs"].get("charges", True))
    max_day = spec["max_trades_per_day"]
    trades: list[dict[str, Any]] = []
    per_day: dict[int, int] = {}
    pos: Optional[dict[str, Any]] = None
    pending: Optional[dict[str, Any]] = None

    def close(price: float, at: int, reason: str, i: int, slipped: bool = True) -> None:
        nonlocal pos
        if slipped:
            price = price * (1 - slip) if pos["side"] == "BUY" else price * (1 + slip)
        gross, ch = trade_pnl(pos["side"], pos["qty"], pos["entry"], price, with_ch)
        trades.append({"symbol": symbol, "side": pos["side"], "qty": pos["qty"],
                       "entry_t": pos["t"], "entry": round(pos["entry"], 2),
                       "exit_t": at, "exit": round(price, 2), "reason": reason,
                       "gross": gross, "charges": ch, "net": round(gross - ch, 2),
                       "bars": i - pos["i"], "stop": pos["sl"], "target": pos["tg"]})
        pos = None

    for i in range(n):
        day, mod = (t[i] + IST) // 86400, ((t[i] + IST) % 86400) // 60
        if pos and day != pos["day"]:
            close(c[i - 1], t[i - 1] + tf, "EOD", i - 1)
        if pos and (pos["exit_pending"] or mod >= sq):
            close(o[i], t[i], "SIGNAL" if pos["exit_pending"] else "SQUARE_OFF", i)
        if pending:
            if pos is None and pending["day"] == day and mod < sq:
                side = pending["side"]
                fill = o[i] * (1 + slip) if side == "BUY" else o[i] * (1 - slip)
                av = {p: s[pending["i"]] for p, s in atrs.items()}
                sl, tg, trd = entry_levels(spec, side, fill, av)
                qty = size(spec, fill, sl)
                if qty >= 1:
                    pos = {"side": side, "qty": qty, "entry": fill, "t": t[i], "i": i, "day": day,
                           "sl": round(sl, 2) if sl else None, "tg": round(tg, 2) if tg else None,
                           "trd": trd, "best": fill, "trail": None, "exit_pending": False}
                    per_day[day] = per_day.get(day, 0) + 1
            pending = None
        if pos:
            buy = pos["side"] == "BUY"
            stops = [x for x in (pos["sl"], pos["trail"]) if x is not None]
            stop = (max(stops) if buy else min(stops)) if stops else None
            tg = pos["tg"]
            why = "TRAIL" if stop is not None and stop == pos["trail"] and stop != pos["sl"] else "SL"
            later = i > pos["i"]   # a gap through the level only exists after the entry bar
            if stop is not None and later and (o[i] <= stop if buy else o[i] >= stop):
                close(o[i], t[i], why, i)
            elif stop is not None and (l[i] <= stop if buy else h[i] >= stop):
                close(stop, t[i], why, i)
            elif tg is not None and later and (o[i] >= tg if buy else o[i] <= tg):
                close(o[i], t[i], "TARGET", i, slipped=False)
            elif tg is not None and (h[i] >= tg if buy else l[i] <= tg):
                close(tg, t[i], "TARGET", i, slipped=False)
        if pos:
            if pos["trd"]:
                if pos["side"] == "BUY":
                    pos["best"] = max(pos["best"], h[i])
                    nt = pos["best"] - pos["trd"]
                    pos["trail"] = nt if pos["trail"] is None else max(pos["trail"], nt)
                else:
                    pos["best"] = min(pos["best"], l[i])
                    nt = pos["best"] + pos["trd"]
                    pos["trail"] = nt if pos["trail"] is None else min(pos["trail"], nt)
            if sig["exit_long" if pos["side"] == "BUY" else "exit_short"][i]:
                pos["exit_pending"] = True
        if pos is None and pending is None and i + 1 < n:
            close_mod = mod + tf // 60
            if start <= close_mod <= end and per_day.get(day, 0) < max_day:
                side = "BUY" if sig["entry_long"][i] else "SELL" if sig["entry_short"][i] else None
                if side:
                    pending = {"side": side, "day": day, "i": i}
    if pos:
        close(c[-1], t[-1] + tf, "END", n - 1)
    return trades


# ---- stats -----------------------------------------------------------------

def stats(trades: list[dict[str, Any]], capital: float) -> dict[str, Any]:
    trades = sorted(trades, key=lambda x: x["exit_t"])
    n = len(trades)
    nets = [x["net"] for x in trades]
    wins = [v for v in nets if v > 0]
    losses = [v for v in nets if v <= 0]
    eq, peak, max_dd, max_dd_pct = capital, capital, 0.0, 0.0
    curve = []
    for x in trades:
        eq += x["net"]
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
        max_dd_pct = max(max_dd_pct, (peak - eq) / peak * 100 if peak > 0 else 0)
        curve.append([x["exit_t"], round(eq, 2)])
    daily: dict[int, float] = {}
    for x in trades:
        k = (x["exit_t"] + IST) // 86400
        daily[k] = daily.get(k, 0.0) + x["net"]
    dv = list(daily.values())
    mean = sum(dv) / len(dv) if dv else 0.0
    sd = math.sqrt(sum((v - mean) ** 2 for v in dv) / (len(dv) - 1)) if len(dv) > 1 else 0.0
    streak = {"win": 0, "loss": 0}
    cur_w = cur_l = 0
    for v in nets:
        cur_w, cur_l = (cur_w + 1, 0) if v > 0 else (0, cur_l + 1)
        streak["win"], streak["loss"] = max(streak["win"], cur_w), max(streak["loss"], cur_l)
    gross_win, gross_loss = sum(wins), -sum(losses)
    net = sum(nets)
    return {
        "trades": n, "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / n * 100, 2) if n else 0.0,
        "gross_pnl": round(sum(x["gross"] for x in trades), 2),
        "charges": round(sum(x["charges"] for x in trades), 2),
        "net_pnl": round(net, 2),
        "return_pct": round(net / capital * 100, 2) if capital else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else (None if not gross_win else 999.0),
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "expectancy": round(net / n, 2) if n else 0.0,
        "largest_win": round(max(nets), 2) if nets else 0.0,
        "largest_loss": round(min(nets), 2) if nets else 0.0,
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "sharpe": round(mean / sd * math.sqrt(252), 2) if sd > 0 else None,
        "max_consecutive_wins": streak["win"], "max_consecutive_losses": streak["loss"],
        "avg_bars_held": round(sum(x["bars"] for x in trades) / n, 1) if n else 0.0,
        "trading_days": len(daily),
        "profitable_days": sum(1 for v in dv if v > 0),
        "equity": curve,
        "daily": [[k * 86400 - IST + 86400 // 2, round(v, 2)] for k, v in sorted(daily.items())],
    }


def by_reason(trades: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for x in trades:
        r = out.setdefault(x["reason"], {"count": 0, "net": 0.0})
        r["count"] += 1
        r["net"] = round(r["net"] + x["net"], 2)
    return out


def run(spec: dict, data: dict[str, dict], caches: Optional[dict[str, dict]] = None) -> dict[str, Any]:
    """Backtest a normalized spec over {symbol: bars}. Symbols trade independently.

    ponytail: no shared-capital constraint across symbols — each symbol sizes
    on its own. Add a portfolio cash ledger if concurrent positions must
    compete for margin.
    """
    caches = caches if caches is not None else {}
    trades: list[dict[str, Any]] = []
    per_symbol = {}
    for sym, d in data.items():
        tr = simulate(d, spec, sym, caches.setdefault(sym, {}))
        trades += tr
        st = stats(tr, spec["capital"])
        per_symbol[sym] = {k: st[k] for k in ("trades", "win_rate", "net_pnl", "profit_factor",
                                              "max_drawdown")} | {"bars": len(d["c"])}
    trades.sort(key=lambda x: x["entry_t"])
    return {"stats": stats(trades, spec["capital"]), "per_symbol": per_symbol,
            "by_reason": by_reason(trades), "trades": trades}


# ---- optimisation ----------------------------------------------------------

def set_path(obj: Any, path: str, value: Any) -> None:
    """Set e.g. "entry_long.conditions.0.left.params.period" in place."""
    keys = [int(k) if k.isdigit() else k for k in path.split(".")]
    for k in keys[:-1]:
        obj = obj[k]
    if isinstance(obj, list) and not isinstance(keys[-1], int):
        raise ValueError(f"bad path {path!r}")
    obj[keys[-1]] = value


MAX_COMBOS = 400
METRICS = ("net_pnl", "profit_factor", "sharpe", "win_rate", "return_pct", "expectancy")


def optimize(spec: dict, data: dict[str, dict], grid: list[dict[str, Any]], metric: str = "net_pnl",
             min_trades: int = 5) -> dict[str, Any]:
    """Grid-search over [{path, values}] and rank by `metric`.

    Ranks only combos with >= min_trades: a 1-trade 100% win rate wins every
    grid search and means nothing.
    """
    if metric not in METRICS:
        raise ValueError(f"metric must be one of {METRICS}")
    if not grid:
        raise ValueError("add at least one parameter range")
    axes = [(g["path"], list(g["values"])) for g in grid]
    combos = list(itertools.product(*(v for _, v in axes)))
    if not combos or len(combos) > MAX_COMBOS:
        raise ValueError(f"{len(combos)} combinations — keep it between 1 and {MAX_COMBOS}")
    caches: dict[str, dict] = {}
    rows = []
    for combo in combos:
        s = copy.deepcopy(spec)
        for (path, _), v in zip(axes, combo):
            set_path(s, path, v)
        s = normalize(s)
        st = run(s, data, caches)["stats"]
        rows.append({"params": {p: v for (p, _), v in zip(axes, combo)},
                     **{k: st[k] for k in ("trades", "win_rate", "net_pnl", "profit_factor", "sharpe",
                                           "max_drawdown", "return_pct", "expectancy")}})
    ok = [r for r in rows if r["trades"] >= min_trades]
    ok.sort(key=lambda r: (r[metric] is not None, r[metric] or 0), reverse=True)
    return {"combos": len(combos), "ranked": ok[:100],
            "too_few_trades": len(rows) - len(ok), "metric": metric}
