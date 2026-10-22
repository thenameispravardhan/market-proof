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
  * scale-out levels close a % of the entry size, filled like a target; each
    part is its own trade and the rest keeps its stop / trail / target;
  * MTM (rupee, or % of the entry premium) stop / target / trail work on the
    whole position — the way to manage a multi-leg option position;
  * a leg's own stop / target (% of its premium) closes just that leg inside
    the bar; its fill stays in the position, which can then move the other
    legs' stops to cost or exit them all;
  * position rules on the option position's Greeks / premium filter entries
    (checked on the legs about to be opened) and exit on a candle's close;
  * daily max loss / max profit flatten the strategy and stop it for the day;
  * everything is flat by `square_off`; with square-off switched off the
    position carries forward (overnight) until an exit rule closes it.

The portfolio is one cash ledger across all symbols: positions compete for
capital (leverage applies to equity/futures/option writing, never to option
premium paid), `max_positions` caps concurrency, and with compounding the
size of the next trade follows the equity curve.
"""
from __future__ import annotations

import copy
import itertools
import math
import statistics
from typing import Any, Callable, Optional

from app.algo import fno
from app.algo import indicators as ind

IST = ind.IST_OFFSET
SESSION_CLOSE_S = 15 * 3600 + 30 * 60
TIMEFRAMES = (1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 120)
COND_TIMEFRAMES = TIMEFRAMES + (1440,)          # 1440 = daily, conditions only
OPS = (">", "<", ">=", "<=", "==", "!=", "crosses_above", "crosses_below", "crosses",
       "rising", "falling", "between", "outside")
# An operand's series reshaped before it is compared: its angle, slope, change, … over `xn` bars.
# angle: degrees of the line's rise over xn bars against ATR(14) of the strategy's candles — a move of one
# ATR per bar is 45°, so the number means the same on any symbol, price level or chart zoom.
MAX_LOOKBACK = 500
# rank: where the value sits in its N-bar range, 0 = the lowest, 100 = the highest (IV rank on VIX);
# percentile: % of the previous N bars that were below it (IV percentile).
TRANSFORMS = ("angle", "slope", "change", "pct_change", "highest", "lowest", "sma", "zscore", "abs",
              "rank", "percentile")
MATH = ("+", "-", "*", "/", "pct_diff")
INSTRUMENTS = ("equity", "future", "option")
SIZING = ("qty", "lots", "amount", "pct_equity", "risk", "risk_pct")
STRIKE_MODES = ("ATM", "ITM", "OTM", "PREMIUM", "DELTA")
MARGIN_MODELS = ("leverage", "span")

DEFAULT_SPEC: dict[str, Any] = {
    "symbols": [],
    "timeframe": 5,
    # time = every `timeframe` minutes; volume / turnover = a candle closes once
    # that much has traded, sized so a typical session has ~per_day candles.
    "bars": {"type": "time", "per_day": 50},
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
    "session": {"start": "09:20", "end": "15:00", "square_off": "15:15"},   # any of them null = off
    "entry_order": {"type": "market", "offset_pct": 0.1, "valid_bars": 3},  # market | pullback | breakout
    "max_trades_per_day": 3,
    "cooldown_bars": 0,
    "max_bars": None,
    "sizing": {"mode": "qty", "value": 1},
    "portfolio": {"capital": 100000, "leverage": 1.0, "max_positions": 10, "compounding": True,
                  "max_position_pct": None},                       # cap one position's margin at X% of equity
    "costs": {"slippage_pct": 0.02, "charges": True},
}
# Optional, absent = off, and left out of a normalized spec when off — so a
# spec saved before they existed normalizes to exactly what it was:
#   "scale_out": [{"type": pct|points|atr|rr, "value", "atr_period", "qty_pct"}]
#       partial exits: at each level close qty_pct % of the ENTRY size;
#   "session": {"days": [1..7]} — entries only on these weekdays (1 = Mon);
#   "margin": {"model": "span", "scan_pct", "exposure_pct"} — F&O blocks an
#       exchange-style SPAN + exposure estimate (fno.span_margin) instead of
#       notional / leverage; scan / exposure None = the index / stock defaults;
#   on a leg: "expiry": current|next (its own expiry: calendars / diagonals),
#       "delta": 0.01-0.99 with strike "DELTA" (the strike nearest that |delta|),
#       "sl_pct" / "tg_pct": that leg's own stop / target, % of its entry premium;
#   "instrument": {"on_leg_stop": "cost" | "all"} — when a leg's stop hits, move
#       the other legs' stops to their entry price, or exit every leg (absent =
#       only that leg exits);
#   "mtm": {"stop_pct", "target_pct"} — position stop / target as % of the
#       net premium at entry (50 = half the credit); the tighter of this and ₹;
#   "position_rules": {"entry": [rule], "exit": [rule]}, rule = {"metric", "op",
#       "value"} on the option position's Greeks / premium (POSITION_METRICS):
#       entries only when every entry rule holds, exit when any exit rule does.
OPTIONAL_KEYS = ("scale_out", "margin", "position_rules")
# Per set (one set = the legs at their configured lots): delta in lots of the
# underlying (1 = one lot of the future), gamma = delta change for a 1% move,
# theta ₹ per day, vega ₹ per vol point, credit ₹ (net premium received; a
# debit is negative); pnl_pct = P&L as % of the entry premium; dte = days to
# the nearest open leg's expiry; iv = the open legs' average IV %.
POSITION_METRICS = ("delta", "abs_delta", "gamma", "theta", "vega", "credit", "pnl_pct", "dte", "iv")
RULE_OPS = (">", "<", ">=", "<=")
LEG_STOP_ACTIONS = ("leg", "cost", "all")
SCALE_KINDS = ("pct", "points", "atr", "rr")


CARRY = 24 * 60   # square-off minute meaning "never" (no clock minute reaches it)


def session_window(spec: dict[str, Any]) -> tuple[int, int, int]:
    """(entries from, entries until, square off) in IST minutes. A switched-off
    entry time falls back to the session edge (09:15 / 15:29); a switched-off
    square-off is CARRY: positions are never time-squared and carry overnight."""
    sess = spec["session"]
    sq = _hhmm(sess["square_off"]) if sess.get("square_off") else CARRY
    start = _hhmm(sess["start"]) if sess.get("start") else 9 * 60 + 15
    end = _hhmm(sess["end"]) if sess.get("end") else min(sq, 15 * 60 + 29)
    return start, end, sq


def day_allowed(spec: dict[str, Any], t: float) -> bool:
    """Entries on this IST weekday? (session.days, 1 = Mon; absent = every day)"""
    days = spec["session"].get("days")
    return not days or ((int(t) + IST) // 86400 + 3) % 7 + 1 in days


TICK = 0.05   # the NSE / BSE price grid: every price an order can carry is a multiple of it


def tick_for(symbol: str, meta: Optional[dict] = None) -> Optional[float]:
    """The price grid for `symbol`: meta's own `tick` if given, else 0.05 on
    NSE / BSE; None (no snapping) elsewhere — MCX and currency ticks differ."""
    if meta and meta.get("tick"):
        return float(meta["tick"])
    return TICK if (symbol or "").split(":", 1)[0].upper() in ("NSE", "BSE") else None


def snap(px: Optional[float], tick: Optional[float], side: int = 0) -> Optional[float]:
    """`px` on the price grid: rounded up for a buy filling against you
    (side > 0), down for a sell (side < 0), else to the nearest step. Never
    below one tick (an option's floor)."""
    if px is None or not tick or not math.isfinite(px):
        return px
    n = px / tick
    k = math.ceil(n - 1e-6) if side > 0 else math.floor(n + 1e-6) if side < 0 else math.floor(n + 0.5)
    return round(max(k, 1) * tick, 6)


def entry_trigger(spec: dict[str, Any], side: str, close: float, tick: Optional[float] = TICK) -> Optional[float]:
    """Price the underlying must trade at for a non-market entry. pullback =
    X% better than the signal close (limit-style), breakout = X% beyond it in
    the trade's direction (stop-style). On the price grid."""
    eo = spec["entry_order"]
    if eo["type"] == "market":
        return None
    k = eo["offset_pct"] / 100
    better = -1 if side == "BUY" else 1
    sign = better if eo["type"] == "pullback" else -better
    return snap(close * (1 + sign * k), tick)


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
        if k in ("session", "costs", "sizing", "portfolio", "mtm", "daily", "instrument", "entry_order", "bars") \
                and isinstance(v, dict):
            s[k] = {**s[k], **v}
        elif k in s or k == "name" or k in OPTIONAL_KEYS:
            s[k] = v
    s["symbols"] = list(dict.fromkeys(str(x).strip().upper() for x in s["symbols"] if str(x).strip()))
    if not s["symbols"]:
        raise ValueError("add at least one symbol")
    if len(s["symbols"]) > 50:
        raise ValueError("at most 50 symbols per strategy")
    s["timeframe"] = int(s["timeframe"])
    if s["timeframe"] not in TIMEFRAMES:
        raise ValueError(f"timeframe must be one of {TIMEFRAMES} minutes")
    if s["bars"].get("type") not in ("time", "volume", "turnover"):
        raise ValueError("candles are time, volume or turnover")
    s["bars"]["per_day"] = _num(s["bars"].get("per_day", 50), "candles per day", 5, integer=True)
    if s["bars"]["per_day"] > 1000:
        raise ValueError("at most 1000 candles per day")
    if s["bars"]["type"] != "time":
        s["timeframe"] = 1        # volume/turnover candles are built from 1-minute data
    if s["direction"] not in ("long", "short", "both"):
        raise ValueError("direction must be long, short or both")
    for k in ("start", "end", "square_off"):
        if s["session"].get(k) in ("", None):
            s["session"][k] = None
    start, end, sq = session_window(s)
    if not 9 * 60 + 15 <= start <= end <= min(sq, 15 * 60 + 29):
        raise ValueError("session must satisfy 09:15 <= start <= end <= square_off <= 15:29")
    days = s["session"].pop("days", None)
    if days:
        try:
            days = sorted({int(x) for x in days})
        except (TypeError, ValueError):
            raise ValueError("trading days are weekday numbers 1 (Mon) to 7 (Sun)") from None
        if not set(days) <= set(range(1, 8)):
            raise ValueError("trading days are weekday numbers 1 (Mon) to 7 (Sun)")
        s["session"]["days"] = days
    eo = s["entry_order"]
    if eo.get("type") not in ("market", "pullback", "breakout"):
        raise ValueError("entry order is market, pullback or breakout")
    eo["offset_pct"] = _num(eo.get("offset_pct", 0.1), "entry offset %", 0)
    eo["valid_bars"] = _num(eo.get("valid_bars", 3), "entry valid bars", 1, integer=True)
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
    for k, label in (("put_skew", "put IV skew"), ("call_skew", "call IV skew")):
        v = _num(inst["iv"].pop(k, None), label, -5, allow_none=True)       # absent / 0 = flat (left out)
        if v:
            if v > 5:
                raise ValueError(f"{label} is -5 to 5 IV points per 1% out of the money (0.8 = typical NIFTY puts)")
            inst["iv"][k] = v
    opt = inst["type"] == "option"
    multi = False
    for key, used in (("legs_long", s["direction"] != "short"), ("legs_short", s["direction"] != "long")):
        legs = inst.get(key) or []
        for lg in legs:
            # FUT = the underlying's future in the same position: a protective
            # put / covered call / collar is FUT + option legs.
            if lg.get("right") not in ("CE", "PE", "FUT") or lg.get("action") not in ("BUY", "SELL"):
                raise ValueError("each leg needs right CE|PE|FUT and action BUY|SELL")
            if lg.get("strike", "ATM") not in STRIKE_MODES:
                raise ValueError(f"strike must be one of {STRIKE_MODES}")
            lg["strike"] = lg.get("strike", "ATM")
            if lg["strike"] == "DELTA":
                lg["delta"] = _num(lg.get("delta", 0.3), "target delta", 0.01)
                if lg["delta"] > 0.99:
                    raise ValueError("target delta is 0.01-0.99 (0.25 = a 25-delta option)")
            else:
                lg.pop("delta", None)
            if lg.get("expiry") in (None, ""):
                lg.pop("expiry", None)
            elif lg["expiry"] not in ("current", "next") or lg["right"] == "FUT":
                raise ValueError("a leg's own expiry is current or next (option legs only)")
            for k, label in (("sl_pct", "leg stop %"), ("tg_pct", "leg target %")):
                if lg.get(k) in (None, "", 0):
                    lg.pop(k, None)
                elif lg["right"] == "FUT":
                    raise ValueError("a leg's own stop / target is for option legs (FUT legs use the position's)")
                else:
                    lg[k] = _num(lg[k], label, 1e-9)
                    if lg[k] > 1000:
                        raise ValueError(f"{label} must be at most 1000")
            if lg.get("reentries") in (None, "", 0):
                lg.pop("reentries", None)
            else:
                lg["reentries"] = _num(lg["reentries"], "leg re-entries", 0, integer=True)
                if lg["reentries"] > 5:
                    raise ValueError("leg re-entries are 0-5 per position")
                if lg["right"] == "FUT":
                    raise ValueError("re-entries are for option legs (a FUT leg has no stop of its own)")
            if not lg.get("roll"):
                lg.pop("roll", None)
            else:
                ro = lg["roll"]
                if lg["right"] == "FUT" or not isinstance(ro, dict):
                    raise ValueError('roll is {"above": |delta|, "to": |delta|, "max": n} on a CE / PE leg')
                ro = {"above": _num(ro.get("above"), "roll above |delta|", 1e-9),
                      "to": _num(ro.get("to"), "roll to |delta|", 1e-9),
                      "max": _num(ro.get("max", 1), "rolls per position", 1, integer=True)}
                if not ro["to"] < ro["above"] < 1:
                    raise ValueError("a roll needs 0 < to < above < 1 (|delta| per unit: above 0.45, to 0.25)")
                if ro["max"] > 5:
                    raise ValueError("rolls per position are 1-5")
                lg["roll"] = ro
            lg["steps"] = _num(lg.get("steps", 0), "strike steps", 0, integer=True)
            lg["premium"] = _num(lg.get("premium", 100), "target premium", 0.05)
            # 0 lots = leg switched off, so the optimiser can sweep a hedge on / off.
            lg["lots"] = _num(lg.get("lots", 1), "leg lots", 0, integer=True)
        active = [lg for lg in legs if lg["lots"]]
        if opt and used and not active:
            raise ValueError(f"options need at least one leg with lots in {key}")
        multi = multi or (opt and used and len(active) > 1)
    leg_stops = opt and any(lg.get("sl_pct") or lg.get("tg_pct")
                            for key in ("legs_long", "legs_short") for lg in inst.get(key) or [])
    if inst.get("on_leg_stop") in (None, "", "leg"):
        inst.pop("on_leg_stop", None)
    elif inst["on_leg_stop"] not in LEG_STOP_ACTIONS:
        raise ValueError(f"on a leg's stop: one of {LEG_STOP_ACTIONS}")

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
        if multi and inst["levels_on"] == "instrument":
            raise ValueError("multi-leg positions can't use price levels on 'the instrument' — "
                             "use MTM ₹ stops/targets or put levels on the underlying")
    if s["target"] and s["target"]["type"] == "rr" and not s["stop_loss"]:
        raise ValueError("an R:R target needs a stop loss")
    so = copy.deepcopy(s.pop("scale_out", None) or [])
    if so:
        if not isinstance(so, list) or len(so) > 5:
            raise ValueError("scale-out takes at most 5 steps")
        for st in so:
            if not isinstance(st, dict) or st.get("type") not in SCALE_KINDS:
                raise ValueError(f"scale-out: type must be one of {SCALE_KINDS}")
            st["value"] = _num(st.get("value"), "scale-out level", 1e-9)
            st["atr_period"] = _num(st.get("atr_period", 14), "scale-out ATR period", 1, integer=True)
            st["qty_pct"] = _num(st.get("qty_pct"), "scale-out % of the position", 1e-9)
            if st["type"] == "rr" and not s["stop_loss"]:
                raise ValueError("a scale-out at an R multiple needs a stop loss")
        if sum(st["qty_pct"] for st in so) > 100 + 1e-9:
            raise ValueError("scale-out steps close more than 100% of the position")
        if multi and inst["levels_on"] == "instrument":
            raise ValueError("multi-leg positions can't scale out on 'the instrument' — put levels on the underlying")
        if leg_stops:
            raise ValueError("scale-out and per-leg stops / targets can't be combined — use one or the other")
        s["scale_out"] = so
    mg = s.pop("margin", None)
    if mg:
        if not isinstance(mg, dict) or mg.get("model", "leverage") not in MARGIN_MODELS:
            raise ValueError(f"margin model is one of {MARGIN_MODELS}")
        if mg.get("model", "leverage") == "span":
            s["margin"] = {"model": "span"}
            for k, label in (("scan_pct", "SPAN price scan %"), ("exposure_pct", "exposure margin %")):
                s["margin"][k] = _num(mg.get(k), label, 0, allow_none=True)
                if s["margin"][k] is not None and s["margin"][k] > 50:
                    raise ValueError(f"{label} must be at most 50")

    for k in ("stop", "target", "trail_start", "trail_gap"):
        s["mtm"][k] = _num(s["mtm"].get(k), f"MTM {k}", 1e-9, allow_none=True)
    for k, label in (("stop_pct", "MTM stop % of premium"), ("target_pct", "MTM target % of premium")):
        v = _num(s["mtm"].pop(k, None), label, 1e-9, allow_none=True)
        if v is not None:
            if not opt:
                raise ValueError(f"{label} needs option legs (it is a share of their premium)")
            s["mtm"][k] = v
    pr = s.pop("position_rules", None) or {}
    if not isinstance(pr, dict):
        raise ValueError("position_rules is {entry: [...], exit: [...]}")
    rules = {}
    for k in ("entry", "exit"):
        lst = copy.deepcopy(pr.get(k) or [])
        if not isinstance(lst, list) or len(lst) > 10:
            raise ValueError(f"position_rules.{k} takes at most 10 rules")
        for r in lst:
            if not isinstance(r, dict) or r.get("metric") not in POSITION_METRICS:
                raise ValueError(f"position rule metric is one of {POSITION_METRICS}")
            if r.get("op") not in RULE_OPS:
                raise ValueError(f"position rule op is one of {RULE_OPS}")
            r["value"] = _num(r.get("value"), f"{r['metric']} rule value", -1e12)
        if lst:
            rules[k] = lst
    hd = pr.get("hedge")
    if hd:
        if not isinstance(hd, dict):
            raise ValueError('position_rules.hedge is {"above": |delta| in lots, "max": hedges}')
        rules["hedge"] = {"above": _num(hd.get("above"), "hedge above |delta| (lots)", 1e-9),
                          "max": _num(hd.get("max", 10), "hedges per position", 1, integer=True)}
        if rules["hedge"]["max"] > 50:
            raise ValueError("hedges per position are 1-50")
    if rules:
        if not opt:
            raise ValueError("position rules (Greeks / premium) need an option instrument")
        s["position_rules"] = rules
    _check_adjustments(s)
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
    pf["max_position_pct"] = _num(pf.get("max_position_pct"), "max % per position", 1e-9, allow_none=True)
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
        for k in ("for_bars", "within_bars"):
            if c.get(k) not in (None, "", 0):
                c[k] = _num(c[k], k.replace("_", " "), 1, integer=True)
                if c[k] > 500:
                    raise ValueError(f"{k.replace('_', ' ')} must be at most 500")
        if c.get("negate") is not None:
            c["negate"] = bool(c["negate"])
        sides = ("left", "right", "right2") if c["op"] in ("between", "outside") else ("left", "right")
        for side in sides:
            o = c.get(side)
            if not isinstance(o, dict) or ("ind" not in o and "value" not in o and "math" not in o):
                if side == "right" and c["op"] in ("rising", "falling"):
                    continue
                raise ValueError(f"condition {side} operand missing"
                                 + (" (between / outside need a low and a high)" if side == "right2" else ""))
            _check_operand(o, side, base_tf)


def _check_operand(o: dict[str, Any], side: str, base_tf: int, depth: int = 0) -> None:
    if o.get("xf") not in (None, ""):
        if o["xf"] not in TRANSFORMS:
            raise ValueError(f"unknown transform {o['xf']!r} (one of {TRANSFORMS})")
        o["xn"] = _num(o.get("xn") or 1, f"{o['xf']} bars", 1, integer=True)
        if o["xn"] > MAX_LOOKBACK:
            raise ValueError(f"{o['xf']} bars must be at most {MAX_LOOKBACK}")
    if "math" in o:
        if depth >= 3:
            raise ValueError("combined values nest at most 3 deep")
        if o["math"] not in MATH:
            raise ValueError(f"unknown combination {o['math']!r} (one of {MATH})")
        for k in ("a", "b"):
            if not isinstance(o.get(k), dict):
                raise ValueError(f"combined value is missing its {'first' if k == 'a' else 'second'} part")
            _check_operand(o[k], side, base_tf, depth + 1)
        for k in ("mult", "add"):
            if o.get(k) not in (None, ""):
                o[k] = _num(o[k], k, -1e12)
        return
    if "ind" not in o:
        o["value"] = _num(o["value"], f"{side} number", -1e15)
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
        # params are checked (and stored normalized) here, so a bad
        # length fails on save instead of at the first live bar
        o["params"] = {k: v for k, v in ind.normalize_params(o["ind"], o.get("params")).items()
                       if k in (o.get("params") or {})}
        if o.get("offset") not in (None, ""):
            o["offset"] = _num(o["offset"], f"{o['ind']} offset (bars ago)", 0, integer=True)


def _walk_operands(spec: dict[str, Any]):
    def walk(g):
        for c in (g or {}).get("conditions", []):
            if "conditions" in c:
                yield from walk(c)
            else:
                for o in (c.get("left"), c.get("right"), c.get("right2")):
                    yield from leaves(o)

    def leaves(o):
        if isinstance(o, dict) and "math" in o:
            yield from leaves(o.get("a"))
            yield from leaves(o.get("b"))
        elif isinstance(o, dict) and "ind" in o:
            yield o
    for k in ("entry_long", "exit_long", "entry_short", "exit_short"):
        yield from walk(spec.get(k))


# Indicators that need buy / sell flow (recorded ticks, else per-minute BVC
# estimates). RVOL is NOT one: it only compares candle volume by time of day.
ORDER_FLOW = {"DELTA", "CVD", "CVD_DIVERGENCE", "VPROFILE", "BAR_FLOW", "FLOW_PRESSURE"}


def vix_timeframes(spec: dict[str, Any]) -> set[int]:
    """Timeframes (minutes) at which conditions read India VIX — the loaders
    attach VIX candles to those bars."""
    return {int(o.get("tf") or 0) or spec["timeframe"] for o in _walk_operands(spec) if o["ind"] == "VIX"}


def bar_minutes(spec: dict[str, Any]) -> float:
    """Typical candle length in minutes — for turning 'N bars' into time live."""
    return spec["timeframe"] if spec["bars"]["type"] == "time" else 375 / spec["bars"]["per_day"]


def needs_minutes(spec: dict[str, Any]) -> bool:
    """Build candles from 1-minute data (volume/turnover candles, or order-flow
    indicators that should see each minute's flow, not the candle as a lump)."""
    return spec["bars"]["type"] != "time" or any(o["ind"] in ORDER_FLOW for o in _walk_operands(spec))


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
    for xn in _transform_lengths(spec):
        longest = max(longest, xn)
    return min(1000, 3 * longest + 5)


def _transform_lengths(spec: dict[str, Any]):
    def walk(g):
        for c in (g or {}).get("conditions", []):
            if "conditions" in c:
                yield from walk(c)
                continue
            yield int(c.get("for_bars") or 0) + int(c.get("within_bars") or 0)
            stack = [c.get("left"), c.get("right"), c.get("right2")]
            while stack:
                o = stack.pop()
                if isinstance(o, dict):
                    yield int(o.get("xn") or 0) if o.get("xf") else 0
                    stack += [o.get("a"), o.get("b")]
    for k in ("entry_long", "exit_long", "entry_short", "exit_short"):
        yield from walk(spec.get(k))


# ---- signals ---------------------------------------------------------------

def _close_times(b: dict) -> list[int]:
    if "tc" in b:            # volume/turnover candles carry their real close times
        return [min(tc, t - (t + IST) % 86400 + SESSION_CLOSE_S) for t, tc in zip(b["t"], b["tc"])]
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


def _combine(how: str, a: ind.S, b: ind.S) -> ind.S:
    def f(x, y):
        if x is None or y is None:
            return None
        if how == "+":
            return x + y
        if how == "-":
            return x - y
        if how == "*":
            return x * y
        if how == "/":
            return x / y if y else None
        return (x - y) / y * 100 if y else None          # pct_diff: how far a is from b, in % of b
    return [f(x, y) for x, y in zip(a, b)]


def _rank(s: ind.S, n: int, how: str) -> ind.S:
    """rank: 0-100 position of each value between the lowest and highest of
    the last n (itself included; 50 when flat). percentile: % of the previous
    n values strictly below it. A gap mid-series yields no values, as above."""
    import bisect

    lo = next((i for i, v in enumerate(s) if v is not None), len(s))
    if any(v is None for v in s[lo:]):
        return [None] * len(s)
    out: ind.S = [None] * len(s)
    if how == "rank":
        hi_, lo_ = ind.highest(s, n), ind.lowest(s, n)
        for i in range(lo, len(s)):
            if hi_[i] is not None and lo_[i] is not None:
                out[i] = 50.0 if hi_[i] == lo_[i] else (s[i] - lo_[i]) / (hi_[i] - lo_[i]) * 100
        return out
    window: list[float] = []                 # the previous n values, sorted
    for i in range(lo, len(s)):
        if len(window) == n:
            out[i] = bisect.bisect_left(window, s[i]) / n * 100
            del window[bisect.bisect_left(window, s[i - n])]
        bisect.insort(window, s[i])
    return out


def _transform(d: dict, s: ind.S, how: str, n: int, cache: dict) -> ind.S:
    if how == "abs":
        return [None if v is None else abs(v) for v in s]
    if how in ("rank", "percentile"):
        return _rank(s, n, how)
    if how in ("highest", "lowest", "sma", "zscore"):
        lo = next((i for i, v in enumerate(s) if v is not None), len(s))
        if any(v is None for v in s[lo:]):              # a gap mid-series: no window reaches across it
            return [None] * len(s)
        fn = {"highest": ind.highest, "lowest": ind.lowest, "sma": ind.sma, "zscore": ind.zscore}[how]
        return fn(s, n)
    prev = ind.shift(s, n)
    if how == "angle":
        key = ("atr14", id(d))
        if key not in cache:
            cache[key] = ind.atr(d, 14)
        atr = cache[key]
        return [None if x is None or y is None or not a else math.degrees(math.atan((x - y) / (n * a)))
                for x, y, a in zip(s, prev, atr)]
    if how == "slope":
        return [None if x is None or y is None else (x - y) / n for x, y in zip(s, prev)]
    if how == "change":
        return [None if x is None or y is None else x - y for x, y in zip(s, prev)]
    return [None if x is None or y is None or not y else (x - y) / y * 100 for x, y in zip(s, prev)]   # pct_change


def _shape(d: dict, op: dict[str, Any], s: ind.S, cache: dict) -> ind.S:
    if op.get("xf"):
        s = _transform(d, s, op["xf"], int(op.get("xn") or 1), cache)
    mult = float(op.get("mult") if op.get("mult") not in (None, "") else 1)
    add = float(op.get("add") or 0)
    if mult != 1 or add:
        s = [None if v is None else v * mult + add for v in s]
    return s


def series(d: dict, op: dict[str, Any], cache: dict) -> ind.S:
    n = len(d["c"])
    if "math" in op:
        return _shape(d, op, _combine(op["math"], series(d, op["a"], cache), series(d, op["b"], cache)), cache)
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
    return _shape(d, op, s, cache)


def _cond(d: dict, c: dict[str, Any], cache: dict) -> list[bool]:
    """A condition per bar, then its modifiers: held for N bars in a row, or true at least once in the
    last N bars, then NOT."""
    raw = _cond_raw(d, c, cache)
    k = int(c.get("for_bars") or 0)
    if k > 1:
        run, out = 0, []
        for v in raw:
            run = run + 1 if v else 0
            out.append(run >= k)
        raw = out
    w = int(c.get("within_bars") or 0)
    if w > 1:
        last, out = -10**9, []
        for i, v in enumerate(raw):
            if v:
                last = i
            out.append(i - last < w)
        raw = out
    return [not v for v in raw] if c.get("negate") else raw


def _cond_raw(d: dict, c: dict[str, Any], cache: dict) -> list[bool]:
    op = c["op"]
    a = series(d, c["left"], cache)
    n = len(a)
    if op in ("rising", "falling"):
        k = max(1, int((c.get("right") or {}).get("value", 1)))
        sgn = 1 if op == "rising" else -1
        return [i >= k and a[i] is not None and a[i - k] is not None and sgn * (a[i] - a[i - k]) > 0
                for i in range(n)]
    b = series(d, c["right"], cache)
    if op in ("between", "outside"):
        b2 = series(d, c["right2"], cache)
        inside = [x is not None and y is not None and z is not None and min(y, z) <= x <= max(y, z)
                  for x, y, z in zip(a, b, b2)]
        if op == "between":
            return inside
        return [not v and None not in (x, y, z) for v, x, y, z in zip(inside, a, b, b2)]
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


def entry_levels(spec: dict, sign: int, ref: float, atr_vals: dict[int, Optional[float]],
                 tick: Optional[float] = TICK) -> dict[str, Optional[float]]:
    """Stop / target / trail gap / trail activation / breakeven trigger for a
    position whose reference price `ref` profits in direction `sign`. Stop,
    target and scale-out prices sit on the price grid (`tick`)."""
    g = lambda lv, key="value": _dist(lv, ref, atr_vals.get(lv["atr_period"]) if lv else None, key)  # noqa: E731
    sl_lv, tg_lv, tr_lv = spec["stop_loss"], spec["target"], spec["trailing"]
    sl_d = g(sl_lv)
    tg_d = (sl_d * tg_lv["value"] if sl_d else None) if tg_lv and tg_lv["type"] == "rr" else g(tg_lv)
    so = []                                      # scale-out distances, nearest first
    for st in spec.get("scale_out") or []:
        dd = (sl_d * st["value"] if sl_d else None) if st["type"] == "rr" else g(st)
        if dd:
            so.append((dd, st["qty_pct"]))
    return {"sl": snap(ref - sign * sl_d, tick) if sl_d else None,
            "tg": snap(ref + sign * tg_d, tick) if tg_d else None,
            "trd": g(tr_lv), "tra": g(tr_lv, "activate") or 0.0,
            "be": g(spec["breakeven"]), "sl_dist": sl_d,
            "so": [(snap(ref + sign * dd, tick), pct) for dd, pct in sorted(so)]}


def scale_plan(levels: list[tuple[float, float]], sets: int) -> list[list[float]]:
    """Scale-out levels as [price, sets to close], nearest first. Each step is
    a % of the ENTRY size rounded down to whole sets (shares / lots): a step
    worth less than one set is skipped — half a lot can't be sold."""
    return [[px, n] for px, pct in levels if (n := int(sets * pct / 100 + 1e-9)) > 0]


def atr_periods(spec: dict) -> set[int]:
    return {lv["atr_period"] for lv in (spec["stop_loss"], spec["target"], spec["trailing"], spec["breakeven"],
                                        *(spec.get("scale_out") or [])) if lv and lv["type"] == "atr"}


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


# ---- option positions: premium, Greeks, rules (shared with the live runner) ---

def premium_base(legs: list[dict]) -> float:
    """|net option premium| at entry (credit or debit, rupees): what the MTM %
    stop / target and the premium P&L % are measured against."""
    return abs(sum(lg["act"] * lg["entry"] * lg["qty"] for lg in legs
                   if lg["kind"] in ("CE", "PE") and not lg.get("gen")))     # the ORIGINAL legs (gen 0)


def mtm_limits(m: dict, legs: list[dict]) -> tuple[Optional[float], Optional[float]]:
    """Rupee MTM stop and target of a position: the tighter of the ₹ value and
    the % of entry premium (each None when neither is set)."""
    base = premium_base(legs)
    stop = [x for x in (m.get("stop"), base * m["stop_pct"] / 100 if m.get("stop_pct") and base else None) if x]
    tgt = [x for x in (m.get("target"), base * m["target_pct"] / 100 if m.get("target_pct") and base else None) if x]
    return (min(stop) if stop else None), (min(tgt) if tgt else None)


def position_metrics(legs: list[dict], u: float, t: float, lot: int, px: Callable[[dict], Optional[float]],
                     iv: Optional[float] = None) -> dict[str, Optional[float]]:
    """POSITION_METRICS of a position, per set. `px(leg)` is a leg's current
    price (a closed leg's own exit); `iv` the IV every leg is priced at
    (backtests) or None = each leg's IV implied from its price (live).

    Closed legs (with an "exit") count only towards pnl_pct. Every value is
    None when a price is missing — a rule never fires on a guess."""
    sets = max(1e-9, legs[0]["qty"] / legs[0]["per_set"]) if legs and legs[0].get("per_set") else 1.0
    lot = lot or 1
    out = {k: 0.0 for k in ("delta", "gamma", "theta", "vega", "credit")}
    pnl, ivs, dtes = 0.0, [], []
    for lg in legs:
        p = lg["exit"] if lg.get("exit") is not None else px(lg)
        if p is None:
            return {k: None for k in POSITION_METRICS}
        pnl += lg["act"] * (p - lg["entry"]) * lg["qty"]
        if lg.get("exit") is not None:
            continue
        if lg["kind"] in ("CE", "PE"):
            yrs = fno.years_to(lg["exp"], t)
            v = fno.leg_iv(lg, u, iv) if iv is not None else fno.implied_vol(p, u, lg["K"], yrs, lg["kind"])
            g = fno.bs_greeks(u, lg["K"], yrs, v, lg["kind"])
            ivs.append(v)
            dtes.append((lg["exp"] - t) / 86400)
            out["credit"] -= lg["act"] * p * lg["qty"]
        else:
            g = {"delta": 1.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
        w = lg["act"] * lg["qty"]
        out["delta"] += w * g["delta"] / lot
        out["gamma"] += w * g["gamma"] * u / 100 / lot
        out["theta"] += w * g["theta"]
        out["vega"] += w * g["vega"]
    out = {k: v / sets for k, v in out.items()}
    base = premium_base(legs)
    out.update(abs_delta=abs(out["delta"]), pnl_pct=pnl / base * 100 if base else None,
               dte=min(dtes) if dtes else None, iv=sum(ivs) / len(ivs) * 100 if ivs else None)
    return out


def _rule_true(r: dict, m: dict) -> bool:
    v = m.get(r["metric"])
    if v is None:
        return False
    return {">": v > r["value"], "<": v < r["value"], ">=": v >= r["value"], "<=": v <= r["value"]}[r["op"]]


def rule_label(r: dict) -> str:
    return f"{r['metric']} {r['op']} {r['value']:.10g}"


def entry_rules_fail(spec: dict, m: dict) -> Optional[dict]:
    """The first entry rule this position does NOT meet, or None (all met / none set)."""
    return next((r for r in (spec.get("position_rules") or {}).get("entry", []) if not _rule_true(r, m)), None)


def exit_rule_hit(spec: dict, m: dict) -> Optional[dict]:
    """The first exit rule this position meets, or None."""
    return next((r for r in (spec.get("position_rules") or {}).get("exit", []) if _rule_true(r, m)), None)


def leg_levels(legs: list[dict], tick: Optional[float] = TICK) -> None:
    """A leg's own stop / target as premium prices on the grid, from its entry (in place)."""
    for lg in legs:
        if lg.get("sl_pct"):
            lg["sl"] = snap(lg["entry"] * (1 - lg["act"] * lg["sl_pct"] / 100), tick)
        if lg.get("tg_pct"):
            lg["tg"] = snap(lg["entry"] * (1 + lg["act"] * lg["tg_pct"] / 100), tick)


def leg_hit(lg: dict, p: float) -> Optional[str]:
    """SL / COST / TARGET when an open leg's premium `p` has reached its level."""
    if lg.get("sl") is not None and -lg["act"] * (p - lg["sl"]) >= 0:
        return "COST" if lg.get("at_cost") else "SL"
    if lg.get("tg") is not None and lg["act"] * (p - lg["tg"]) >= 0:
        return "TARGET"
    return None


def legs_to_cost(legs: list[dict]) -> None:
    """After a leg's stop: every other open option leg's stop moves to its entry price."""
    for lg in legs:
        if lg.get("exit") is None and lg["kind"] in ("CE", "PE"):
            lg["sl"], lg["at_cost"] = lg["entry"], True


# ---- adjustments on a candle's close: leg re-entry, roll, delta hedge (shared with the live runner) ----
#
# Order on a close: an exit (signal / rule) ends it; else re-entries, then rolls, then the hedge on the
# legs as they stand after the rolls. Every leg carries "src" (index of the configured leg it came from)
# and "gen" (0 = original, 1 = first replacement …); replacements carry "entry_reason" (LEG_REENTRY /
# LEG_ROLLED / HEDGE), hedge legs "hedge": True. The counters are read back from the legs themselves.

ADJUST_REASONS = {"reentry": "LEG_REENTRY", "roll": "LEG_ROLLED", "hedge": "HEDGE"}


def _adjust_cfgs(s: dict) -> list[dict]:
    inst, d = s["instrument"], s["direction"]
    return [lg for key, used in (("legs_long", d != "short"), ("legs_short", d != "long"))
            if used for lg in inst.get(key) or [] if lg.get("lots")]


def _check_adjustments(s: dict) -> None:
    """Validator: re-entries / rolls / a hedge need an option instrument, can't scale out, and need levels
    that don't ride on one leg's premium (a rolled leg would freeze them)."""
    cfgs = _adjust_cfgs(s)
    re_ = [lg for lg in cfgs if lg.get("reentries")]
    if not (re_ or any(lg.get("roll") for lg in cfgs) or (s.get("position_rules") or {}).get("hedge")):
        return
    inst = s["instrument"]
    if inst["type"] != "option":
        raise ValueError("leg re-entries, rolls and the delta hedge need an option instrument")
    if s.get("scale_out"):
        raise ValueError("leg re-entries, rolls and the delta hedge can't be combined with scale-out")
    if inst["levels_on"] == "instrument" and any(s[k] for k in ("stop_loss", "target", "trailing", "breakeven")):
        raise ValueError("adjusted positions need levels_on 'underlying' (or no stop / target / trail / breakeven) — "
                         "a rolled leg would freeze levels on its premium")
    if re_ and inst.get("on_leg_stop") == "all":
        raise ValueError("re-entries can't be used with on_leg_stop 'all' — every leg exits on the first stop")
    if inst.get("on_leg_stop") != "cost" and any(not lg.get("sl_pct") for lg in re_):
        raise ValueError("a leg with re-entries needs its own stop (sl_pct) — it re-enters after that stop")


def adjusts(spec: dict) -> bool:
    """Does this (normalized) spec adjust open positions on a candle's close?"""
    return spec["instrument"]["type"] == "option" and (
        any(lg.get("reentries") or lg.get("roll") for lg in _adjust_cfgs(spec))
        or bool((spec.get("position_rules") or {}).get("hedge")))


def adjust_window(spec: dict, close_min: int) -> tuple[bool, bool]:
    """(re-entries / rolls allowed, hedge allowed) for a candle closing at `close_min` (IST minutes):
    re-entries and rolls stop with the entry window (session.end), everything once square-off starts."""
    _, end, sq = session_window(spec)
    return close_min <= end and close_min < sq, close_min < sq


def leg_cfgs(spec: dict, side: str) -> list[dict]:
    """The configured legs of a position on `side` — what a leg's "src" indexes."""
    return spec["instrument"]["legs_long" if side == "BUY" else "legs_short"]


def one_leg_spec(spec: dict, cfg: dict) -> dict:
    """`spec` with `cfg` as its only leg on both sides: resolve ONE leg like an entry would."""
    return {**spec, "instrument": {**spec["instrument"], "legs_long": [cfg], "legs_short": [cfg]}}


def roll_cfg(cfg: dict) -> dict:
    """The leg a roll opens: the same leg at the strike nearest |delta| = roll.to."""
    return {**cfg, "strike": "DELTA", "delta": cfg["roll"]["to"]}


def hedge_cfg(lots: int) -> dict:
    return {"right": "FUT", "action": "BUY" if lots > 0 else "SELL", "lots": abs(lots), "strike": "ATM", "steps": 0}


def _latest(legs: list[dict]) -> dict[int, dict]:
    """The newest leg of each configured leg (highest gen), hedges aside."""
    out: dict[int, dict] = {}
    for lg in legs:
        k = lg.get("src")
        if k is not None and not lg.get("hedge") and (k not in out or lg.get("gen", 0) >= out[k].get("gen", 0)):
            out[k] = lg
    return out


def next_gen(legs: list[dict], src: Optional[int]) -> int:
    """gen of a new leg replacing configured leg `src` (1 = first replacement); a hedge (src None) is 0."""
    if src is None:
        return 0
    return max([lg.get("gen", 0) for lg in legs if lg.get("src") == src], default=0) + 1


def adj_count(legs: list[dict], src: Optional[int], reason: str) -> int:
    return sum(1 for lg in legs if lg.get("entry_reason") == reason and (src is None or lg.get("src") == src))


def reentries_due(spec: dict, side: str, legs: list[dict]) -> list[tuple[int, dict]]:
    """(src, cfg) of every leg to re-open now: its newest leg exited on its OWN stop (LEG_SL / LEG_COST,
    never a target or a roll) and it has re-entries left. BUY legs first."""
    cfgs = leg_cfgs(spec, side)
    due = [(k, cfgs[k]) for k, lg in sorted(_latest(legs).items()) if k < len(cfgs) and cfgs[k].get("reentries")
           and lg.get("exit") is not None and lg.get("exit_reason") in ("LEG_SL", "LEG_COST")
           and adj_count(legs, k, ADJUST_REASONS["reentry"]) < cfgs[k]["reentries"]]
    return sorted(due, key=lambda x: x[1]["action"] != "BUY")


def rolls_due(spec: dict, side: str, legs: list[dict],
              abs_delta: Callable[[dict], Optional[float]]) -> list[tuple[dict, dict]]:
    """(open leg, cfg) of every leg whose |delta| per unit (abs_delta(leg); None = unknown, never rolled)
    is above its roll.above, with rolls left. BUY legs first."""
    cfgs = leg_cfgs(spec, side)
    due = []
    for k, lg in sorted(_latest(legs).items()):
        cfg = cfgs[k] if k < len(cfgs) else {}
        if not cfg.get("roll") or lg.get("exit") is not None or lg["kind"] not in ("CE", "PE") \
                or adj_count(legs, k, ADJUST_REASONS["roll"]) >= cfg["roll"]["max"]:
            continue
        dl = abs_delta(lg)
        if dl is not None and dl > cfg["roll"]["above"]:
            due.append((lg, cfg))
    return sorted(due, key=lambda x: x[1]["action"] != "BUY")


def hedge_lots(spec: dict, legs: list[dict], delta_per_set: Optional[float], sets: float) -> int:
    """Signed FUT lots to add now (0 = none): -round(total delta in lots) once |delta per set| is above
    position_rules.hedge.above, while hedges are left. Rounded half away from zero (same live and backtest)."""
    hd = (spec.get("position_rules") or {}).get("hedge")
    if not hd or delta_per_set is None or abs(delta_per_set) <= hd["above"] \
            or adj_count(legs, None, ADJUST_REASONS["hedge"]) >= hd["max"]:
        return 0
    total = delta_per_set * sets
    return -int(math.copysign(math.floor(abs(total) + 0.5), total))


def leg_abs_delta(lg: dict, u: float, t: float, iv: Optional[float], px: Optional[float] = None) -> Optional[float]:
    """|delta| per unit of an open option leg: at the backtest IV (`iv`), or implied from its price `px` (live)."""
    yrs = fno.years_to(lg["exp"], t)
    if iv is None:
        if not px:
            return None
        v = fno.implied_vol(px, u, lg["K"], yrs, lg["kind"])
    else:
        v = fno.leg_iv(lg, u, iv)
    return abs(fno.bs_greeks(u, lg["K"], yrs, v, lg["kind"])["delta"])


def _brokerage(value: float, segment: str) -> float:
    if not value:
        return 0.0
    return 20.0 if segment == "opt" else min(20.0, 0.0003 * value)


def leg_pnl(act: int, qty: int, entry: float, exit_: float, kind: str, with_charges: bool,
            qty0: Optional[int] = None) -> tuple[float, float]:
    """`qty0` = the leg's size at entry when this is one part of a scaled-out
    position: the entry was ONE order, so each part carries its pro-rata share
    of that order's brokerage instead of a whole order's."""
    gross = (exit_ - entry) * qty * act
    buy, sell = (entry, exit_) if act > 0 else (exit_, entry)
    seg = SEGMENT.get(kind, "eq")
    ch = charges(buy * qty, sell * qty, seg) if with_charges else 0.0
    if with_charges and qty0 and qty0 != qty:
        ch = round(ch + 1.18 * (_brokerage(entry * qty0, seg) * qty / qty0 - _brokerage(entry * qty, seg)), 2)
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
                    qty_key: str = "per_set", model: Optional[dict] = None, t: Optional[float] = None,
                    iv: Optional[float] = None, name: str = "") -> float:
    """Capital a position blocks.

    model {"model": "span"} (the spec's "margin"): F&O legs block the
    exchange-style SPAN + exposure estimate (fno.span_margin — bought wings
    cut it, premium paid on top), cash legs notional / leverage. `iv` None =
    implied from each leg's price (live).

    Otherwise (leverage model): written calls and written puts on the same
    underlying offset (only one side can lose at expiry, as SPAN treats a short
    straddle/strangle), so the writing margin is the larger side's; premium for
    bought options and notional/leverage for futures and cash are added on top.
    ponytail: bought wings don't reduce the writing margin there — the span
    model is the one that credits a hedge."""
    if (model or {}).get("model") == "span" and any(lg["kind"] != "EQ" for lg in legs):
        fo = [lg for lg in legs if lg["kind"] != "EQ"]
        m = fno.span_margin(fo, underlying, t if t is not None else 0.0, name=name, iv=iv, price_key=price_key,
                            qty_key=qty_key, scan_pct=model.get("scan_pct"), exposure_pct=model.get("exposure_pct"))
        return m["total"] + sum(leg_margin("EQ", lg["act"], lg[price_key], underlying, lg[qty_key], leverage)
                                for lg in legs if lg["kind"] == "EQ")
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
        n = int(v)
    elif mode in ("amount", "pct_equity"):
        budget = v if mode == "amount" else equity * v / 100
        n = int(budget // per_set_margin) if per_set_margin > 0 else 0
    else:
        risk = v if mode == "risk" else equity * v / 100
        n = int(risk // per_set_risk) if per_set_risk else 0
    cap = spec["portfolio"].get("max_position_pct")
    if cap and per_set_margin > 0:                   # no single position may block more than cap% of equity
        n = min(n, int(equity * cap / 100 // per_set_margin))
    return n


# ---- per-symbol context ----------------------------------------------------

def prepare(d: dict, spec: dict) -> None:
    """Attach the option-pricing context to one symbol's bars (in place):
    `iv` per bar (decimal) and `expiry_ts` per bar (for DTE)."""
    meta = d.setdefault("meta", {})
    meta.setdefault("name", fno.fno_name(meta.get("symbol", "")))
    meta.setdefault("exch", fno.exchange_of(meta.get("symbol", "")))
    inst = spec["instrument"]
    kind = inst["expiry_kind"] if inst["type"] == "option" else "monthly"
    # Days the underlying actually traded: an expiry that falls on a day with
    # no candles was an exchange holiday and really settled the day before.
    d["trading_days"] = frozenset((t + IST) // 86400 for t in d["t"])
    d["expiry_ts"] = [fno.expiry_after(meta["name"], meta["exch"], kind, inst["expiry"], t, d["trading_days"])
                      for t in d["t"]]
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


def sim_legs(spec: dict, side: str, meta: dict, u: float, t: float, iv: float,
             tdays: Optional[frozenset] = None) -> list[dict[str, Any]]:
    """The legs a backtest opens for one set on a `side` signal: the symbol,
    its future, or the option legs with their strikes and expiries resolved
    (Black-Scholes at `iv` for PREMIUM / DELTA strikes). No prices or qty yet."""
    inst = spec["instrument"]
    sym = meta.get("symbol", "")
    sign = 1 if side == "BUY" else -1
    lot = int(meta.get("lot") or 1) if inst["type"] != "equity" else 1
    if inst["type"] == "equity":
        return [{"kind": "EQ", "act": sign, "label": sym, "per_set": 1}]
    if inst["type"] == "future":
        exp = fno.expiry_after(meta["name"], meta["exch"], "monthly", inst["expiry"], t, tdays)
        return [{"kind": "FUT", "act": sign, "exp": exp, "per_set": lot,
                 "label": f"{meta['name']} FUT {fno.label_date(exp)}"}]
    legs: list[dict[str, Any]] = []
    step = float(meta.get("step") or fno.default_step(meta["name"], u))
    for src, cfg in enumerate(inst["legs_long" if side == "BUY" else "legs_short"]):
        if not cfg["lots"]:
            continue
        if cfg["right"] == "FUT":          # priced on spot, like a future-type strategy
            exp = fno.expiry_after(meta["name"], meta["exch"], "monthly", inst["expiry"], t, tdays)
            legs.append({"kind": "FUT", "act": 1 if cfg["action"] == "BUY" else -1, "exp": exp,
                         "per_set": lot * cfg["lots"], "label": f"{meta['name']} FUT {fno.label_date(exp)}",
                         "src": src, "gen": 0})
            continue
        exp = fno.expiry_after(meta["name"], meta["exch"], inst["expiry_kind"], cfg.get("expiry") or inst["expiry"],
                               t, tdays)
        tyrs = fno.years_to(exp, t)
        sk = (inst["iv"].get("put_skew", 0.0), inst["iv"].get("call_skew", 0.0))
        kiv = lambda k: fno.skewed_iv(cfg["right"], k, u, iv, *sk)  # noqa: E731 — each strike at its own skewed IV
        if cfg["strike"] == "PREMIUM":
            k_ = fno.strike_for_premium(u, step, cfg["right"], cfg["premium"],
                                        lambda k: fno.bs_price(u, k, tyrs, kiv(k), cfg["right"]))
        elif cfg["strike"] == "DELTA":
            k_ = fno.strike_for_delta(u, step, cfg["right"], cfg["delta"],
                                      lambda k: fno.bs_greeks(u, k, tyrs, kiv(k), cfg["right"])["delta"])
        else:
            k_ = fno.pick_strike(u, step, cfg["right"], cfg["strike"], cfg["steps"])
        legs.append({"kind": cfg["right"], "act": 1 if cfg["action"] == "BUY" else -1, "K": k_, "exp": exp,
                     "per_set": lot * cfg["lots"], "src": src, "gen": 0,
                     "label": f"{meta['name']} {fno.label_date(exp)} {k_:g} {cfg['right']}",
                     **{k: cfg[k] for k in ("sl_pct", "tg_pct") if cfg.get(k)},
                     **({"skew": sk} if any(sk) else {})})
    return legs


def _leg_px(leg: dict, u: float, t: float, iv: float) -> float:
    if leg.get("exit") is not None:          # closed early by its own stop / target: its fill is final
        return leg["exit"]
    if leg["kind"] in ("EQ", "FUT"):
        return u
    return fno.bs_price(u, leg["K"], fno.years_to(leg["exp"], t), fno.leg_iv(leg, u, iv), leg["kind"])


def premium_atr(atrs: dict[int, Optional[float]], leg: dict, u: float, t: float,
                iv: Optional[float] = None) -> dict[int, Optional[float]]:
    """ATR for levels on an OPTION premium: the underlying's ATR × |delta|
    (how far the premium moves per point of the underlying). `iv` None =
    implied from the leg's live price (the runner); a future / stock leg is 1:1."""
    if leg.get("kind") not in ("CE", "PE"):
        return atrs
    yrs = fno.years_to(leg["exp"], t)
    if iv is None:
        px = float(leg.get("price") or leg.get("entry") or 0)
        iv = _solve(lambda s: fno.bs_price(u, leg["K"], yrs, s, leg["kind"]), px, 0.01, 5.0) if px > 0 else 0.0
    else:
        iv = fno.leg_iv(leg, u, iv)
    h = max(u * 0.001, 0.01)
    dlt = abs(fno.bs_price(u + h, leg["K"], yrs, iv, leg["kind"]) - fno.bs_price(u - h, leg["K"], yrs, iv, leg["kind"])) / (2 * h)
    return {p: (v * dlt if v else v) for p, v in atrs.items()}


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
        trade_from: Optional[int] = None, oos_from: Optional[int] = None) -> dict[str, Any]:
    """Backtest a normalized spec over {symbol: bars}; one shared portfolio.

    Each symbol's bars may carry `htf` ({tf: bars}) for multi-timeframe
    conditions, `vix` bars, and `meta` ({symbol, name, exch, lot, step}).
    Entries are taken only on bars at/after `trade_from` (earlier bars warm
    the indicators up). With `oos_from`, stats are also split into in-sample
    (entries before it) and out-of-sample (entries from it).
    """
    caches = caches if caches is not None else {}
    inst = spec["instrument"]
    pf = spec["portfolio"]
    slip = spec["costs"]["slippage_pct"] / 100
    with_ch = spec["costs"]["charges"]
    start, end, sq = session_window(spec)
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

    tick_of = {sym: tick_for(data[sym]["meta"].get("symbol") or sym, data[sym]["meta"]) for sym in syms}
    events = sorted(((t, k, i) for k, sym in enumerate(syms) for i, t in enumerate(data[sym]["t"])))
    trades: list[dict[str, Any]] = []
    skipped = {"no_capital": 0, "max_positions": 0, "daily_halt": 0, "entry_expired": 0, "entry_rules": 0}
    exit_rules = (spec.get("position_rules") or {}).get("exit")
    adjusting = adjusts(spec)
    on_leg_stop = inst.get("on_leg_stop", "leg")
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

    def close(sym: str, u: float, t: float, reason: str, i: int, slipped: bool = True,
              sets: Optional[int] = None) -> None:
        """Close the position — or, with `sets` below its size, that many sets
        of it (a scale-out: booked as its own trade, the rest stays open)."""
        nonlocal realized, used_margin, open_n, day_realized
        s_ = st[sym]
        pos, d = s_["pos"], s_["d"]
        n = pos["sets"] if sets is None else min(sets, pos["sets"])
        part = n < pos["sets"]
        iv = d.get("iv", [0.0] * len(d["t"]))[i] if inst["type"] == "option" else 0.0
        gross = chg = 0.0
        legs_out = []
        for l in pos["legs"]:
            q = l["qty"] * n // pos["sets"]          # qty = per_set x sets, so this is exact
            px = _leg_px(l, u, t, iv)
            if slipped and l.get("exit") is None:      # closing a long sells (down), a short buys (up)
                px = snap(px * (1 - slip), tick_of[sym], -1) if l["act"] > 0 else snap(px * (1 + slip), tick_of[sym], 1)
            else:
                px = snap(px, tick_of[sym])
            g, ch = leg_pnl(l["act"], q, l["entry"], px, l["kind"], with_ch, l["qty0"])
            gross, chg = gross + g, chg + ch
            legs_out.append({"label": l["label"], "side": "BUY" if l["act"] > 0 else "SELL", "qty": q,
                             "entry": round(l["entry"], 2), "exit": round(px, 2),
                             **({"reason": l["exit_reason"]} if l.get("exit_reason") else {}),
                             **({"entry_reason": l["entry_reason"]} if l.get("entry_reason") else {})})
            if part:
                l["qty"] -= q
        net = gross - chg
        margin = pos["margin"] * n / pos["sets"]
        risk = pos["risk"] * n / pos["sets"] if pos["risk"] else None
        single = len(pos["legs"]) == 1
        trades.append({
            "symbol": sym, "instrument": " + ".join(l["label"] for l in legs_out), "side": pos["side"],
            "qty": legs_out[0]["qty"], "lots": n,
            "entry_t": pos["t"], "exit_t": t,
            "entry": legs_out[0]["entry"] if single else round(pos["u0"], 2),
            "exit": legs_out[0]["exit"] if single else round(u, 2),
            "u_entry": round(pos["u0"], 2), "u_exit": round(u, 2),
            "reason": reason, "gross": round(gross, 2), "charges": round(chg, 2), "net": round(net, 2),
            "bars": i - pos["i"], "margin": round(margin, 2), "legs": legs_out,
            "stop": pos["sl"], "target": pos["tg"],
            # excursions on the reference price, in % of entry: how far it went
            # against / for you before the exit — the data to tune stops with.
            "mae_pct": round(pos["sign"] * (pos["worst"] - pos["ref_entry"]) / pos["ref_entry"] * 100, 3)
            if pos["ref_entry"] else None,
            "mfe_pct": round(pos["sign"] * (pos["best"] - pos["ref_entry"]) / pos["ref_entry"] * 100, 3)
            if pos["ref_entry"] else None,
            "r": round(net / risk, 2) if risk else None,
            "minutes": round((t - pos["t"]) / 60),
            **({"greeks": pos["greeks0"]} if pos.get("greeks0") else {}),
        })
        realized += net
        day_realized += net
        used_margin -= margin
        if part:
            if pos["peak"] is not None:            # the MTM trail follows what is still open
                pos["peak"] *= (pos["sets"] - n) / pos["sets"]
            pos["sets"] -= n
            pos["margin"] -= margin
            if risk:
                pos["risk"] -= risk
            return
        open_n -= 1
        open_syms.discard(sym)
        s_["pos"] = None
        s_["cool"] = i + spec["cooldown_bars"]

    def open_(sym: str, side: str, i: int, sig_i: int, u_fill: Optional[float] = None) -> Optional[str]:
        """Fill at bar i's open (or at `u_fill` for a triggered entry). Returns a skip reason or None."""
        nonlocal used_margin, open_n
        s_ = st[sym]
        d = s_["d"]
        meta = d["meta"]
        u, t = (d["o"][i] if u_fill is None else u_fill), d["t"][i]
        tdays = d.get("trading_days")
        iv = d["iv"][i] if inst["type"] == "option" else 0.0
        sign = 1 if side == "BUY" else -1
        legs = sim_legs(spec, side, meta, u, t, iv, tdays)
        tk = tick_of[sym]
        for l in legs:
            raw = _leg_px(l, u, t, iv)
            l["entry"] = snap(raw * (1 + slip), tk, 1) if l["act"] > 0 else snap(raw * (1 - slip), tk, -1)
        greeks0 = None
        if inst["type"] == "option":
            one = [{**l, "qty": l["per_set"]} for l in legs]
            m0 = position_metrics(one, u, t, int(meta.get("lot") or 1), lambda l: l["entry"], iv)
            if entry_rules_fail(spec, m0):
                return "entry_rules"
            greeks0 = {k: round(m0[k], 4 if k in ("delta", "gamma") else 2)
                       for k in ("delta", "gamma", "theta", "vega", "credit", "iv", "dte") if m0[k] is not None}
            leg_levels(legs, tk)
        ref = "u" if inst["type"] == "equity" or inst["levels_on"] == "underlying" else 0
        rsign = sign if ref == "u" else legs[0]["act"]
        ref_entry = u if ref == "u" else legs[0]["entry"]
        atrs = {p: a[sig_i] for p, a in s_["atr"].items()}
        if ref != "u":                                   # levels on the option premium: its own ATR
            atrs = premium_atr(atrs, legs[0], u, t, iv)
        lv = entry_levels(spec, rsign, ref_entry, atrs, tk)
        per_set_margin = position_margin(legs, u, pf["leverage"], model=spec.get("margin"), t=t,
                                         iv=iv if inst["type"] == "option" else None, name=meta["name"])
        per_set_risk = lv["sl_dist"] * legs[0]["per_set"] if lv["sl_dist"] else None
        eq = equity_now()
        # Losses always come out of the account, compounding or not: once the
        # cash is gone there is nothing left to trade with (compounding only
        # decides whether sizing follows the equity curve).
        cash = min(eq, pf["capital"] + realized)
        if cash <= 0:
            return "no_capital"
        sets = units(spec, eq, per_set_margin, per_set_risk)
        if per_set_margin > 0:
            sets = min(sets, int(max(0.0, cash - used_margin) // per_set_margin))
        if sets < 1:
            return "no_capital"
        for l in legs:
            l["qty"] = l["qty0"] = l["per_set"] * sets
        margin = per_set_margin * sets
        s_["pos"] = {"side": side, "legs": legs, "ref": ref, "sign": rsign, "ref_entry": ref_entry,
                     "sl": lv["sl"], "tg": lv["tg"], "trd": lv["trd"], "tra": lv["tra"], "be": lv["be"],
                     "be_on": False, "trail": None, "best": ref_entry, "worst": ref_entry, "peak": None,
                     # rupees at risk to the initial stop — the "1R" of R-multiples
                     "risk": lv["sl_dist"] * legs[0]["per_set"] * sets
                     if lv["sl_dist"] and (ref != "u" or inst["type"] != "option") else None,
                     "t": t, "i": i, "day": (t + IST) // 86400, "u0": u, "sets": sets, "margin": margin,
                     "exit_pending": None, "mid_bar": u_fill is not None,
                     "so": scale_plan(lv["so"], sets), "so_k": 0, "greeks0": greeks0,
                     "leg_rules": any(l.get("sl") is not None or l.get("tg") is not None for l in legs)}
        used_margin += margin
        open_n += 1
        open_syms.add(sym)
        s_["per_day"][(t + IST) // 86400] = s_["per_day"].get((t + IST) // 86400, 0) + 1
        return None

    def leg_events(sym: str, i: int) -> bool:
        """Each leg's own stop / target inside bar i. The bar is walked open ->
        low -> high -> close (open -> high -> low -> close on a red candle); on
        each stretch the underlying moves one way, so every leg's premium does
        too, and the first level crossed fills first. A leg stop can move the
        other legs' stops to cost or exit them all. True when nothing is left."""
        s_ = st[sym]
        pos, d = s_["pos"], s_["d"]
        o, h, l, c, t = d["o"][i], d["h"][i], d["l"][i], d["c"][i], d["t"][i]
        iv = d["iv"][i]
        later = i > pos["i"]
        fav_ok = later or not pos.get("mid_bar")
        path = [o if later else pos["u0"]] + ([l, h, c] if c >= o else [h, l, c])
        for a, b in zip(path, path[1:]):
            while True:
                hit = None
                for lg in pos["legs"]:
                    if lg.get("exit") is not None or lg["kind"] not in ("CE", "PE"):
                        continue
                    f = lambda u, lg=lg: _leg_px(lg, u, t, iv)  # noqa: E731
                    for why, lvl, way in (("SL", lg.get("sl"), -lg["act"]), ("TARGET", lg.get("tg"), lg["act"])):
                        if lvl is None or (why == "TARGET" and not fav_ok):
                            continue
                        if way * (f(a) - lvl) >= 0:
                            u_x = a
                        elif way * (f(b) - lvl) >= 0:
                            u_x = _solve(f, lvl, a, b)
                        else:
                            continue
                        if hit is None or abs(u_x - a) < abs(hit[0] - a):
                            hit = (u_x, lg, why)
                if hit is None:
                    break
                u_x, lg, why = hit
                px = _leg_px(lg, u_x, t, iv)
                if why == "SL":                      # a stop fills against you, like the position's
                    px = snap(px * (1 - slip), tick_of[sym], -1) if lg["act"] > 0 else snap(px * (1 + slip), tick_of[sym], 1)
                else:
                    px = snap(px, tick_of[sym])
                reason = "LEG_TARGET" if why == "TARGET" else "LEG_COST" if lg.get("at_cost") else "LEG_SL"
                lg["exit"], lg["exit_reason"] = px, reason
                if why == "SL" and on_leg_stop == "all":
                    close(sym, u_x, t, reason, i)
                    return True
                if why == "SL" and on_leg_stop == "cost":
                    legs_to_cost(pos["legs"])
                if all(x.get("exit") is not None for x in pos["legs"]):
                    close(sym, u_x, t, reason, i)
                    return True
                a = u_x
        return False

    def adjust(sym: str, i: int) -> None:
        """Re-entries, then rolls, then the delta hedge, on bar i's close (see reentries_due & co).
        Fills at the close's prices with slippage against you; every added leg must fit the free
        capital once the position's margin is recomputed, else it is skipped and counted."""
        nonlocal used_margin
        s_ = st[sym]
        pos, d = s_["pos"], s_["d"]
        meta, tk = d["meta"], tick_of[sym]
        u, tc, iv = d["c"][i], ind.close_time(d, i), d["iv"][i]
        lot = int(meta.get("lot") or 1)
        legs_ok, hedge_ok = adjust_window(spec, ((tc + IST) % 86400) // 60)

        def fill(px: float, act: int) -> float:
            return snap(px * (1 + slip), tk, 1) if act > 0 else snap(px * (1 - slip), tk, -1)

        def add(cfg: dict, reason: str, src: Optional[int], sets: int, skip_key: str) -> bool:
            nonlocal used_margin
            lg = sim_legs(one_leg_spec(spec, cfg), pos["side"], meta, u, tc, iv, d.get("trading_days"))[0]
            lg["entry"] = fill(_leg_px(lg, u, tc, iv), lg["act"])
            lg["qty"] = lg["qty0"] = lg["per_set"] * sets
            lg.update(src=src, gen=next_gen(pos["legs"], src), entry_reason=reason,
                      **({"hedge": True} if reason == ADJUST_REASONS["hedge"] else {}))
            leg_levels([lg], tk)
            new_m = position_margin([x for x in pos["legs"] + [lg] if x.get("exit") is None], u, pf["leverage"],
                                    qty_key="qty", model=spec.get("margin"), t=tc, iv=iv, name=meta["name"])
            cash = min(equity_now(), pf["capital"] + realized)
            if new_m - pos["margin"] > cash - used_margin:
                skipped[skip_key] = skipped.get(skip_key, 0) + 1
                return False
            pos["legs"].append(lg)
            used_margin += new_m - pos["margin"]
            pos["margin"] = new_m
            pos["leg_rules"] = pos["leg_rules"] or lg.get("sl") is not None or lg.get("tg") is not None
            return True

        if legs_ok:
            for src, cfg in reentries_due(spec, pos["side"], pos["legs"]):
                add(cfg, ADJUST_REASONS["reentry"], src, pos["sets"], "reentry_no_capital")
            for lg, cfg in rolls_due(spec, pos["side"], pos["legs"], lambda x: leg_abs_delta(x, u, tc, iv)):
                p = _leg_px(lg, u, tc, iv)                 # out first: the tested leg closes before its replacement
                lg["exit"], lg["exit_reason"] = fill(p, -lg["act"]), "LEG_ROLL"
                add(roll_cfg(cfg), ADJUST_REASONS["roll"], lg["src"], pos["sets"], "roll_no_capital")
        if hedge_ok:
            m = position_metrics(pos["legs"], u, tc, lot, lambda x: _leg_px(x, u, tc, iv), iv)
            n = hedge_lots(spec, pos["legs"], m["delta"], pos["sets"])
            if n:
                add(hedge_cfg(n), ADJUST_REASONS["hedge"], None, 1, "hedge_no_capital")
        if all(x.get("exit") is not None for x in pos["legs"]):
            close(sym, u, tc, "LEG_ROLL", i)        # a roll whose replacement didn't fit left nothing open

    def check_intrabar(sym: str, i: int) -> None:
        s_ = st[sym]
        if s_["pos"]["leg_rules"] and leg_events(sym, i):
            return
        pos, d = s_["pos"], s_["d"]
        o, h, l, c, t = d["o"][i], d["h"][i], d["l"][i], d["c"][i], d["t"][i]
        iv = d["iv"][i] if inst["type"] == "option" else 0.0
        sgn = pos["sign"]
        later = i > pos["i"]
        # A mid-bar (triggered) fill can't claim the entry bar's favourable
        # extreme: OHLC doesn't say it came after the fill. Stops still count.
        fav_ok = later or not pos.get("mid_bar")
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
        while pos["so"] and fav_ok:                 # scale-out levels, nearest first, filled like a target
            lvl, n = pos["so"][0]
            if pos["tg"] is not None and sgn * (lvl - pos["tg"]) >= 0:
                break                               # at / past the target: the target takes it all (as live)
            u_fav = max((h, l), key=lambda u: sgn * f(u))
            if later and sgn * (f(o) - lvl) >= 0:
                u_x = o
            elif sgn * (f(u_fav) - lvl) >= 0:
                u_x = _solve(f, lvl, o, u_fav)
            else:
                break
            pos["so"].pop(0)
            pos["so_k"] += 1
            why = f"SCALE_OUT_{pos['so_k']}"
            if n >= pos["sets"]:
                return close(sym, u_x, t, why, i, slipped=False)
            close(sym, u_x, t, why, i, slipped=False, sets=n)
        if pos["tg"] is not None and fav_ok:
            ro = f(o)
            u_fav = max((h, l), key=lambda u: sgn * f(u))
            if later and sgn * (ro - pos["tg"]) >= 0:
                return close(sym, o, t, "TARGET", i, slipped=False)
            if sgn * (f(u_fav) - pos["tg"]) >= 0:
                return close(sym, _solve(f, pos["tg"], o, u_fav), t, "TARGET", i, slipped=False)
        m_stop, m_target = mtm_limits(mtm_rules, pos["legs"])
        if m_stop or m_target or mtm_rules["trail_start"]:
            m = lambda u: mtm(pos, u, t, iv)  # noqa: E731
            pts = sorted((o, h, l, c), key=m)
            lo_u, hi_u = pts[0], pts[-1]
            floor = -m_stop if m_stop else None
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
            if m_target and fav_ok and m(hi_u) >= m_target:
                u_x = o if m(o) >= m_target else _solve(m, m_target, o, hi_u)
                return close(sym, u_x, t, "MTM_TARGET", i, slipped=False)

    for t, k, i in events:
        sym = syms[k]
        s_ = st[sym]
        d = s_["d"]
        day, mod = (t + IST) // 86400, ((t + IST) % 86400) // 60
        if day != day_key:
            day_key, day_realized = day, 0.0
        if s_["pos"] and day != s_["pos"]["day"] and sq < CARRY:
            close(sym, d["c"][i - 1], ind.close_time(d, i - 1), "EOD", i - 1)
        if s_["pos"] and (s_["pos"]["exit_pending"] or mod >= sq):
            close(sym, d["o"][i], t, s_["pos"]["exit_pending"] or "SQUARE_OFF", i)
        if s_["pending"]:
            p = s_["pending"]
            fill: Optional[float] = None
            lo_, hi_, op_ = d["l"][i], d["h"][i], d["o"][i]
            if p["level"] is None:
                fill = op_
            elif (p["side"] == "BUY") == (p["kind"] == "pullback"):     # buy dip / sell breakdown
                if lo_ <= p["level"]:
                    fill = min(op_, p["level"])
            elif hi_ >= p["level"]:                                     # buy breakout / sell rally
                fill = max(op_, p["level"])
            alive = p["day"] == day and mod < sq and halted != day and s_["pos"] is None
            if fill is None and alive and i < p["expires"]:
                pass                                      # still waiting for the trigger
            else:
                s_["pending"] = None
                if halted == day:
                    skipped["daily_halt"] += 1
                elif fill is None or not alive:
                    if p["level"] is not None:
                        skipped["entry_expired"] += 1
                elif open_n >= pf["max_positions"]:
                    skipped["max_positions"] += 1
                else:
                    why = open_(sym, p["side"], i, p["i"], fill)
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
            worst_now = min((d["h"][i], d["l"][i]), key=lambda u: sgn * f(u))
            pos["best"] = max(pos["best"], f(best_now)) if sgn > 0 else min(pos["best"], f(best_now))
            pos["worst"] = min(pos["worst"], f(worst_now)) if sgn > 0 else max(pos["worst"], f(worst_now))
            gain = sgn * (pos["best"] - pos["ref_entry"])
            if pos["trd"] and gain >= pos["tra"]:
                nt = snap(pos["best"] - sgn * pos["trd"], tick_of[sym])
                pos["trail"] = nt if pos["trail"] is None else (max if sgn > 0 else min)(pos["trail"], nt)
            if pos["be"] and gain >= pos["be"]:
                pos["be_on"] = True
            m_c = mtm(pos, d["c"][i], t, iv)
            pos["peak"] = m_c if pos["peak"] is None else max(pos["peak"], m_c)
            if s_["sig"]["exit_long" if pos["side"] == "BUY" else "exit_short"][i]:
                pos["exit_pending"] = "SIGNAL"
            elif spec["max_bars"] and i - pos["i"] + 1 >= spec["max_bars"]:
                pos["exit_pending"] = "TIME_STOP"
            elif exit_rules:                 # Greeks / premium rules, on the candle's close like a signal
                tc = ind.close_time(d, i)
                pm = position_metrics(pos["legs"], d["c"][i], tc, int(d["meta"].get("lot") or 1),
                                      lambda l: _leg_px(l, d["c"][i], tc, iv), iv)
                r_ = exit_rule_hit(spec, pm)
                if r_:
                    pos["exit_pending"] = f"RULE_{r_['metric'].upper()}"
            if adjusting and not pos["exit_pending"] and halted != day:
                adjust(sym, i)                  # after the exits: re-entries, rolls, then the hedge
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
            close_mod = ((ind.close_time(d, i) + IST) % 86400) // 60
            if start <= close_mod <= end and s_["per_day"].get(day, 0) < spec["max_trades_per_day"] \
                    and day_allowed(spec, t):
                side = "BUY" if s_["sig"]["entry_long"][i] else "SELL" if s_["sig"]["entry_short"][i] else None
                if side:
                    s_["pending"] = {"side": side, "day": day, "i": i,
                                     "level": entry_trigger(spec, side, d["c"][i], tick_of[sym]),
                                     "kind": spec["entry_order"]["type"],
                                     "expires": i + 1 + spec["entry_order"]["valid_bars"]}
    for sym in syms:
        if st[sym]["pos"]:
            d = st[sym]["d"]
            close(sym, d["c"][-1], ind.close_time(d, len(d["t"]) - 1), "END", len(d["t"]) - 1)

    trades.sort(key=lambda x: x["entry_t"])
    per_symbol = {}
    live_bars = 0
    holds = []
    for sym in syms:
        d = data[sym]
        mine = [x for x in trades if x["symbol"] == sym]
        s2 = stats(mine, pf["capital"])
        first = next((j for j, t in enumerate(d["t"]) if trade_from is None or t >= trade_from), None)
        n_live = len(d["t"]) - first if first is not None else 0
        live_bars += n_live
        # Buy & hold over the same window — the benchmark the strategy must beat.
        hold = (d["c"][-1] / d["o"][first] - 1) * 100 if first is not None and d["o"][first] else None
        if hold is not None:
            holds.append(hold)
        per_symbol[sym] = {k: s2[k] for k in ("trades", "win_rate", "net_pnl", "profit_factor", "max_drawdown")} \
            | {"bars": n_live, "buy_hold_pct": round(hold, 2) if hold is not None else None}
    out = stats(trades, pf["capital"])
    out["skipped"] = skipped
    out["exposure_pct"] = round(sum(x["bars"] + 1 for x in positions(trades)) / live_bars * 100, 2) if live_bars else 0.0
    out["buy_hold_pct"] = round(sum(holds) / len(holds), 2) if holds else None
    if oos_from:
        out["in_sample"] = summary(stats([x for x in trades if x["entry_t"] < oos_from], pf["capital"]))
        out["out_of_sample"] = summary(stats([x for x in trades if x["entry_t"] >= oos_from], pf["capital"]))
        out["oos_from"] = oos_from
    return {"stats": out, "per_symbol": per_symbol, "by_reason": by_reason(trades), "trades": trades}


# ---- stats -----------------------------------------------------------------

def positions(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Scale-out parts merged back into the one position they came from (same
    symbol + entry time — a symbol holds one position at a time): per-trade
    metrics count positions, not exit rows. R is lot-weighted (each part's
    risk is its share of the position's), excursions are the position's."""
    by: dict[tuple, list[dict]] = {}
    for x in trades:
        by.setdefault((x.get("symbol"), x["entry_t"]), []).append(x)
    out = []
    for parts in by.values():
        if len(parts) == 1 or not any(str(x.get("reason", "")).startswith("SCALE_OUT") for x in parts):
            out.extend(parts)
            continue
        parts.sort(key=lambda x: x["exit_t"])
        lots = sum(x.get("lots") or 1 for x in parts)
        rs = [(x["r"], x.get("lots") or 1) for x in parts if x.get("r") is not None]
        m = {**parts[-1], **{k: round(sum(x[k] for x in parts), 2) for k in ("net", "gross", "charges")},
             "qty": sum(x.get("qty") or 0 for x in parts), "lots": lots,
             "r": round(sum(r * w for r, w in rs) / sum(w for _, w in rs), 2) if len(rs) == len(parts) else None,
             "reason": "+".join(x["reason"] for x in parts)}
        out.append(m)
    return sorted(out, key=lambda x: x["exit_t"])


def stats(trades: list[dict[str, Any]], capital: float) -> dict[str, Any]:
    rows = sorted(trades, key=lambda x: x["exit_t"])     # exit rows: P&L lands when each part closes
    trades = positions(rows)                             # positions: what per-trade metrics count
    n = len(trades)
    nets = [x["net"] for x in trades]
    wins = [v for v in nets if v > 0]
    losses = [v for v in nets if v <= 0]
    eq, peak, max_dd, max_dd_pct = capital, capital, 0.0, 0.0
    curve = []
    for x in rows:
        eq += x["net"]
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
        max_dd_pct = max(max_dd_pct, (peak - eq) / peak * 100 if peak > 0 else 0)
        curve.append([x["exit_t"], round(eq, 2)])
    daily: dict[int, float] = {}
    monthly: dict[str, float] = {}
    weekday: dict[int, list[float]] = {}
    for x in rows:
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
    mean_t = net_mean = sum(nets) / n if n else 0.0
    sd_t = math.sqrt(sum((v - mean_t) ** 2 for v in nets) / (n - 1)) if n > 1 else 0.0
    # Ulcer index: RMS of the percentage drawdown along the equity curve —
    # punishes deep AND long drawdowns, unlike max DD which sees one point.
    pk, sq_dd = capital, 0.0
    for _, e in curve:
        pk = max(pk, e)
        sq_dd += ((pk - e) / pk * 100) ** 2 if pk > 0 else 0.0
    ulcer = math.sqrt(sq_dd / len(curve)) if curve else 0.0
    rs = [x["r"] for x in trades if x.get("r") is not None]
    maes = [x["mae_pct"] for x in trades if x.get("mae_pct") is not None]
    mfes = [x["mfe_pct"] for x in trades if x.get("mfe_pct") is not None]
    days_span = ((trades[-1]["exit_t"] - trades[0]["entry_t"]) / 86400) if n else 0
    final = capital + net
    from app.research import stats as rstats
    rets = [v / capital for v in dv] if capital else []          # daily returns on starting capital
    skew, kurt = rstats._skew_kurt(rets) if len(rets) > 2 else (None, None)   # noqa: SLF001
    worst = sorted(dv)
    k5 = max(1, len(worst) // 20)                                # the worst 5% of days (at least one)
    pct = lambda q: worst[min(len(worst) - 1, int(q * len(worst)))]   # noqa: E731
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
        # the typical trade, unmoved by one huge winner or loser
        "median_win": round(statistics.median(wins), 2) if wins else 0.0,
        "median_loss": round(statistics.median(losses), 2) if losses else 0.0,
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
        "avg_minutes_held": round(sum(x.get("minutes", 0) for x in trades) / n, 1) if n else 0.0,
        # t-stat of the mean trade: |t| > 2 is the usual bar for "not luck".
        "t_stat": round(net_mean / (sd_t / math.sqrt(n)), 2) if n > 1 and sd_t > 0 else None,
        "payoff_ratio": round((gross_win / len(wins)) / (gross_loss / len(losses)), 2)
        if wins and losses and gross_loss else None,
        # Kelly fraction W - (1-W)/R; negative = no edge, size nothing.
        "kelly_pct": round((len(wins) / n - (1 - len(wins) / n) / ((gross_win / len(wins)) / (gross_loss / len(losses))))
                           * 100, 2) if wins and losses and gross_loss else None,
        "recovery_factor": round(net / max_dd, 2) if max_dd > 0 else None,
        "ulcer_index": round(ulcer, 2),
        # Van Tharp's System Quality Number: sqrt(n) x mean / sd of trade P&L (n capped at 100). >2 good, >3 excellent.
        "sqn": round(math.sqrt(min(n, 100)) * mean_t / sd_t, 2) if n > 1 and sd_t > 0 else None,
        # P(the true Sharpe > 0) given this many days and their skew / fat tails (Bailey & Lopez de Prado).
        "psr_pct": round(rstats.probabilistic_sharpe(rets) * 100, 1)
        if len(rets) > 2 and rstats.probabilistic_sharpe(rets) is not None else None,
        "skew": round(skew, 2) if skew is not None else None,
        "excess_kurtosis": round(kurt - 3, 2) if kurt is not None else None,
        # historical 95% one-day Value at Risk and the average day beyond it (expected shortfall)
        "var95": round(-pct(0.05), 2) if len(dv) >= 20 else None,
        "cvar95": round(-sum(worst[:k5]) / k5, 2) if len(dv) >= 20 else None,
        # size of the best 5% of days vs the worst 5%: > 1 = the right tail is fatter
        "tail_ratio": round(abs(pct(0.95)) / abs(pct(0.05)), 2) if len(dv) >= 20 and pct(0.05) < 0 else None,
        "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
        "avg_mae_pct": round(sum(maes) / len(maes), 3) if maes else None,
        "avg_mfe_pct": round(sum(mfes) / len(mfes), 3) if mfes else None,
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


SUMMARY_KEYS = ("trades", "win_rate", "net_pnl", "profit_factor", "sharpe", "max_drawdown_pct", "expectancy",
                "return_pct", "t_stat")


def summary(st: dict[str, Any]) -> dict[str, Any]:
    return {k: st.get(k) for k in SUMMARY_KEYS}


def monte_carlo(trades: list[dict[str, Any]], capital: float, runs: int = 1000, seed: int = 7) -> Optional[dict[str, Any]]:
    """Resample the trade sequence with replacement `runs` times.

    One backtest is one path; the order of wins and losses is luck. The
    spread of final P&L and of max drawdown across resampled paths is what
    to size capital for — plan for the 95th-percentile drawdown, not the
    one this history happened to produce. Seeded, so it is reproducible.
    """
    import random

    nets = [x["net"] for x in positions(trades)]
    n = len(nets)
    if n < 10:
        return None
    rnd = random.Random(seed)
    finals, dds, ruined = [], [], 0
    for _ in range(runs):
        eq = peak = capital
        dd = 0.0
        broke = False
        for _ in range(n):
            eq += nets[rnd.randrange(n)]
            peak = max(peak, eq)
            dd = max(dd, (peak - eq) / peak * 100 if peak > 0 else 100.0)
            broke = broke or eq <= capital / 2
        finals.append(eq - capital)
        dds.append(dd)
        ruined += broke
    finals.sort()
    dds.sort()
    q = lambda xs, p: xs[min(len(xs) - 1, int(p * len(xs)))]  # noqa: E731
    return {"runs": runs, "trades": n,
            "net_p5": round(q(finals, 0.05), 2), "net_p50": round(q(finals, 0.5), 2), "net_p95": round(q(finals, 0.95), 2),
            "dd_pct_p50": round(q(dds, 0.5), 2), "dd_pct_p95": round(q(dds, 0.95), 2),
            "prob_loss_pct": round(sum(1 for f in finals if f < 0) / runs * 100, 1),
            "prob_half_capital_pct": round(ruined / runs * 100, 1)}


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
METRICS = ("net_pnl", "profit_factor", "sharpe", "sortino", "win_rate", "return_pct", "expectancy", "calmar", "t_stat")


def optimize(spec: dict, data: dict[str, dict], grid: list[dict[str, Any]], metric: str = "net_pnl",
             min_trades: int = 5, trade_from: Optional[int] = None, oos_from: Optional[int] = None) -> dict[str, Any]:
    """Grid-search over [{path, values}] and rank by `metric`.

    Ranks only combos with >= min_trades: a 1-trade 100% win rate wins every
    grid search and means nothing. With `oos_from` the ranking uses ONLY the
    in-sample trades and each row carries its out-of-sample result — the
    walk-forward check that a winner was found, not fitted.
    """
    axes, combos = _grid(grid, metric)
    caches: dict[str, dict] = {}
    rows = []
    daily_by_combo: list[dict[int, float]] = []
    capital = 0.0
    invalid: list[str] = []
    for combo in combos:
        s = _variant(spec, axes, combo, invalid)
        if s is None:
            continue
        capital = float(s["portfolio"]["capital"])
        res = run(s, data, caches, trade_from, oos_from)
        full = res["stats"]
        # Daily net P&L of the SELECTION sample (in-sample when there is an
        # OOS split): the series the overfitting statistics are computed on.
        day_net: dict[int, float] = {}
        for x in res["trades"]:
            if oos_from is None or x["entry_t"] < oos_from:
                k = (x["exit_t"] + IST) // 86400
                day_net[k] = day_net.get(k, 0.0) + x["net"]
        daily_by_combo.append(day_net)
        st = full["in_sample"] if oos_from else full
        rows.append({"params": {p: v for (p, _), v in zip(axes, combo)},
                     **{k: st.get(k) for k in ("trades", "win_rate", "net_pnl", "profit_factor", "sharpe", "sortino",
                                               "calmar", "max_drawdown", "max_drawdown_pct", "return_pct",
                                               "expectancy", "t_stat")},
                     "oos": full.get("out_of_sample")})
    for r, d in zip(rows, daily_by_combo):
        r["_daily"] = d
    if not rows:
        raise ValueError(f"no valid combination: {invalid[0]}")
    ok = [r for r in rows if r["trades"] >= min_trades]
    ok.sort(key=lambda r: (r[metric] is not None, r[metric] or 0), reverse=True)
    over = overfitting_stats([r["_daily"] for r in ok], capital, n_trials=len(combos))
    for r in rows:
        r.pop("_daily", None)
    return {"combos": len(combos), "ranked": ok[:100],
            "too_few_trades": len(rows) - len(ok), "metric": metric, "invalid": len(invalid),
            "overfitting": over,
            # every combo, for the parameter heatmap (a plateau vs a lone spike)
            "all": [{"params": r["params"], "value": r[metric], "trades": r["trades"]} for r in rows]}


def _grid(grid: list[dict[str, Any]], metric: str) -> tuple[list[tuple[str, list]], list[tuple]]:
    if metric not in METRICS:
        raise ValueError(f"metric must be one of {METRICS}")
    if not grid:
        raise ValueError("add at least one parameter range")
    axes = [(g["path"], list(g["values"])) for g in grid]
    combos = list(itertools.product(*(v for _, v in axes)))
    if not combos or len(combos) > MAX_COMBOS:
        raise ValueError(f"{len(combos)} combinations — keep it between 1 and {MAX_COMBOS}")
    return axes, combos


def _variant(spec: dict, axes: list[tuple[str, list]], combo: tuple, invalid: list[str]) -> Optional[dict]:
    """The spec with this combo's values, or None (reason appended to `invalid`)
    when the combo itself is not a valid strategy (e.g. scale-out steps past
    100%): one bad corner of a grid must not sink the whole sweep."""
    s = copy.deepcopy(spec)
    for (path, _), v in zip(axes, combo):
        set_path(s, path, v)
    try:
        return normalize(s)
    except ValueError as e:
        invalid.append(str(e))
        return None


def walk_forward(spec: dict, data: dict[str, dict], grid: list[dict[str, Any]], metric: str = "net_pnl",
                 min_trades: int = 5, trade_from: Optional[int] = None, windows: int = 4,
                 is_mult: float = 3.0, anchored: bool = False) -> dict[str, Any]:
    """Rolling walk-forward optimisation (backtest only).

    The range is cut into `is_mult + windows` equal slices. For each of the
    last `windows` slices the grid is ranked on the `is_mult` slices before it
    (anchored: on everything before it) and the winner then trades that slice
    unseen. The stitched out-of-sample slices are the honest estimate of
    "re-optimise every period, trade the winner". Walk-forward efficiency =
    out-of-sample P&L per day / in-sample P&L per day.

    ponytail: each combo runs once over the whole range and its trades are
    sliced by entry time, so compounding / free capital carry across slices
    per combo, not along the stitched path; re-run per slice if that matters.
    """
    windows, is_mult = int(windows), float(is_mult)
    if not 2 <= windows <= 12:
        raise ValueError("walk-forward takes 2-12 out-of-sample windows")
    if not 0.5 <= is_mult <= 10:
        raise ValueError("in-sample length must be 0.5-10x an out-of-sample window")
    axes, combos = _grid(grid, metric)
    caches: dict[str, dict] = {}
    runs, capital, invalid = [], 0.0, []
    for combo in combos:
        s = _variant(spec, axes, combo, invalid)
        if s is None:
            continue
        capital = float(s["portfolio"]["capital"])
        runs.append(({p: v for (p, _), v in zip(axes, combo)}, run(s, data, caches, trade_from)["trades"]))
    if not runs:
        raise ValueError(f"no valid combination: {invalid[0]}")
    t0 = trade_from if trade_from is not None else min(d["t"][0] for d in data.values())
    t1 = max(d["t"][-1] for d in data.values()) + 1
    span = (t1 - t0) / (is_mult + windows)
    folds, oos_trades, is_net, is_s = [], [], 0.0, 0.0
    for k in range(windows):
        o0 = t0 + (is_mult + k) * span
        o1 = t1 if k == windows - 1 else o0 + span
        i0 = t0 if anchored else o0 - is_mult * span
        best = None
        for params, trades in runs:
            # in-sample = entered AND exited before the window: no P&L from inside it
            ins = [x for x in trades if i0 <= x["entry_t"] and x["exit_t"] < o0]
            if len(ins) < min_trades:
                continue
            st_ = stats(ins, capital)
            key = (st_[metric] is not None, st_[metric] or 0)
            if best is None or key > best[0]:
                best = (key, params, st_, trades)
        fold: dict[str, Any] = {"is_from": int(i0), "oos_from": int(o0), "oos_to": int(o1),
                                "params": None, "is": None, "oos": None}
        if best:
            outs = [x for x in best[3] if o0 <= x["entry_t"] < o1]
            oos_trades += outs
            fold.update({"params": best[1], "is": summary(best[2]), "is_value": best[2][metric],
                         "oos": summary(stats(outs, capital))})
            is_net += best[2]["net_pnl"]
            is_s += o0 - i0
        folds.append(fold)
    total = stats(oos_trades, capital)
    oos_s = sum(f["oos_to"] - f["oos_from"] for f in folds if f["params"])
    wfe = (total["net_pnl"] / oos_s) / (is_net / is_s) * 100 if oos_s and is_s and is_net > 0 else None
    return {"combos": len(combos), "invalid": len(invalid), "metric": metric, "windows": windows,
            "anchored": anchored, "folds": folds,
            "oos": {**summary(total), "max_drawdown": total["max_drawdown"], "equity": total["equity"]},
            "wfe_pct": round(wfe, 1) if wfe is not None else None}


def overfitting_stats(daily: list[dict[int, float]], capital: float, *, n_trials: int) -> Optional[dict[str, Any]]:
    """Deflated Sharpe Ratio of the top-ranked configuration and the
    Probability of Backtest Overfitting across all ranked ones.

    `daily` is in rank order (best first), one {day: net P&L} per
    configuration. Days a configuration did not trade count as a 0 return.
    `n_trials` is EVERY combination tried, not just the ranked ones: the
    deflation is only honest with the full count of attempts."""
    from app.research import stats as rstats

    if not daily or not capital:
        return None
    days = sorted({k for d in daily for k in d})
    if len(days) < 10:
        return {"note": "fewer than 10 trading days in the selection sample; not computed"}
    matrix = [[d.get(k, 0.0) / capital for d in daily] for k in days]
    best = [row[0] for row in matrix]
    trial_sr = []
    for j in range(len(daily)):
        col = [row[j] for row in matrix]
        sd = rstats.stdev(col)
        if sd:
            trial_sr.append(sum(col) / len(col) / sd)
    out: dict[str, Any] = {
        "days": len(days),
        "deflated_sharpe": rstats.deflated_sharpe(best, n_trials=max(1, n_trials),
                                                  trial_sharpes=trial_sr if len(trial_sr) > 1 else None),
    }
    # 8 blocks = 70 in/out combinations: enough for a stable estimate, cheap
    # next to the up-to-400 backtests the grid already ran.
    splits = next((k for k in (8, 6, 4) if len(days) // k >= 2), None)
    out["pbo"] = rstats.pbo_cscv(matrix, n_splits=splits) if splits and len(daily) >= 2 else None
    return out
