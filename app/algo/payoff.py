"""Payoff, Greeks and funds required for one set of a strategy's legs — the
builder's preview, so a position can be seen before it is backtested.

The legs are resolved exactly as a backtest resolves them (engine.sim_legs:
same strikes, expiries, lots) at a given spot and IV, priced with
Black-Scholes. Curves are P&L in rupees against the underlying's price: at
the first expiry (later legs still carry time value — calendars), today
(T+0) and halfway. Margin is shown both ways: the exchange-style SPAN +
exposure estimate and the strategy's own leverage model, plus what each leg
would block on its own — buying vs writing, and what a hedge saves.
"""
from __future__ import annotations

import math
from typing import Any, Optional

from app.algo import engine, fno

POINTS = 161


def _value(lg: dict, s: float, at: float, iv: float) -> float:
    if lg["kind"] in ("EQ", "FUT"):
        return s
    if at >= lg["exp"]:
        return max(0.0, s - lg["K"]) if lg["kind"] == "CE" else max(0.0, lg["K"] - s)
    return fno.bs_price(s, lg["K"], fno.years_to(lg["exp"], at), fno.leg_iv(lg, s, iv), lg["kind"])


def _pnl(legs: list[dict], s: float, at: float, iv: float) -> float:
    return sum(lg["act"] * (_value(lg, s, at, iv) - lg["price"]) * lg["qty"] for lg in legs)


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def preview(spec: dict[str, Any], side: str, spot: float, t: float, iv: float,
            meta: dict[str, Any], capital: Optional[float] = None) -> dict[str, Any]:
    """`spec` normalized; `iv` decimal; `meta` {symbol, name, exch, lot, step?}."""
    legs = engine.sim_legs(spec, side, meta, spot, t, iv)
    if not legs:
        raise ValueError("no legs with lots on this side")
    for lg in legs:
        lg["qty"] = lg["per_set"]
        lg["price"] = engine._leg_px(lg, spot, t, iv)   # noqa: SLF001 — the backtest's own pricing
    opts = [lg for lg in legs if lg["kind"] in ("CE", "PE")]
    t1 = min((lg["exp"] for lg in opts), default=t + 86400)
    yrs = fno.years_to(t1, t)
    sd = spot * iv * math.sqrt(yrs)
    far = max((abs(lg["K"] - spot) for lg in opts), default=0.0)
    half = min(spot * 0.5, max(4 * sd, spot * 0.03, far * 1.4))
    lo, hi = max(spot * 0.01, spot - half), spot + half
    xs = [lo + (hi - lo) * k / (POINTS - 1) for k in range(POINTS)]
    mid = t + (t1 - t) / 2
    curve = [{"spot": round(x, 2), "expiry": round(_pnl(legs, x, t1, iv), 2),
              "now": round(_pnl(legs, x, t, iv), 2), "mid": round(_pnl(legs, x, mid, iv), 2)} for x in xs]

    # Beyond the grid the expiry P&L is a straight line: its slope above the
    # highest strike decides "unlimited"; below, the floor is the price at ~0.
    up_slope = sum(lg["act"] * lg["qty"] for lg in legs if lg["kind"] != "PE")
    exp_vals = [p["expiry"] for p in curve] + [_pnl(legs, spot * 0.001, t1, iv)]
    max_profit = None if up_slope > 0 else round(max(exp_vals), 2)
    max_loss = None if up_slope < 0 else round(min(exp_vals), 2)
    bes = []
    for a, b in zip(curve, curve[1:]):
        if (a["expiry"] < 0) != (b["expiry"] < 0) and a["expiry"] != b["expiry"]:
            bes.append(round(a["spot"] + (b["spot"] - a["spot"]) * (-a["expiry"]) / (b["expiry"] - a["expiry"]), 2))

    # Probability of profit at the first expiry: lognormal spot at `iv`, risk-neutral drift.
    var = iv * iv * yrs
    if var > 0:
        mu = math.log(spot) + (fno.RISK_FREE - iv * iv / 2) * yrs
        cdf = lambda x: _ncdf((math.log(x) - mu) / math.sqrt(var))  # noqa: E731
        edges = [0.0] + [(a["spot"] + b["spot"]) / 2 for a, b in zip(curve, curve[1:])] + [math.inf]
        pop = sum((1.0 if e1 == math.inf else cdf(e1)) - (cdf(e0) if e0 > 0 else 0.0)
                  for p, e0, e1 in zip(curve, edges, edges[1:]) if p["expiry"] > 0)
    else:
        pop = 1.0 if _pnl(legs, spot, t1, iv) > 0 else 0.0

    greek_keys = ("delta", "gamma", "theta", "vega", "rho", "vanna", "charm", "vomma", "speed")
    rows, net = [], {k: 0.0 for k in greek_keys}
    mg = spec.get("margin") or {}
    span_kw = {"name": meta["name"], "iv": iv, "scan_pct": mg.get("scan_pct"), "exposure_pct": mg.get("exposure_pct")}
    for lg in legs:
        g = ({**{k: 0.0 for k in greek_keys}, "delta": 1.0, "prob_itm": None} if lg["kind"] in ("EQ", "FUT")
             else fno.bs_greeks(spot, lg["K"], fno.years_to(lg["exp"], t), fno.leg_iv(lg, spot, iv), lg["kind"]))
        pos = {k: g[k] * lg["act"] * lg["qty"] for k in greek_keys}
        for k in net:
            net[k] += pos[k]
        alone = (fno.span_margin([lg], spot, t, **span_kw)["total"] if lg["kind"] != "EQ"
                 else lg["price"] * lg["qty"] / spec["portfolio"]["leverage"])
        rows.append({"label": lg["label"], "kind": lg["kind"], "side": "BUY" if lg["act"] > 0 else "SELL",
                     "strike": lg.get("K"), "expiry": lg.get("exp"), "qty": lg["qty"],
                     "dte": round((lg["exp"] - t) / 86400, 2) if lg.get("exp") else None,
                     "price": round(lg["price"], 2), "delta": round(g["delta"], 3),
                     "prob_itm_pct": round(g["prob_itm"] * 100, 1) if g["prob_itm"] is not None else None,
                     # elasticity: % the premium moves for a 1% move of the underlying
                     "lambda": round(g["delta"] * spot / lg["price"], 2) if lg["kind"] in ("CE", "PE") and lg["price"] else None,
                     "rho": round(pos["rho"], 2), "vanna": round(pos["vanna"], 4), "charm": round(pos["charm"], 4),
                     "vomma": round(pos["vomma"], 2),
                     # its own stop / target, as premium prices (fno levels from the entry)
                     "sl_px": round(lg["price"] * (1 - lg["act"] * lg["sl_pct"] / 100), 2) if lg.get("sl_pct") else None,
                     "tg_px": round(lg["price"] * (1 + lg["act"] * lg["tg_pct"] / 100), 2) if lg.get("tg_pct") else None,
                     "pos_delta": round(pos["delta"], 2), "theta": round(pos["theta"], 2),
                     "vega": round(pos["vega"], 2), "gamma": round(pos["gamma"], 5),
                     "margin_alone": round(alone, 2),
                     # what the same contract blocks bought vs written — the buyer / seller gap
                     "margin_if_bought": round(lg["price"] * lg["qty"], 2) if lg["kind"] in ("CE", "PE") else None,
                     "margin_if_sold": round(fno.span_margin([{**lg, "act": -1}], spot, t, **span_kw)["total"], 2)
                     if lg["kind"] in ("CE", "PE") else None})
    fo = [lg for lg in legs if lg["kind"] != "EQ"]
    span = fno.span_margin(fo, spot, t, price_key="price", qty_key="qty", **span_kw) if fo else \
        {"span": 0.0, "exposure": 0.0, "premium": 0.0, "total": 0.0, "scan_pct": None, "exposure_pct": None}
    eq_part = sum(lg["price"] * lg["qty"] / spec["portfolio"]["leverage"] for lg in legs if lg["kind"] == "EQ")
    span_total = span["total"] + eq_part
    lev_total = engine.position_margin(legs, spot, spec["portfolio"]["leverage"], price_key="price", qty_key="qty")
    used = span_total if mg.get("model") == "span" else lev_total
    naked = sum(r["margin_alone"] for r in rows)
    credit = sum(-lg["act"] * lg["price"] * lg["qty"] for lg in opts)
    capital = capital if capital is not None else spec["portfolio"]["capital"]
    for lg in legs:
        lg["entry"] = lg["price"]
    pm = engine.position_metrics(legs, spot, t, int(meta.get("lot") or 1), lambda lg: lg["price"], iv) if opts else None
    rules = [{"rule": engine.rule_label(r), "metric": r["metric"],
              "value": None if pm is None or pm.get(r["metric"]) is None else round(pm[r["metric"]], 4),
              "ok": pm is not None and engine._rule_true(r, pm)}       # noqa: SLF001
             for r in (spec.get("position_rules") or {}).get("entry", [])]
    return {
        "spot": round(spot, 2), "iv_pct": round(iv * 100, 2), "lot": meta.get("lot"), "side": side,
        "first_expiry": t1 if opts else None, "dte": round((t1 - t) / 86400, 2) if opts else None,
        "legs": rows, "curve": curve, "breakevens": bes,
        "max_profit": max_profit, "max_loss": max_loss, "pop_pct": round(pop * 100, 1),
        "expected_move": round(sd, 2) if opts else None,
        "greeks": {k: round(v, 7 if k == "speed" else 5 if k in ("gamma", "vanna", "charm") else 2) for k, v in net.items()},
        # the same numbers the position rules use (per set: delta / gamma in lots, theta / vega in ₹)
        "position": {k: (round(v, 4) if v is not None else None) for k, v in pm.items()} if pm else None,
        "entry_rules": rules,
        "premium": {"net": round(credit, 2), "kind": "credit" if credit > 0 else "debit" if credit < 0 else "none"},
        "margin": {"model": mg.get("model") or "leverage", "used": round(used, 2),
                   "span": {**span, "total": round(span_total, 2)}, "leverage": round(lev_total, 2),
                   "leverage_x": spec["portfolio"]["leverage"],
                   "naked": round(naked, 2), "hedge_benefit": round(max(0.0, naked - span_total), 2),
                   "sets_affordable": int(capital // used) if used > 0 else None, "capital": capital},
        "return_on_margin_pct": round(max_profit / used * 100, 2) if max_profit is not None and used > 0 else None,
    }
