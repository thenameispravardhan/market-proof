"""The research loop: one candidate through every out-of-sample test, one verdict.

A strategy idea earns a LIVE switch only when its result holds out of
sample. One call runs it through all of it on the same trades:

  * held-out split — the last `oos_pct` % of the range is never looked at
    while choosing; the chosen combo then trades it unseen;
  * rolling walk-forward — re-choose on each in-sample window, trade the next
    one unseen, stitch the unseen windows;
  * Deflated Sharpe — after EVERY try on the idea (the strategy's trial count
    plus this grid), not just this sweep;
  * PBO — how often the in-sample winner lands in the bottom half out of
    sample (needs a grid of 2+ combos).

Each combo is backtested ONCE over the whole range; the split, the folds and
both statistics are cut from those trades by entry time. With no grid the
idea's rules are tested as they stand (one combo, every window traded).

`verdict()` holds the bar: pass only when no check fails. The result is
stored as a strategy card (app/db/models.AlgoResearchCard) — the record of
what was tried, how many times, and whether it survived.
"""
from __future__ import annotations

from typing import Any, Optional

from app.algo import engine

BAR = {"dsr": 0.95, "pbo": 0.5}
CURVE_POINTS = 400          # a stored card's equity curve is cut to this many points


def _day_net(trades: list[dict[str, Any]]) -> dict[int, float]:
    out: dict[int, float] = {}
    for x in trades:
        k = (x["exit_t"] + engine.IST) // 86400
        out[k] = out.get(k, 0.0) + x["net"]
    return out


def _thin(curve: list, n: int = CURVE_POINTS) -> list:
    if len(curve) <= n:
        return curve
    step = len(curve) / n
    return [curve[int(i * step)] for i in range(n - 1)] + [curve[-1]]


def check(spec: dict, data: dict[str, dict], grid: Optional[list[dict[str, Any]]], *, oos_from: int,
          metric: str = "net_pnl", min_trades: int = 10, trade_from: Optional[int] = None,
          windows: int = 4, is_mult: float = 3.0, anchored: bool = False, prior_trials: int = 0) -> dict[str, Any]:
    """Run the idea (the spec, or each combo of `grid` on it) through the
    held-out split, a walk-forward and the overfitting statistics.
    `prior_trials`: tries on the idea before this check."""
    windows, is_mult = engine.wf_check(windows, is_mult)
    if grid:
        axes, combos = engine._grid(grid, metric)     # noqa: SLF001
    else:
        if metric not in engine.METRICS:
            raise ValueError(f"metric must be one of {engine.METRICS}")
        axes, combos = [], [()]
    caches: dict[str, dict] = {}
    runs: list[tuple[dict, list[dict[str, Any]]]] = []
    capital, invalid = 0.0, []
    for combo in combos:
        s = engine._variant(spec, axes, combo, invalid)    # noqa: SLF001
        if s is None:
            continue
        capital = float(s["portfolio"]["capital"])
        runs.append(({p: v for (p, _), v in zip(axes, combo)}, engine.run(s, data, caches, trade_from)["trades"]))
    if not runs:
        raise ValueError(f"no valid combination: {invalid[0]}")
    n_trials = max(1, int(prior_trials) + len(combos))
    fixed = not axes

    # held-out split: choose on the trades entered before oos_from only
    ranked = []
    for params, trades in runs:
        ins = [x for x in trades if x["entry_t"] < oos_from]
        st = engine.stats(ins, capital)
        if fixed or st["trades"] >= min_trades:
            ranked.append(((st[metric] is not None, st[metric] or 0), params, st, trades, _day_net(ins)))
    ranked.sort(key=lambda r: r[0], reverse=True)
    split: Optional[dict[str, Any]] = None
    best_trades: list[dict[str, Any]] = []
    if ranked:
        _, params, st_is, best_trades, _ = ranked[0]
        outs = [x for x in best_trades if x["entry_t"] >= oos_from]
        st_oos = engine.stats(outs, capital)
        split = {"params": params, "oos_from": int(oos_from), "is": engine.summary(st_is),
                 "oos": {**engine.summary(st_oos), "max_drawdown": st_oos["max_drawdown"]},
                 "ranked": len(ranked)}

    # Deflated Sharpe and PBO. A grid: on the selection (in-sample) sample,
    # as the optimiser reports them. Fixed rules: the whole range, deflated
    # by every try on the idea (no other configurations, so no PBO).
    over: Optional[dict[str, Any]]
    if fixed:
        over = {"deflated_sharpe": engine.trial_dsr(runs[0][1], data, capital, n_trials, trade_from), "pbo": None}
    elif ranked:
        over = engine.overfitting_stats([r[4] for r in ranked], capital, n_trials=n_trials)
    else:
        over = None

    wf, _ = engine.wf_folds(runs, data, capital, metric, 0 if fixed else min_trades, trade_from,
                            windows, is_mult, anchored)
    wf["oos"]["equity"] = _thin(wf["oos"]["equity"])
    wf["param_sets"] = len({tuple(sorted(f["params"].items())) for f in wf["folds"] if f["params"]})
    wf["is_mult"] = is_mult

    full = engine.stats(best_trades, capital) if ranked else None
    out = {"combos": len(combos), "invalid": len(invalid), "metric": metric, "min_trades": min_trades,
           "trials": n_trials, "fixed": fixed, "capital": capital,
           "split": split, "walk_forward": wf, "overfitting": over,
           "full": ({**engine.summary(full), "max_drawdown_pct": full["max_drawdown_pct"],
                     "equity": _thin(full["equity"])} if full else None)}
    out["checks"], out["verdict"] = verdict(out)
    return out


def verdict(res: dict[str, Any], bar: Optional[dict[str, float]] = None) -> tuple[list[dict[str, Any]], str]:
    """Each check is pass (True), fail (False) or not applicable (None).
    The idea passes only when nothing fails; anything else is killed."""
    bar = {**BAR, **(bar or {})}
    n = res["min_trades"]
    over = res.get("overfitting") or {}
    dsr = (over.get("deflated_sharpe") or {}).get("dsr")
    pbo = (over.get("pbo") or {}).get("pbo")
    wf = res["walk_forward"]["oos"]
    split = res.get("split")
    checks = [
        {"key": "dsr", "label": f"Deflated Sharpe ≥ {bar['dsr'] * 100:.0f}% after {res['trials']} tries",
         "value": dsr, "pass": dsr is not None and dsr >= bar["dsr"],
         "note": None if dsr is not None else over.get("note") or "too few trading days or no variance"},
        {"key": "walk_forward", "label": f"Walk-forward unseen windows net > 0 on ≥ {n} trades",
         "value": wf["net_pnl"], "pass": wf["trades"] >= n and wf["net_pnl"] > 0,
         "note": f"{wf['trades']} trades" if wf["trades"] < n else None},
        {"key": "held_out", "label": f"Held-out period net > 0 on ≥ {n} trades",
         "value": split["oos"]["net_pnl"] if split else None,
         "pass": bool(split) and split["oos"]["trades"] >= n and split["oos"]["net_pnl"] > 0,
         "note": None if split and split["oos"]["trades"] >= n
         else f"{split['oos']['trades']} trades" if split else f"no combo had {n} in-sample trades"},
        {"key": "pbo", "label": f"PBO ≤ {bar['pbo'] * 100:.0f}%", "value": pbo,
         "pass": None if pbo is None else pbo <= bar["pbo"],
         "note": "fixed rules — no other configurations to compare" if res["fixed"] and pbo is None
         else None if pbo is not None else "too few days or combos"},
    ]
    return checks, "pass" if all(c["pass"] is not False for c in checks) else "kill"


# ---- the one-page card ------------------------------------------------------

def _operand(o: Optional[dict[str, Any]]) -> str:
    if not isinstance(o, dict):
        return "—"
    if "value" in o and "ind" not in o:
        return str(o["value"])
    if "math" in o:
        return "(" + str(o.get("math")) + ")"
    txt = str(o.get("ind", "?"))
    if o.get("field"):
        txt += f".{o['field']}"
    if o.get("params"):
        txt += "(" + ",".join(str(v) for v in o["params"].values()) + ")"
    if o.get("tf"):
        txt += f" {o['tf']}m"
    if o.get("symbol"):
        txt += f" of {o['symbol']}"
    if o.get("xf"):
        txt = f"{o['xf']}({txt}, {o.get('xn')})"
    return txt


def group_text(g: Optional[dict[str, Any]]) -> str:
    if not g or not g.get("conditions"):
        return "—"
    parts = []
    for c in g["conditions"]:
        if "conditions" in c:
            parts.append("(" + group_text(c) + ")")
            continue
        t = f"{_operand(c.get('left'))} {c.get('op', '?').replace('_', ' ')}"
        if c.get("right") is not None:
            t += f" {_operand(c['right'])}"
        if c.get("right2") is not None:
            t += f" and {_operand(c['right2'])}"
        if c.get("for_bars"):
            t += f" for {c['for_bars']} bars"
        if c.get("within_bars"):
            t += f" within {c['within_bars']} bars"
        parts.append(("NOT " if c.get("negate") else "") + t)
    return f" {g.get('logic', 'AND')} ".join(parts)


def _level(lv: Optional[dict[str, Any]]) -> str:
    if not lv:
        return "none"
    return f"{lv.get('value')} {lv.get('type')}"


def rules_text(spec: dict[str, Any]) -> list[str]:
    """The strategy in a few plain lines, for the card."""
    inst = spec.get("instrument") or {}
    out = [f"{', '.join(spec.get('symbols') or [])} · {spec.get('timeframe')}m · {spec.get('direction')} · "
           f"{inst.get('type', 'equity')}"]
    if spec.get("direction") != "short":
        out.append(f"Long when {group_text(spec.get('entry_long'))}"
                   + (f"; exit when {group_text(spec['exit_long'])}" if spec.get("exit_long") else ""))
    if spec.get("direction") != "long":
        out.append(f"Short when {group_text(spec.get('entry_short'))}"
                   + (f"; exit when {group_text(spec['exit_short'])}" if spec.get("exit_short") else ""))
    costs = spec.get("costs") or {}
    out.append(f"Stop {_level(spec.get('stop_loss'))} · target {_level(spec.get('target'))} · "
               f"slippage {costs.get('slippage_pct')}% · charges {'on' if costs.get('charges') else 'off'}")
    return out


def _inr(v: Optional[float]) -> str:
    return "—" if v is None else f"₹{v:,.0f}"


def _pct(v: Optional[float]) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def markdown(card: dict[str, Any]) -> str:
    """One page per idea: rules, tries, DSR, PBO, out-of-sample P&L, verdict."""
    r = card["result"]
    setup = card.get("setup") or {}
    lines = [f"# {card['name']} — {'PASS' if card['verdict'] == 'pass' else 'KILLED'}", "",
             f"Card #{card['id']} · {card.get('created_at') or ''} · strategy "
             f"{('#' + str(card['strategy_id']) + (' v' + str(card['version']) if card.get('version') else '')) if card.get('strategy_id') else 'not saved'}"
             f" · {setup.get('start')} → {setup.get('end')}", ""]
    if card.get("hypothesis"):
        lines += ["## Idea", "", card["hypothesis"], ""]
    lines += ["## Rules", ""] + [f"- {x}" for x in rules_text(card["spec"])]
    if setup.get("grid"):
        lines += ["- Grid: " + "; ".join(f"{g['path']} ∈ {g['values']}" for g in setup["grid"])]
    lines += ["", "## Bar", "", "| Check | Value | Result |", "|---|---|---|"]
    for c in card["checks"]:
        v = c["value"]
        val = _pct(v) if c["key"] in ("dsr", "pbo") else _inr(v)
        res = "pass" if c["pass"] else "n/a" if c["pass"] is None else "FAIL"
        lines.append(f"| {c['label']} | {val} | {res}{' — ' + c['note'] if c.get('note') else ''} |")
    split, wf = r.get("split"), r["walk_forward"]
    lines += ["", "## Out of sample", "",
              f"- Tries on this idea: {r['trials']} ({r['combos']} in this check)"]
    if split:
        o = split["oos"]
        lines.append(f"- Held-out period: {o['trades']} trades, net {_inr(o['net_pnl'])}, PF {o['profit_factor']}, "
                     f"Sharpe {o['sharpe']} (in-sample net {_inr(split['is']['net_pnl'])})"
                     + (f" — chosen {split['params']}" if split["params"] else ""))
    w = wf["oos"]
    lines.append(f"- Walk-forward ({wf['windows']} {'anchored' if wf['anchored'] else 'rolling'} windows): "
                 f"{w['trades']} trades, net {_inr(w['net_pnl'])}, PF {w['profit_factor']}, "
                 f"efficiency {'—' if wf['wfe_pct'] is None else str(wf['wfe_pct']) + '%'}, "
                 f"{wf['param_sets']} distinct parameter set(s) across windows")
    if r.get("full"):
        f = r["full"]
        lines.append(f"- Whole range (chosen combo): {f['trades']} trades, net {_inr(f['net_pnl'])}, "
                     f"max DD {f['max_drawdown_pct']}%")
    if card.get("note"):
        lines += ["", "## Notes", "", card["note"]]
    return "\n".join(lines) + "\n"
