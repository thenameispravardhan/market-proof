"""Strategy spec -> signals -> portfolio simulation -> stats.

A strategy is plain JSON, built on the Algo page (see DEFAULT_SPEC). Signals
are computed on the UNDERLYING symbols; what is traded is the `instrument`:
the stock/index itself, its future, or option legs (CE/PE, BUY/SELL, ATM /
N strikes ITM or OTM / nearest a target premium, weekly or monthly expiry).

Operands: {"ind", "params", "field", "offset", "tf", "mult", "add"} or a
constant {"value": 70}. `tf` evaluates the indicator on another timeframe
(minutes, 1440 = daily) and only ever sees that timeframe's COMPLETED bars.
`mult`/`add` turn "close > EMA x 1.02" or "open > prev close + 20" into one
condition. Groups nest: a list item with "conditions" is itself a group.

Execution model, shared with the live runner:
  * conditions are evaluated on a COMPLETED bar's close;
  * entries fill at the NEXT bar's open (live: the LTP right after close);
  * stop / target / trailing / breakeven are checked inside each bar, the
    stop first when a bar spans both (the conservative assumption);
  * MTM (rupee) stop / target / trail work on the whole position — the way
    to manage a multi-leg option position;
  * daily max loss / max profit flatten the strategy and stop it for the day;
  * intraday only — everything is flat by `square_off`.

The portfolio is one cash ledger across all symbols: positions compete for
capital (leverage applies to equity/futures/option writing, never to option
premium paid), `max_positions` caps concurrency, and with compounding the
size of the next trade follows the equity curve.
"""
from __future__ import annotations

import copy
import itertools
import math
from typing import Any, Callable, Optional

from app.algo import fno
from app.algo import indicators as ind

IST = ind.IST_OFFSET
SESSION_CLOSE_S = 15 * 3600 + 30 * 60
TIMEFRAMES = (1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 120)
COND_TIMEFRAMES = TIMEFRAMES + (1440,)          # 1440 = daily, conditions only
OPS = (">", "<", ">=", "<=", "==", "!=", "crosses_above", "crosses_below", "crosses",
       "rising", "falling")
INSTRUMENTS = ("equity", "future", "option")
SIZING = ("qty", "lots", "amount", "pct_equity", "risk", "risk_pct")
STRIKE_MODES = ("ATM", "ITM", "OTM", "PREMIUM")

DEFAULT_SPEC: dict[str, Any] = {
    "symbols": [],
    "timeframe": 5,
    "direction": "long",
    "entry_long": {"logic": "AND", "conditions": []},
    "exit_long": None,
    "entry_short": {"logic": "AND", "conditions": []},
    "exit_short": None,
    "instrument": {
        "type": "equity",                 # equity | future | option
        "expiry": "current",              # current | next
        "expiry_kind": "weekly",          # weekly | monthly (options; weekly only NIFTY/SENSEX)
        "legs_long": [{"right": "CE", "action": "BUY", "strike": "ATM", "steps": 0, "premium": 100, "lots": 1}],
        "legs_short": [{"right": "PE", "action": "BUY", "strike": "ATM", "steps": 0, "premium": 100, "lots": 1}],
        "levels_on": "instrument",        # stop/target on the traded price, or on the underlying
        "iv": {"source": "auto", "value": 15},  # backtest pricing: auto (VIX for indices) | hv | vix | fixed %
    },
    "stop_loss": {"type": "pct", "value": 1.0},
    "target": {"type": "pct", "value": 2.0},
    "trailing": None,                     # {"type", "value", "activate"}
    "breakeven": None,                    # {"type", "value"}: once this far in profit, stop -> entry
    "mtm": {"stop": None, "target": None, "trail_start": None, "trail_gap": None},   # rupees
    "daily": {"max_loss": None, "max_profit": None},                                # rupees
    "session": {"start": "09:20", "end": "15:00", "square_off": "15:15"},
    "max_trades_per_day": 3,
    "cooldown_bars": 0,
    "max_bars": None,
    "sizing": {"mode": "qty", "value": 1},
    "portfolio": {"capital": 100000, "leverage": 1.0, "max_positions": 10, "compounding": True},
    "costs": {"slippage_pct": 0.02, "charges": True},
}


def _hhmm(s: str) -> int:
    hh, mm = str(s).split(":")
    v = int(hh) * 60 + int(mm)
    if not 0 <= v < 1440:
        raise ValueError(f"bad time {s!r}")
    return v


def _num(v: Any, name: str, lo: float = 0.0, integer: bool = False, allow_none: bool = False) -> Any:
    if v is None or v == "":
        if allow_none:
            return None
        raise ValueError(f"{name} is required")
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if math.isnan(x) or x < lo:
        raise ValueError(f"{name} must be >= {lo}")
    return int(x) if integer else x


def normalize(spec: dict[str, Any]) -> dict[str, Any]:
    """Defaults filled in, everything checked. Raises ValueError (-> 422)."""
    s = copy.deepcopy(DEFAULT_SPEC)
    spec = dict(spec or {})
    if "capital" in spec and "portfolio" not in spec:       # specs saved before portfolios
        spec["portfolio"] = {"capital": spec.pop("capital")}
    for k, v in spec.items():
        if k in ("session", "costs", "sizing", "portfolio", "mtm", "daily", "instrument") and isinstance(v, dict):
            s[k] = {**s[k], **v}
        elif k in s or k == "name":
            s[k] = v
    s["symbols"] = list(dict.fromkeys(str(x).strip().upper() for x in s["symbols"] if str(x).strip()))
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
            _check_group(s[key], s["timeframe"])
    needs = {"long": ["entry_long"], "short": ["entry_short"], "both": ["entry_long", "entry_short"]}[s["direction"]]
    for key in needs:
        if not (s[key] or {}).get("conditions"):
            raise ValueError(f"{key} needs at least one condition")

    inst = s["instrument"]
    inst["iv"] = {**DEFAULT_SPEC["instrument"]["iv"], **(inst.get("iv") or {})}
    if inst["type"] not in INSTRUMENTS:
        raise ValueError(f"instrument type must be one of {INSTRUMENTS}")
    if inst["expiry"] not in ("current", "next") or inst["expiry_kind"] not in ("weekly", "monthly"):
        raise ValueError("expiry is current|next and weekly|monthly")
    if inst["levels_on"] not in ("instrument", "underlying"):
        raise ValueError("levels_on is instrument or underlying")
    if inst["iv"]["source"] not in ("auto", "hv", "vix", "fixed"):
        raise ValueError("iv source is auto, hv, vix or fixed")
    inst["iv"]["value"] = _num(inst["iv"]["value"], "fixed IV %", 0.1)
    opt = inst["type"] == "option"
    multi = False
    for key, used in (("legs_long", s["direction"] != "short"), ("legs_short", s["direction"] != "long")):
        legs = inst.get(key) or []
        if opt and used and not legs:
            raise ValueError(f"options need at least one leg in {key}")
        for lg in legs:
            if lg.get("right") not in ("CE", "PE") or lg.get("action") not in ("BUY", "SELL"):
                raise ValueError("each leg needs right CE|PE and action BUY|SELL")
            if lg.get("strike", "ATM") not in STRIKE_MODES:
                raise ValueError(f"strike must be one of {STRIKE_MODES}")
            lg["strike"] = lg.get("strike", "ATM")
            lg["steps"] = _num(lg.get("steps", 0), "strike steps", 0, integer=True)
            lg["premium"] = _num(lg.get("premium", 100), "target premium", 0.05)
            lg["lots"] = _num(lg.get("lots", 1), "leg lots", 1, integer=True)
        multi = multi or (opt and used and len(legs) > 1)

    for key, kinds in (("stop_loss", ("pct", "points", "atr")), ("target", ("pct", "points", "atr", "rr")),
                       ("trailing", ("pct", "points", "atr")), ("breakeven", ("pct", "points", "atr"))):
        lv = s[key]
        if not lv:
            s[key] = None
            continue
        if lv.get("type") not in kinds:
            raise ValueError(f"{key}: type must be one of {kinds}")
        lv["value"] = _num(lv.get("value"), key, 1e-9)
        lv["atr_period"] = _num(lv.get("atr_period", 14), f"{key} ATR period", 1, integer=True)
        if key == "trailing":
            lv["activate"] = _num(lv.get("activate") or 0, "trail activation", 0)
        if opt and lv["type"] == "atr" and inst["levels_on"] == "instrument":
            raise ValueError(f"{key}: ATR levels on an option premium make no sense — "
                             "set levels on the underlying")
        if multi and inst["levels_on"] == "instrument":
            raise ValueError("multi-leg positions can't use price levels on 'the instrument' — "
                             "use MTM ₹ stops/targets or put levels on the underlying")
    if s["target"] and s["target"]["type"] == "rr" and not s["stop_loss"]:
        raise ValueError("an R:R target needs a stop loss")

    for k in ("stop", "target", "trail_start", "trail_gap"):
        s["mtm"][k] = _num(s["mtm"].get(k), f"MTM {k}", 1e-9, allow_none=True)
    if (s["mtm"]["trail_start"] is None) != (s["mtm"]["trail_gap"] is None):
        raise ValueError("MTM trailing needs both a start and a gap")
    for k in ("max_loss", "max_profit"):
        s["daily"][k] = _num(s["daily"].get(k), f"daily {k}", 1e-9, allow_none=True)

    sz = s["sizing"]
    if sz.get("mode") not in SIZING:
        raise ValueError(f"sizing mode must be one of {SIZING}")
    if inst["type"] != "equity" and sz["mode"] == "qty":
        sz["mode"] = "lots"                          # F&O trades in lots
    if inst["type"] == "equity" and sz["mode"] == "lots":
        sz["mode"] = "qty"
    sz["value"] = _num(sz.get("value"), "size", 1e-9)
    if sz["mode"] in ("risk", "risk_pct"):
        if not s["stop_loss"]:
            raise ValueError("risk-based sizing needs a stop loss")
        if opt and (multi or inst["levels_on"] == "underlying"):
            raise ValueError("risk-based sizing for options needs a single leg with levels on the premium")
    pf = s["portfolio"]
    pf["capital"] = _num(pf.get("capital"), "capital", 1)
    pf["leverage"] = _num(pf.get("leverage", 1), "leverage", 0.1)
    pf["max_positions"] = _num(pf.get("max_positions", 10), "max positions", 1, integer=True)
    pf["compounding"] = bool(pf.get("compounding", True))
    s["max_trades_per_day"] = _num(s["max_trades_per_day"], "max trades per day", 1, integer=True)
    s["cooldown_bars"] = _num(s.get("cooldown_bars") or 0, "cooldown bars", 0, integer=True)
    s["max_bars"] = _num(s.get("max_bars"), "max bars in trade", 1, integer=True, allow_none=True)
    s["costs"]["slippage_pct"] = _num(s["costs"].get("slippage_pct", 0), "slippage %", 0)
    s["costs"]["charges"] = bool(s["costs"].get("charges", True))
    return s


def _check_group(g: dict[str, Any], base_tf: int, depth: int = 0) -> None:
    if depth > 4:
        raise ValueError("condition groups nest at most 4 deep")
    if g.get("logic", "AND") not in ("AND", "OR"):
        raise ValueError("group logic must be AND or OR")
    for c in g.get("conditions", []):
        if "conditions" in c:
            _check_group(c, base_tf, depth + 1)
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
                tf = int(o.get("tf") or 0)
                if tf and (tf not in COND_TIMEFRAMES or (tf != 1440 and tf % base_tf)):
                    raise ValueError(f"{o['ind']}: condition timeframe {tf} must be daily or a multiple "
                                     f"of the strategy's {base_tf}m")
                for k in ("mult", "add"):
                    if o.get(k) not in (None, ""):
                        o[k] = _num(o[k], k, -1e12)


def _walk_operands(spec: dict[str, Any]):
    def walk(g):
        for c in (g or {}).get("conditions", []):
            if "conditions" in c:
                yield from walk(c)
            else:
                for o in (c.get("left"), c.get("right")):
                    if isinstance(o, dict) and "ind" in o:
                        yield o
    for k in ("entry_long", "exit_long", "entry_short", "exit_short"):
        yield from walk(spec.get(k))


def cond_timeframes(spec: dict[str, Any]) -> set[int]:
    """Higher timeframes the conditions reference (the base excluded)."""
    return {int(o["tf"]) for o in _walk_operands(spec) if int(o.get("tf") or 0) not in (0, spec["timeframe"])}


def warmup_bars(spec: dict[str, Any]) -> int:
    """Bars of history indicators need before the first tradable bar."""
    longest = 14
    for o in _walk_operands(spec):
        for v in (o.get("params") or {}).values():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                longest = max(longest, int(v))
        longest = max(longest, int(o.get("offset") or 0))
    return min(1000, 3 * longest + 5)


# ---- signals ---------------------------------------------------------------

def _close_times(b: dict) -> list[int]:
    tf = b["tf_s"]
    return [min(t + tf, t - (t + IST) % 86400 + SESSION_CLOSE_S) for t in b["t"]]


def _align(d: dict, h: dict, s: ind.S, cache: dict) -> ind.S:
    """Each base bar sees the last HIGHER-timeframe bar that had CLOSED by the
    base bar's own close — never the one still forming."""
    key = ("align", id(h))
    if key not in cache:
        ch, cb = _close_times(h), _close_times(d)
        idx, j = [], -1
        for x in cb:
            while j + 1 < len(ch) and ch[j + 1] <= x:
                j += 1
            idx.append(j)
        cache[key] = idx
    return [s[j] if j >= 0 else None for j in cache[key]]


def series(d: dict, op: dict[str, Any], cache: dict) -> ind.S:
    n = len(d["c"])
    if "ind" not in op:
        return [float(op["value"])] * n
    tf = int(op.get("tf") or 0)
    src = d
    if tf and tf != d.get("tf_min"):
        src = (d.get("htf") or {}).get(tf)
        if src is None:
            raise ValueError(f"no {tf}-minute bars loaded for {op['ind']}")
    name, params = op["ind"], op.get("params") or {}
    key = (id(src), name, tuple(sorted(params.items())))
    if key not in cache:
        cache[key] = ind.compute(src, name, params)
    s = cache[key][op.get("field") or ind.REGISTRY[name][2][0]]
    s = ind.shift(s, int(op.get("offset") or 0))
    if src is not d:
        s = _align(d, src, s, cache)
    mult = float(op.get("mult") if op.get("mult") not in (None, "") else 1)
    add = float(op.get("add") or 0)
    if mult != 1 or add:
        s = [None if v is None else v * mult + add for v in s]
    return s


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

def _dist(lv: Optional[dict], price: float, atr_val: Optional[float], value_key: str = "value") -> Optional[float]:
    if not lv or not lv.get(value_key):
        return None
    v = lv[value_key]
    if lv["type"] == "pct":
        return price * v / 100
    if lv["type"] == "points":
        return v
    if lv["type"] == "atr":
        return atr_val * v if atr_val else None
    return None


def entry_levels(spec: dict, sign: int, ref: float, atr_vals: dict[int, Optional[float]]) -> dict[str, Optional[float]]:
    """Stop / target / trail gap / trail activation / breakeven trigger for a
    position whose reference price `ref` profits in direction `sign`."""
    g = lambda lv, key="value": _dist(lv, ref, atr_vals.get(lv["atr_period"]) if lv else None, key)  # noqa: E731
    sl_lv, tg_lv, tr_lv = spec["stop_loss"], spec["target"], spec["trailing"]
    sl_d = g(sl_lv)
    tg_d = (sl_d * tg_lv["value"] if sl_d else None) if tg_lv and tg_lv["type"] == "rr" else g(tg_lv)
    return {"sl": ref - sign * sl_d if sl_d else None,
            "tg": ref + sign * tg_d if tg_d else None,
            "trd": g(tr_lv), "tra": g(tr_lv, "activate") or 0.0,
            "be": g(spec["breakeven"]), "sl_dist": sl_d}


def atr_periods(spec: dict) -> set[int]:
    return {lv["atr_period"] for lv in (spec["stop_loss"], spec["target"], spec["trailing"], spec["breakeven"])
            if lv and lv["type"] == "atr"}


SEGMENT = {"EQ": "eq", "FUT": "fut", "CE": "opt", "PE": "opt"}


def charges(buy_value: float, sell_value: float, segment: str = "eq") -> float:
    """Indian INTRADAY round-trip charges in rupees, by segment.

    eq:  brokerage min(₹20, 0.03%)/order, STT 0.025% sell, NSE 0.00297%, stamp 0.003% buy
    fut: brokerage min(₹20, 0.03%)/order, STT 0.02% sell,  NSE 0.00173%, stamp 0.002% buy
    opt: brokerage ₹20/order,            STT 0.1% sell premium, NSE 0.03503%, stamp 0.003% buy
    plus SEBI ₹10/crore and 18% GST on brokerage + exchange + SEBI.
    """
    if segment == "opt":
        brokerage = (20.0 if buy_value else 0) + (20.0 if sell_value else 0)
        stt, exch_rate, stamp = 0.001 * sell_value, 0.0003503, 0.00003 * buy_value
    elif segment == "fut":
        brokerage = min(20.0, 0.0003 * buy_value) + min(20.0, 0.0003 * sell_value)
        stt, exch_rate, stamp = 0.0002 * sell_value, 0.0000173, 0.00002 * buy_value
    else:
        brokerage = min(20.0, 0.0003 * buy_value) + min(20.0, 0.0003 * sell_value)
        stt, exch_rate, stamp = 0.00025 * sell_value, 0.0000297, 0.00003 * buy_value
    turnover = buy_value + sell_value
    exch, sebi = exch_rate * turnover, 0.000001 * turnover
    return round(brokerage + stt + exch + sebi + 0.18 * (brokerage + exch + sebi) + stamp, 2)


def leg_pnl(act: int, qty: int, entry: float, exit_: float, kind: str, with_charges: bool) -> tuple[float, float]:
    gross = (exit_ - entry) * qty * act
    buy, sell = (entry, exit_) if act > 0 else (exit_, entry)
    ch = charges(buy * qty, sell * qty, SEGMENT.get(kind, "eq")) if with_charges else 0.0
    return gross, ch


def trade_pnl(side: str, qty: int, entry: float, exit_: float, with_charges: bool, kind: str = "EQ") -> tuple[float, float]:
    gross, ch = leg_pnl(1 if side == "BUY" else -1, qty, entry, exit_, kind, with_charges)
    return round(gross, 2), ch


def leg_margin(kind: str, act: int, price: float, underlying: float, qty: int, leverage: float) -> float:
    """Capital a leg blocks: premium paid for a bought option (no leverage),
    notional / leverage for everything else (written options on the underlying)."""
    if kind in ("CE", "PE"):
        return price * qty if act > 0 else underlying * qty / leverage
    return price * qty / leverage


def position_margin(legs: list[dict], underlying: float, leverage: float, price_key: str = "entry",
                    qty_key: str = "per_set") -> float:
    """Capital a position blocks. Written calls and written puts on the same
    underlying offset (only one side can lose at expiry, as SPAN treats a short
    straddle/strangle), so the writing margin is the larger side's; premium for
    bought options and notional/leverage for futures and cash are added on top.

    ponytail: bought wings don't reduce the writing margin (real SPAN would
    give an iron fly a smaller one) — conservative, so sizing errs small."""
    side = {"CE": 0.0, "PE": 0.0}
    rest = 0.0
    for lg in legs:
        m = leg_margin(lg["kind"], lg["act"], lg[price_key], underlying, lg[qty_key], leverage)
        if lg["kind"] in side and lg["act"] < 0:
            side[lg["kind"]] += m
        else:
            rest += m
    return max(side.values()) + rest


def units(spec: dict, equity: float, per_set_margin: float, per_set_risk: Optional[float]) -> int:
    """How many SETS to trade (shares for equity; lot-multiples for F&O)."""
    sz = spec["sizing"]
    v = float(sz["value"])
    mode = sz["mode"]
    if mode in ("qty", "lots"):
        return int(v)
    if mode in ("amount", "pct_equity"):
        budget = v if mode == "amount" else equity * v / 100
        return int(budget // per_set_margin) if per_set_margin > 0 else 0
    risk = v if mode == "risk" else equity * v / 100
    return int(risk // per_set_risk) if per_set_risk else 0


# ---- per-symbol context ----------------------------------------------------

def prepare(d: dict, spec: dict) -> None:
    """Attach the option-pricing context to one symbol's bars (in place):
    `iv` per bar (decimal) and `expiry_ts` per bar (for DTE)."""
    meta = d.setdefault("meta", {})
    meta.setdefault("name", fno.fno_name(meta.get("symbol", "")))
    meta.setdefault("exch", fno.exchange_of(meta.get("symbol", "")))
    inst = spec["instrument"]
    kind = inst["expiry_kind"] if inst["type"] == "option" else "monthly"
    d["expiry_ts"] = [fno.expiry_after(meta["name"], meta["exch"], kind, inst["expiry"], t) for t in d["t"]]
    if inst["type"] != "option":
        return
    n = len(d["t"])
    src = inst["iv"]["source"]
    if src == "fixed":
        d["iv"] = [inst["iv"]["value"] / 100] * n
        return
    # Realised vol of the underlying, from the days BEFORE each bar's day.
    closes: list[tuple[int, float]] = []
    for t, c in zip(d["t"], d["c"]):
        k = (t + IST) // 86400
        if closes and closes[-1][0] == k:
            closes[-1] = (k, c)
        else:
            closes.append((k, c))
    # rets[r] is the return INTO day r+1, so day j may use rets[:j-1] — days
    # strictly before it. No lookahead into the day being traded.
    rets = [math.log(b[1] / a[1]) for a, b in zip(closes, closes[1:])]
    hv_by_day: dict[int, float] = {}
    default = 0.15 if meta["name"] in fno.INDEX_LOTS else 0.30
    for j, (k, _) in enumerate(closes):
        window = rets[max(0, j - 21):max(0, j - 1)]
        if len(window) >= 5:
            m = sum(window) / len(window)
            hv_by_day[k] = max(0.05, math.sqrt(sum((r - m) ** 2 for r in window) / (len(window) - 1)) * math.sqrt(252))
    hv = [hv_by_day.get((t + IST) // 86400, default) for t in d["t"]]
    if src == "auto":           # implied vol where there is one: VIX for index options
        src = "vix" if meta["name"] in fno.INDEX_LOTS else "hv"
    if src == "vix" and d.get("vix"):
        v = _align(d, d["vix"], d["vix"]["c"], {})
        d["iv"] = [x / 100 if x else h for x, h in zip(v, hv)]
    else:
        d["iv"] = hv


def _leg_px(leg: dict, u: float, t: float, iv: float) -> float:
    if leg["kind"] in ("EQ", "FUT"):
        return u
    return fno.bs_price(u, leg["K"], fno.years_to(leg["exp"], t), iv, leg["kind"])


def _solve(f: Callable[[float], float], target: float, a: float, b: float) -> float:
    """u in [a, b] where f(u) crosses `target` (f continuous; endpoints straddle)."""
    fa = f(a) - target
    for _ in range(40):
        m = (a + b) / 2
        fm = f(m) - target
        if (fm <= 0) == (fa <= 0):
            a, fa = m, fm
        else:
            b = m
    return (a + b) / 2


# ---- simulation ------------------------------------------------------------

def run(spec: dict, data: dict[str, dict], caches: Optional[dict[str, dict]] = None,
        trade_from: Optional[int] = None) -> dict[str, Any]:
    """Backtest a normalized spec over {symbol: bars}; one shared portfolio.

    Each symbol's bars may carry `htf` ({tf: bars}) for multi-timeframe
    conditions, `vix` bars, and `meta` ({symbol, name, exch, lot, step}).
    Entries are taken only on bars at/after `trade_from` (earlier bars warm
    the indicators up).
    """
    caches = caches if caches is not None else {}
    inst = spec["instrument"]
    pf = spec["portfolio"]
    slip = spec["costs"]["slippage_pct"] / 100
    with_ch = spec["costs"]["charges"]
    start, end = _hhmm(spec["session"]["start"]), _hhmm(spec["session"]["end"])
    sq = _hhmm(spec["session"]["square_off"])
    mtm_rules, daily = spec["mtm"], spec["daily"]
    syms = list(data)

    st: dict[str, dict[str, Any]] = {}
    for sym in syms:
        d = data[sym]
        d.setdefault("meta", {}).setdefault("symbol", sym)
        prep_key = repr(sorted(inst.items()))
        if d.get("_prep") != prep_key:
            prepare(d, spec)
            d["_prep"] = prep_key
        cache = caches.setdefault(sym, {})
        st[sym] = {"d": d, "sig": signals(d, spec, cache),
                   "atr": {p: series(d, {"ind": "ATR", "params": {"period": p}}, cache) for p in atr_periods(spec)},
                   "pos": None, "pending": None, "per_day": {}, "cool": 0, "u": None}

    events = sorted(((t, k, i) for k, sym in enumerate(syms) for i, t in enumerate(data[sym]["t"])))
    trades: list[dict[str, Any]] = []
    skipped = {"no_capital": 0, "max_positions": 0, "daily_halt": 0}
    realized = 0.0
    used_margin = 0.0
    open_n = 0
    open_syms: set[str] = set()
    day_key, day_realized, halted = None, 0.0, None

    def equity_now() -> float:
        return pf["capital"] + (realized if pf["compounding"] else 0.0)

    def mtm(pos: dict, u: float, t: float, iv: float) -> float:
        return sum(l["act"] * (_leg_px(l, u, t, iv) - l["entry"]) * l["qty"] for l in pos["legs"])

    def ref_at(pos: dict, u: float, t: float, iv: float) -> float:
        return u if pos["ref"] == "u" else _leg_px(pos["legs"][0], u, t, iv)

    def close(sym: str, u: float, t: float, reason: str, i: int, slipped: bool = True) -> None:
        nonlocal realized, used_margin, open_n, day_realized
        s_ = st[sym]
        pos, d = s_["pos"], s_["d"]
        iv = d.get("iv", [0.0] * len(d["t"]))[i] if inst["type"] == "option" else 0.0
        gross = chg = 0.0
        legs_out = []
        for l in pos["legs"]:
            px = _leg_px(l, u, t, iv)
            if slipped:
                px = px * (1 - slip) if l["act"] > 0 else px * (1 + slip)
            g, ch = leg_pnl(l["act"], l["qty"], l["entry"], px, l["kind"], with_ch)
            gross, chg = gross + g, chg + ch
            legs_out.append({"label": l["label"], "side": "BUY" if l["act"] > 0 else "SELL", "qty": l["qty"],
                             "entry": round(l["entry"], 2), "exit": round(px, 2)})
        net = gross - chg
        single = len(pos["legs"]) == 1
        trades.append({
            "symbol": sym, "instrument": " + ".join(l["label"] for l in legs_out), "side": pos["side"],
            "qty": pos["legs"][0]["qty"], "lots": pos["sets"],
            "entry_t": pos["t"], "exit_t": t,
            "entry": legs_out[0]["entry"] if single else round(pos["u0"], 2),
            "exit": legs_out[0]["exit"] if single else round(u, 2),
            "u_entry": round(pos["u0"], 2), "u_exit": round(u, 2),
            "reason": reason, "gross": round(gross, 2), "charges": round(chg, 2), "net": round(net, 2),
            "bars": i - pos["i"], "margin": round(pos["margin"], 2), "legs": legs_out,
            "stop": pos["sl"], "target": pos["tg"],
        })
        realized += net
        day_realized += net
        used_margin -= pos["margin"]
        open_n -= 1
        open_syms.discard(sym)
        s_["pos"] = None
        s_["cool"] = i + spec["cooldown_bars"]

    def open_(sym: str, side: str, i: int, sig_i: int) -> Optional[str]:
        """Fill at bar i's open. Returns a skip reason or None."""
        nonlocal used_margin, open_n
        s_ = st[sym]
        d = s_["d"]
        meta = d["meta"]
        u, t = d["o"][i], d["t"][i]
        iv = d["iv"][i] if inst["type"] == "option" else 0.0
        sign = 1 if side == "BUY" else -1
        lot = int(meta.get("lot") or 1) if inst["type"] != "equity" else 1
        legs: list[dict[str, Any]] = []
        if inst["type"] == "equity":
            legs.append({"kind": "EQ", "act": sign, "label": sym, "per_set": 1})
        elif inst["type"] == "future":
            exp = fno.expiry_after(meta["name"], meta["exch"], "monthly", inst["expiry"], t)
            legs.append({"kind": "FUT", "act": sign, "exp": exp, "per_set": lot,
                         "label": f"{meta['name']} FUT {fno.label_date(exp)}"})
        else:
            step = float(meta.get("step") or fno.default_step(meta["name"], u))
            for cfg in inst["legs_long" if side == "BUY" else "legs_short"]:
                exp = fno.expiry_after(meta["name"], meta["exch"], inst["expiry_kind"], inst["expiry"], t)
                tyrs = fno.years_to(exp, t)
                if cfg["strike"] == "PREMIUM":
                    k_ = fno.strike_for_premium(u, step, cfg["right"], cfg["premium"],
                                                lambda k: fno.bs_price(u, k, tyrs, iv, cfg["right"]))
                else:
                    k_ = fno.pick_strike(u, step, cfg["right"], cfg["strike"], cfg["steps"])
                legs.append({"kind": cfg["right"], "act": 1 if cfg["action"] == "BUY" else -1, "K": k_, "exp": exp,
                             "per_set": lot * cfg["lots"],
                             "label": f"{meta['name']} {fno.label_date(exp)} {k_:g} {cfg['right']}"})
        for l in legs:
            raw = _leg_px(l, u, t, iv)
            l["entry"] = raw * (1 + slip) if l["act"] > 0 else raw * (1 - slip)
        ref = "u" if inst["type"] == "equity" or inst["levels_on"] == "underlying" else 0
        rsign = sign if ref == "u" else legs[0]["act"]
        ref_entry = u if ref == "u" else legs[0]["entry"]
        lv = entry_levels(spec, rsign, ref_entry, {p: a[sig_i] for p, a in s_["atr"].items()})
        per_set_margin = position_margin(legs, u, pf["leverage"])
        per_set_risk = lv["sl_dist"] * legs[0]["per_set"] if lv["sl_dist"] else None
        eq = equity_now()
        sets = units(spec, eq, per_set_margin, per_set_risk)
        if per_set_margin > 0:
            sets = min(sets, int(max(0.0, eq - used_margin) // per_set_margin))
        if sets < 1:
            return "no_capital"
        for l in legs:
            l["qty"] = l["per_set"] * sets
        margin = per_set_margin * sets
        s_["pos"] = {"side": side, "legs": legs, "ref": ref, "sign": rsign, "ref_entry": ref_entry,
                     "sl": lv["sl"], "tg": lv["tg"], "trd": lv["trd"], "tra": lv["tra"], "be": lv["be"],
                     "be_on": False, "trail": None, "best": ref_entry, "peak": None,
                     "t": t, "i": i, "day": (t + IST) // 86400, "u0": u, "sets": sets, "margin": margin,
                     "exit_pending": None}
        used_margin += margin
        open_n += 1
        open_syms.add(sym)
        s_["per_day"][(t + IST) // 86400] = s_["per_day"].get((t + IST) // 86400, 0) + 1
        return None

    def check_intrabar(sym: str, i: int) -> None:
        s_ = st[sym]
        pos, d = s_["pos"], s_["d"]
        o, h, l, c, t = d["o"][i], d["h"][i], d["l"][i], d["c"][i], d["t"][i]
        iv = d["iv"][i] if inst["type"] == "option" else 0.0
        sgn = pos["sign"]
        later = i > pos["i"]
        f = lambda u: ref_at(pos, u, t, iv)  # noqa: E731
        stops = [x for x in (pos["sl"], pos["trail"], pos["ref_entry"] if pos["be_on"] else None) if x is not None]
        if stops:
            stop = max(stops) if sgn > 0 else min(stops)
            why = "TRAIL" if stop == pos["trail"] else "BREAKEVEN" if pos["be_on"] and stop == pos["ref_entry"] \
                and stop != pos["sl"] else "SL"
            ro = f(o)
            u_adv = min((h, l), key=lambda u: sgn * f(u))
            if later and sgn * (ro - stop) <= 0:
                return close(sym, o, t, why, i)
            if sgn * (f(u_adv) - stop) <= 0:
                return close(sym, _solve(f, stop, o, u_adv), t, why, i)
        if pos["tg"] is not None:
            ro = f(o)
            u_fav = max((h, l), key=lambda u: sgn * f(u))
            if later and sgn * (ro - pos["tg"]) >= 0:
                return close(sym, o, t, "TARGET", i, slipped=False)
            if sgn * (f(u_fav) - pos["tg"]) >= 0:
                return close(sym, _solve(f, pos["tg"], o, u_fav), t, "TARGET", i, slipped=False)
        if mtm_rules["stop"] or mtm_rules["target"] or mtm_rules["trail_start"]:
            m = lambda u: mtm(pos, u, t, iv)  # noqa: E731
            pts = sorted((o, h, l, c), key=m)
            lo_u, hi_u = pts[0], pts[-1]
            floor = -mtm_rules["stop"] if mtm_rules["stop"] else None
            if mtm_rules["trail_start"] and pos["peak"] is not None and pos["peak"] >= mtm_rules["trail_start"]:
                tfloor = pos["peak"] - mtm_rules["trail_gap"]
                if floor is None or tfloor > floor:
                    floor, why = tfloor, "MTM_TRAIL"
                else:
                    why = "MTM_SL"
            else:
                why = "MTM_SL"
            if floor is not None and m(lo_u) <= floor:
                return close(sym, o if m(o) <= floor else _solve(m, floor, o, lo_u), t, why, i)
            if mtm_rules["target"] and m(hi_u) >= mtm_rules["target"]:
                u_x = o if m(o) >= mtm_rules["target"] else _solve(m, mtm_rules["target"], o, hi_u)
                return close(sym, u_x, t, "MTM_TARGET", i, slipped=False)

    for t, k, i in events:
        sym = syms[k]
        s_ = st[sym]
        d = s_["d"]
        day, mod = (t + IST) // 86400, ((t + IST) % 86400) // 60
        if day != day_key:
            day_key, day_realized = day, 0.0
        if s_["pos"] and day != s_["pos"]["day"]:
            close(sym, d["c"][i - 1], d["t"][i - 1] + d["tf_s"], "EOD", i - 1)
        if s_["pos"] and (s_["pos"]["exit_pending"] or mod >= sq):
            close(sym, d["o"][i], t, s_["pos"]["exit_pending"] or "SQUARE_OFF", i)
        if s_["pending"]:
            p, s_["pending"] = s_["pending"], None
            if s_["pos"] is None and p["day"] == day and mod < sq:
                if halted == day:
                    skipped["daily_halt"] += 1
                elif open_n >= pf["max_positions"]:
                    skipped["max_positions"] += 1
                else:
                    why = open_(sym, p["side"], i, p["i"])
                    if why:
                        skipped[why] += 1
        if s_["pos"]:
            check_intrabar(sym, i)
        pos = s_["pos"]
        if pos:
            iv = d["iv"][i] if inst["type"] == "option" else 0.0
            f = lambda u: ref_at(pos, u, t, iv)  # noqa: E731
            sgn = pos["sign"]
            best_now = max((d["h"][i], d["l"][i]), key=lambda u: sgn * f(u))
            pos["best"] = max(pos["best"], f(best_now)) if sgn > 0 else min(pos["best"], f(best_now))
            gain = sgn * (pos["best"] - pos["ref_entry"])
            if pos["trd"] and gain >= pos["tra"]:
                nt = pos["best"] - sgn * pos["trd"]
                pos["trail"] = nt if pos["trail"] is None else (max if sgn > 0 else min)(pos["trail"], nt)
            if pos["be"] and gain >= pos["be"]:
                pos["be_on"] = True
            m_c = mtm(pos, d["c"][i], t, iv)
            pos["peak"] = m_c if pos["peak"] is None else max(pos["peak"], m_c)
            if s_["sig"]["exit_long" if pos["side"] == "BUY" else "exit_short"][i]:
                pos["exit_pending"] = "SIGNAL"
            elif spec["max_bars"] and i - pos["i"] + 1 >= spec["max_bars"]:
                pos["exit_pending"] = "TIME_STOP"
        s_["u"] = (d["c"][i], i)
        if (daily["max_loss"] or daily["max_profit"]) and halted != day:
            day_pnl = day_realized
            for s2 in (st[x] for x in open_syms):
                if s2["u"]:
                    j = s2["u"][1]
                    iv2 = s2["d"]["iv"][j] if inst["type"] == "option" else 0.0
                    day_pnl += mtm(s2["pos"], s2["u"][0], s2["d"]["t"][j], iv2)
            hit = ("DAILY_MAX_LOSS" if daily["max_loss"] and day_pnl <= -daily["max_loss"] else
                   "DAILY_TARGET" if daily["max_profit"] and day_pnl >= daily["max_profit"] else None)
            if hit:
                halted = day
                for s2 in st.values():
                    if s2["pos"]:
                        s2["pos"]["exit_pending"] = hit
                    s2["pending"] = None
        if (s_["pos"] is None and s_["pending"] is None and i + 1 < len(d["t"]) and i >= s_["cool"]
                and halted != day and (trade_from is None or t >= trade_from)):
            close_mod = mod + d["tf_s"] // 60
            if start <= close_mod <= end and s_["per_day"].get(day, 0) < spec["max_trades_per_day"]:
                side = "BUY" if s_["sig"]["entry_long"][i] else "SELL" if s_["sig"]["entry_short"][i] else None
                if side:
                    s_["pending"] = {"side": side, "day": day, "i": i}
    for sym in syms:
        if st[sym]["pos"]:
            d = st[sym]["d"]
            close(sym, d["c"][-1], d["t"][-1] + d["tf_s"], "END", len(d["t"]) - 1)

    trades.sort(key=lambda x: x["entry_t"])
    per_symbol = {}
    for sym in syms:
        mine = [x for x in trades if x["symbol"] == sym]
        s2 = stats(mine, pf["capital"])
        per_symbol[sym] = {k: s2[k] for k in ("trades", "win_rate", "net_pnl", "profit_factor", "max_drawdown")} \
            | {"bars": len(data[sym]["t"])}
    out = stats(trades, pf["capital"])
    out["skipped"] = skipped
    return {"stats": out, "per_symbol": per_symbol, "by_reason": by_reason(trades), "trades": trades}


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
    monthly: dict[str, float] = {}
    weekday: dict[int, list[float]] = {}
    for x in trades:
        k = (x["exit_t"] + IST) // 86400
        daily[k] = daily.get(k, 0.0) + x["net"]
        ym = _ym(k)
        monthly[ym] = round(monthly.get(ym, 0.0) + x["net"], 2)
        weekday.setdefault((k + 3) % 7, []).append(x["net"])
    dv = list(daily.values())
    mean = sum(dv) / len(dv) if dv else 0.0
    sd = math.sqrt(sum((v - mean) ** 2 for v in dv) / (len(dv) - 1)) if len(dv) > 1 else 0.0
    down = [v for v in dv if v < 0]
    dsd = math.sqrt(sum(v * v for v in down) / len(dv)) if down and dv else 0.0
    streak = {"win": 0, "loss": 0}
    cur_w = cur_l = 0
    for v in nets:
        cur_w, cur_l = (cur_w + 1, 0) if v > 0 else (0, cur_l + 1)
        streak["win"], streak["loss"] = max(streak["win"], cur_w), max(streak["loss"], cur_l)
    gross_win, gross_loss = sum(wins), -sum(losses)
    net = sum(nets)
    days_span = ((trades[-1]["exit_t"] - trades[0]["entry_t"]) / 86400) if n else 0
    final = capital + net
    cagr = ((final / capital) ** (365 / days_span) - 1) * 100 if n and days_span >= 30 and final > 0 else None
    return {
        "trades": n, "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / n * 100, 2) if n else 0.0,
        "gross_pnl": round(sum(x["gross"] for x in trades), 2),
        "charges": round(sum(x["charges"] for x in trades), 2),
        "net_pnl": round(net, 2),
        "return_pct": round(net / capital * 100, 2) if capital else 0.0,
        "cagr_pct": round(cagr, 2) if cagr is not None else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else (None if not gross_win else 999.0),
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "expectancy": round(net / n, 2) if n else 0.0,
        "largest_win": round(max(nets), 2) if nets else 0.0,
        "largest_loss": round(min(nets), 2) if nets else 0.0,
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "sharpe": round(mean / sd * math.sqrt(252), 2) if sd > 0 else None,
        "sortino": round(mean / dsd * math.sqrt(252), 2) if dsd > 0 else None,
        "calmar": round((cagr or 0) / max_dd_pct, 2) if cagr is not None and max_dd_pct > 0 else None,
        "max_consecutive_wins": streak["win"], "max_consecutive_losses": streak["loss"],
        "avg_bars_held": round(sum(x["bars"] for x in trades) / n, 1) if n else 0.0,
        "trading_days": len(daily),
        "profitable_days": sum(1 for v in dv if v > 0),
        "best_day": round(max(dv), 2) if dv else 0.0,
        "worst_day": round(min(dv), 2) if dv else 0.0,
        "equity": curve,
        "daily": [[k * 86400 - IST + 86400 // 2, round(v, 2)] for k, v in sorted(daily.items())],
        "monthly": monthly,
        "weekday": {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][k]: round(sum(v), 2)
                    for k, v in sorted(weekday.items())},
    }


def _ym(day_index: int) -> str:
    import datetime as _dt

    d = _dt.date(1970, 1, 1) + _dt.timedelta(days=day_index)
    return f"{d.year}-{d.month:02d}"


def by_reason(trades: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for x in trades:
        r = out.setdefault(x["reason"], {"count": 0, "net": 0.0})
        r["count"] += 1
        r["net"] = round(r["net"] + x["net"], 2)
    return out


# ---- optimisation ----------------------------------------------------------

def set_path(obj: Any, path: str, value: Any) -> None:
    """Set e.g. "entry_long.conditions.0.left.params.period" in place."""
    keys = [int(k) if k.isdigit() else k for k in path.split(".")]
    for k in keys[:-1]:
        if isinstance(obj, dict) and obj.get(k) is None:
            obj[k] = {}
        obj = obj[k]
    if isinstance(obj, list) and not isinstance(keys[-1], int):
        raise ValueError(f"bad path {path!r}")
    obj[keys[-1]] = value


MAX_COMBOS = 400
METRICS = ("net_pnl", "profit_factor", "sharpe", "sortino", "win_rate", "return_pct", "expectancy", "calmar")


def optimize(spec: dict, data: dict[str, dict], grid: list[dict[str, Any]], metric: str = "net_pnl",
             min_trades: int = 5, trade_from: Optional[int] = None) -> dict[str, Any]:
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
        st = run(s, data, caches, trade_from)["stats"]
        rows.append({"params": {p: v for (p, _), v in zip(axes, combo)},
                     **{k: st[k] for k in ("trades", "win_rate", "net_pnl", "profit_factor", "sharpe", "sortino",
                                           "calmar", "max_drawdown", "return_pct", "expectancy")}})
    ok = [r for r in rows if r["trades"] >= min_trades]
    ok.sort(key=lambda r: (r[metric] is not None, r[metric] or 0), reverse=True)
    return {"combos": len(combos), "ranked": ok[:100],
            "too_few_trades": len(rows) - len(ok), "metric": metric}
