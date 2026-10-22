# B3 → A4 hand-off: two-symbol positions (`pair_leg`)

B3 (backtest) is done; this is the shape A4 (live / paper) builds on. Lead: B.

## Spec key

```json
"pair_leg": {"symbol": "NSE:NIFTYBANK-INDEX", "kind": "FUT", "ratio": {"type": "beta", "n": 100}, "expiry": "current"}
```

- `kind`: `FUT` = the pair symbol's future (`expiry` current / next, whole lots), `EQ` = shares.
- `ratio`: ₹ of the pair leg per ₹1 of the position. `beta` = `engine._pair("beta", strategy closes,
  pair closes aligned, n)` read **at the signal candle's close** (index `sig_i`), never the fill's.
  `fixed` = `value`.
- Validation (`engine._check_pair_leg`): equity or futures instrument only, time candles, market
  entries, no stop / target / trailing / breakeven / scale-out. Exits: exit conditions, `max_bars`,
  MTM ₹ stop / target / trail **on each close**, square-off, daily limits.

## Leg shape (what a backtest trade carries, and what the live position should hold)

```json
"legs": [
  {"symbol": "NSE:HDFCBANK-EQ", "label": "NSE:HDFCBANK-EQ", "side": "BUY", "qty": 600, "entry": 1712.4, "exit": 1730.1},
  {"symbol": "NSE:NIFTYBANK-INDEX", "label": "BANKNIFTY FUT 28OCT26", "side": "SELL", "qty": 30, "entry": 54210.0, "exit": 54050.5}
],
"hedge": {"ratio": 1.12, "actual": 1.58}
```

Inside the engine the pair leg is `{"kind", "act": -sign, "symbol", "other": True, "exp"?, "ratio", "entry", "qty"}`;
`other` is how pricing tells it apart (it is priced off the pair symbol, not the position's underlying).

Sizing: the position's sets are sized on both legs' margin; the pair leg's qty =
`round(ratio × first-leg value / pair entry price / lot) × lot`; under one lot → skipped
(`pair_too_small`), never traded half-hedged.

## What A4 needs to build

1. Order both legs at the same candle open; if the second leg is refused, close the first
   (roll back) and log it — never hold one leg.
2. Price the pair leg from its own quotes for MTM; MTM rules on the candle close like the backtest.
3. Close both legs together on every exit reason.
4. Lift the guard: `PUT /api/algo/strategies/{id}` refuses `enabled` while the spec has a
   `pair_leg` (app/api/algo.py, `update_strategy`). Remove it in the same change that lands 1–3.

Live warm-up already counts the β window (`engine.warmup_bars`), and `engine.ref_frames` lists the
pair symbol, so `data.live_bundle` loads its candles with no change.
