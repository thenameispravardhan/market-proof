"""Tick replay: were the candle backtest's exits the ones the ticks made?

A candle says how high and how low price went, not in which order. When a
trade's stop and target both sit inside one candle, the backtest has to
assume a path (open -> low -> high -> close on a green candle) and may book
a target the market reached only after the stop. Recorded trade prints (B0)
settle it: walk them from the fill and see which level price touched first.

Replayed: trades with a fixed stop and / or target on the traded symbol's own
price (shares, or a future with its levels on the future). Trailing stops,
breakeven, scale-outs and option premiums are not replayed — those trades are
counted as such, never silently passed.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

LEVEL_EXITS = ("SL", "TARGET")


def replay_trade(x: dict[str, Any], ticks: list[tuple[int, float]], bar_s: int) -> dict[str, Any]:
    """`ticks`: (ts, price) from the fill on, in order. The first level price
    touches is the tick exit; none before the backtest's exit candle closes
    means the backtest's own exit (a signal, square-off ...) stands."""
    sign = 1 if x["side"] == "BUY" else -1
    sl, tg = x.get("stop"), x.get("target")
    end = x["exit_t"] + bar_s                     # the backtest's exit candle, in full
    hit: Optional[tuple[str, int, float]] = None
    both_in_one = False
    for t, p in ticks:
        if t < x["entry_t"]:
            continue
        if t >= end:
            break
        if sl is not None and sign * (p - sl) <= 0:
            hit = ("SL", t, sl)
        elif tg is not None and sign * (p - tg) >= 0:
            hit = ("TARGET", t, tg)
        if hit:
            break
    if sl is not None and tg is not None:
        # did the backtest's exit candle hold both levels? then its path guess decided
        rng = [p for t, p in ticks if x["exit_t"] <= t < end]
        both_in_one = bool(rng) and min(rng) <= min(sl, tg) and max(rng) >= max(sl, tg)
    bt = x["reason"] if x["reason"] in LEVEL_EXITS else "OTHER"
    tick = hit[0] if hit else "OTHER"
    out = {"symbol": x["symbol"], "side": x["side"], "entry_t": x["entry_t"], "exit_t": x["exit_t"],
           "bt_reason": x["reason"], "bt_exit": x["exit"], "tick_reason": tick if hit else x["reason"],
           "tick_t": hit[1] if hit else None, "tick_exit": hit[2] if hit else x["exit"],
           "both_levels_in_exit_candle": both_in_one, "agree": bt == tick}
    out["pnl_diff"] = round(sign * (out["tick_exit"] - x["exit"]) * x["qty"], 2)
    return out


def replay(trades: list[dict[str, Any]], spec: dict[str, Any],
           ticks_for: Callable[[str, int, int], list[tuple[int, float]]]) -> dict[str, Any]:
    """Every replayable trade of a backtest against `ticks_for(symbol, from,
    to)` -> [(ts, price)]. Summary: how many agree, how many exits the ticks
    reversed, the P&L that moves, and how the ambiguous candles went."""
    inst = spec["instrument"]
    why_not = None
    if inst["type"] == "option":
        why_not = "option premiums aren't replayed (their levels move with the underlying and IV)"
    elif inst["type"] == "future" and inst.get("levels_on") == "underlying":
        why_not = "levels on the underlying of a future aren't replayed (the index has no prints)"
    elif spec.get("trailing") or spec.get("breakeven") or spec.get("scale_out"):
        why_not = "trailing stops, breakeven and scale-outs aren't replayed — only fixed stops / targets"
    elif spec.get("pair_leg"):
        why_not = "pair positions exit on their spread, not on one leg's price"
    elif not (spec.get("stop_loss") or spec.get("target")):
        why_not = "no stop or target to replay"
    if why_not:
        return {"replayable": False, "why": why_not, "trades": len(trades)}
    bar_s = int(spec["timeframe"]) * 60
    rows, no_ticks = [], 0
    for x in trades:
        tk = ticks_for(x["symbol"], x["entry_t"], x["exit_t"] + bar_s)
        if not tk:
            no_ticks += 1
            continue
        rows.append(replay_trade(x, tk, bar_s))
    flipped = [r for r in rows if not r["agree"]]
    amb = [r for r in rows if r["both_levels_in_exit_candle"]]
    return {"replayable": True, "trades": len(trades), "replayed": len(rows), "no_ticks": no_ticks,
            "agree": len(rows) - len(flipped),
            "agree_pct": round((len(rows) - len(flipped)) / len(rows) * 100, 1) if rows else None,
            "pnl_diff": round(sum(r["pnl_diff"] for r in rows), 2),
            "ambiguous_candles": len(amb), "ambiguous_right": sum(1 for r in amb if r["agree"]),
            "flipped": {f"{a}->{b}": sum(1 for r in flipped if (r["bt_reason"], r["tick_reason"]) == (a, b))
                        for a, b in sorted({(r["bt_reason"], r["tick_reason"]) for r in flipped})},
            "disagreements": flipped[:200]}
