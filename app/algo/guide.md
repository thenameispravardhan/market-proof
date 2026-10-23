# Algo Lab guide — everything the Builder & Backtest can do

This guide is written for two readers: you, and an AI assistant you hand it
to. Give it to any AI chat (ChatGPT, Claude, Gemini…), describe the strategy
you want in plain words, and ask for the JSON. Then import that JSON in the
Algo Lab (**⬆ import** for a file, **📋 paste JSON** for text) — the builder
fills in, you backtest, tweak, save and automate.

Sections 10-12 walk through every field of the Builder screen, every
number on the results screen and the other tabs. The last part of this file
(*Reference*) is generated from the running app,
so the indicator list, their settings and every allowed value are always
current.

---

## 0. Prompt to give the AI

Copy this, fill in the last line, and attach this guide:

```text
You are helping me build a trading strategy for the "Algo Lab" in my trading app.
The attached guide describes the strategy JSON exactly. Follow it strictly:
- Reply with ONE JSON object of the form {"name": "...", "spec": {...}} and nothing else
  (inside a json code block is fine).
- Use only the keys, indicators, fields, operators and values the guide lists.
  Numbers are JSON numbers, never strings. Times are "HH:MM" in IST.
- Symbols are Fyers symbols such as "NSE:SBIN-EQ" or "NSE:NIFTY50-INDEX".
- Option positions: multi-leg positions need "levels_on": "underlying" or stop_loss /
  target set to null (manage them with mtm, per-leg sl_pct / tg_pct or position_rules).
- A candle's BODY is abs(close - open) (see 4.5); PRICE "range" is high - low, wicks included.
- Two indicators that should cross (EMA 5 / EMA 24) use the same source unless I say otherwise.
- Keep it as simple as the idea allows; leave out anything not needed (defaults apply).
- After the JSON, list in 2-3 lines any assumption you made.

My strategy: <describe it here — market, timeframe, entry, exit, risk, what to trade>
```

If the import shows an error, paste the error back to the AI and ask it to fix
the JSON — the messages say exactly which value is wrong.

---

## 1. How the engine works (read once)

* **One engine everywhere.** The backtest, the optimiser and the live runner
  run the same JSON with the same code: what you backtest is what runs.
* **Signals come from the underlying symbol** (the stock or index you list in
  `symbols`). The **instrument** decides what is actually traded: the symbol
  itself (cash), its future, or option legs.
* **Candles.** Conditions are checked on each **completed** candle's close.
  An entry fills at the **next candle's open** (live: the price right after
  the close). Nothing ever sees a candle that is still forming.
* **Stops and targets** are checked inside each candle; when one candle
  touches both, the stop is assumed to come first (the conservative choice).
  A market entry fills at the candle's open, so that whole candle counts for
  its stop and target. A pullback / breakout entry that fills inside a candle
  can still be stopped in it (measured from the fill), but can't book a
  target there — nor count that candle's high / low from before its fill.
* **Everything is flat by the square-off time** (unless square-off is
  switched off — then positions carry overnight until an exit rule closes them;
  an F&O position is still closed at 15:15 on its nearest contract's expiry
  day, never held into the exchange's settlement (stock F&O deliver
  physically), and nothing new is opened in a contract expiring that day after
  15:15 — exit reason `EXPIRY`, live and in backtests. A position found still
  open after its expiry (the runner was down; a backtest data gap) is booked at
  its settlement value: options at their intrinsic value, futures at the close).
* **Options in backtests are priced with Black-Scholes** from the underlying's
  real candles (Fyers keeps no history for expired contracts). IV comes from
  India VIX (indices), the stock's realised volatility, a fixed number, or
  — where option chains were recorded — the recorded IV surface; option
  backtests are good estimates, not exact fills. Live trading uses the
  real option chain and real prices.
* **One portfolio.** All symbols of a strategy share one capital pool;
  positions compete for it, `max_positions` caps how many are open at once.
* **Costs.** Slippage on every fill and Indian charges (brokerage, STT,
  exchange, SEBI, GST, stamp duty — per segment) are deducted.

---

## 2. The JSON at a glance

An import file is `{"name": "...", "spec": {...}}` (a bare `spec` object also
works). Everything you leave out takes the default shown here:

```jsonc
{
  "name": "My strategy",
  "spec": {
    "symbols": ["NSE:SBIN-EQ"],               // 1-50 Fyers symbols; signals are computed on these
    "timeframe": 5,                            // minutes per candle (see Reference)
    "bars": {"type": "time", "per_day": 50},   // time | volume | turnover candles
    "direction": "long",                       // long | short | both
    "entry_long":  {"logic": "AND", "conditions": []},   // when to take a LONG / bullish signal
    "exit_long":   null,                                 // optional extra exit conditions
    "entry_short": {"logic": "AND", "conditions": []},   // when to take a SHORT / bearish signal
    "exit_short":  null,
    "instrument": {
      "type": "equity",                        // equity | future | option
      "expiry": "current",                     // current | next
      "expiry_kind": "weekly",                 // weekly | monthly (options)
      "legs_long":  [{"right": "CE", "action": "BUY", "strike": "ATM", "steps": 0, "premium": 100, "lots": 1}],
      "legs_short": [{"right": "PE", "action": "BUY", "strike": "ATM", "steps": 0, "premium": 100, "lots": 1}],
      "levels_on": "instrument",               // where stop / target are measured: instrument | underlying
      "iv": {"source": "auto", "value": 15}    // backtest option pricing: auto | vix | hv | fixed (value %) | surface
    },
    "stop_loss": {"type": "pct", "value": 1.0},
    "target":    {"type": "pct", "value": 2.0},
    "trailing":  null,
    "breakeven": null,
    "mtm":   {"stop": null, "target": null, "trail_start": null, "trail_gap": null},
    "daily": {"max_loss": null, "max_profit": null},
    "session": {"start": "09:20", "end": "15:00", "square_off": "15:15"},
    "entry_order": {"type": "market", "offset_pct": 0.1, "valid_bars": 3},
    "max_trades_per_day": 3,
    "cooldown_bars": 0,
    "max_bars": null,
    "sizing": {"mode": "qty", "value": 1},
    "portfolio": {"capital": 100000, "leverage": 1.0, "max_positions": 10, "compounding": true,
                  "max_position_pct": null},
    "costs": {"slippage_pct": 0.02, "charges": true}
  }
}
```

Optional sections (left out = off): `scale_out`, `margin`, `position_rules`,
`rank` (7), `guard` (6.5, live only), `pair_leg` (6.8), `universe` (7.1),
`entry_order.limit` (6.7, live only), `session.days`, `mtm.stop_pct` /
`mtm.target_pct`, `instrument.on_leg_stop`, `instrument.iv.put_skew` /
`call_skew`, and the per-leg keys `delta`, `expiry`, `sl_pct`, `tg_pct`,
`reentries`, `roll`, `hedge` (5.1).

---

## 3. Symbols, candles and direction

* **symbols** — Fyers symbols: stocks `NSE:RELIANCE-EQ`, indices
  `NSE:NIFTY50-INDEX`, `NSE:NIFTYBANK-INDEX`, `NSE:FINNIFTY-INDEX`,
  `NSE:MIDCPNIFTY-INDEX`, `BSE:SENSEX-INDEX`. A plain name like `SBIN` is
  resolved to its NSE symbol. Up to 50.
* **timeframe** — minutes per candle. **bars.type**: `time` (every
  `timeframe` minutes), `volume` or `turnover` (a candle closes once that much
  has traded; sized so a typical session has about `per_day` candles, 5-1000).
* **direction** — `long` uses `entry_long` / `exit_long` / `legs_long`;
  `short` uses the short side; `both` uses both.
  **Important for options:** "long" / "short" only name WHICH signal fires.
  A strategy that always sells a straddle at 09:20 is `"direction": "long"`
  with an `entry_long` condition and SELL legs in `legs_long`.

---

## 4. Conditions

### 4.1 Groups

```jsonc
{"logic": "AND", "conditions": [ condition, condition, {"logic": "OR", "conditions": [...]} ]}
```

`AND` = all must be true, `OR` = any. A group can contain groups (up to 4
deep). An entry group with no conditions never fires. `exit_long` /
`exit_short` are optional (null = exit only by stops, targets, MTM, rules,
time stop or square-off).

### 4.2 A condition

```jsonc
{"left": operand, "op": ">", "right": operand}
{"left": operand, "op": "between", "right": operand, "right2": operand}
```

| op | true when |
|---|---|
| `>` `<` `>=` `<=` `==` `!=` | compares left with right on this candle |
| `crosses_above` / `crosses_below` | left was ≤ right on the previous candle and is > right now (or the mirror) |
| `crosses` | either cross |
| `rising` / `falling` | left is higher / lower than N candles ago; N = `right.value` (default 1) |
| `between` / `outside` | left is inside / outside the range `right`..`right2` (either order) |

Modifiers on a condition (optional):

* `"for_bars": N` — true only after N candles **in a row** (1-500).
* `"within_bars": N` — true if it was true at least once in the last N candles.
* `"negate": true` — NOT (applied last).

### 4.3 Operands

**A number:** `{"value": 70}`

**An indicator:**

```jsonc
{"ind": "EMA", "params": {"period": 20, "source": "close"}, "field": "value",
 "offset": 0, "tf": 15, "mult": 1.0, "add": 0, "xf": "slope", "xn": 3}
```

* `ind` — indicator name (see Reference). `params` — only the ones you change
  (defaults fill the rest). `field` — which output (default = the first).
* `offset` — candles ago (1 = the previous candle).
* `tf` — evaluate on another timeframe: `1440` = daily, or a multiple of the
  strategy's timeframe. Only that timeframe's **completed** candles are seen.
* `mult`, `add` — `value × mult + add`. "close > EMA × 1.02" is
  `{"ind": "EMA", "mult": 1.02}`; "open > previous close + 20" is
  `{"ind": "DAILY", "field": "prev_close", "add": 20}`.
* `xf` + `xn` — reshape the series before comparing (transforms, below).
* `symbol` — compute this indicator on **another symbol's** candles: an index
  (`"NSE:NIFTY50-INDEX"`, `"NSE:NIFTYBANK-INDEX"` — indices need the full Fyers
  symbol) or a stock (`"SBIN"` or `"NSE:HDFCBANK-EQ"`). Everything on the
  operand (`tf`, `offset`, `xf`, `mult`…) applies on that symbol's candles; each
  candle of the strategy then sees its last **completed** value. Up to 5 other
  symbols per strategy. Not for `VIX`, `DTE`, `TIME` or order-flow indicators.

**A combined value:** `{"math": "-", "a": operand, "b": operand}` with
`math` one of `+ - * /` or `pct_diff` (`(a - b) / b × 100`, the % gap).
Combined values take `mult` / `add` / `xf` too and nest 3 deep.

**A pair** (usually `b` on another `symbol`): `{"math": "pair_z", "a": operand,
"b": operand, "n": 100}` fits log a on log b by least squares over the last
`n` candles (10-500, default 100; each candle sees candles up to its own
close) and gives one of:

| math | value |
|---|---|
| `pair_z` | how many standard deviations `a` is from its fitted value — the pairs-trading signal |
| `spread` | the same gap in % (log units × 100) |
| `beta` | the hedge ratio: `a`'s % move per 1% of `b` |
| `corr` | correlation of the two's one-candle returns over `n` |

Both sides must be positive (prices). A candle where either is missing
restarts the window.

### 4.4 Transforms (`xf`, over `xn` candles, 1-500)

| xf | meaning |
|---|---|
| `angle` | degrees of the line's rise over xn candles against ATR(14): one ATR per candle = 45°, so the number means the same on any symbol |
| `slope` | average change per candle over xn candles |
| `change` / `pct_change` | change / % change over xn candles |
| `highest` / `lowest` / `sma` | highest / lowest / average of the last xn values |
| `zscore` | standard deviations from its xn-candle average |
| `abs` | without its sign |
| `rank` | 0-100: where the value sits between its lowest (0) and highest (100) of the last xn — **IV rank** = VIX on `tf: 1440` with `rank` over ~200 (live trading keeps about a year of daily candles, so stay at or below ~200 daily bars) |
| `percentile` | % of the previous xn values that were below it — **IV percentile** |

### 4.5 Useful condition patterns

* Time window: `{"left": {"ind": "TIME", "field": "hhmm"}, "op": ">=", "right": {"value": 920}}`
  (TIME is the candle's START time in IST, HHMM).
* Expiry day only: `{"left": {"ind": "DTE"}, "op": "<", "right": {"value": 1}}`.
* VIX filter: `{"left": {"ind": "VIX"}, "op": ">", "right": {"value": 14}}`.
* IV rank above 50: `{"left": {"ind": "VIX", "tf": 1440, "xf": "rank", "xn": 200}, "op": ">", "right": {"value": 50}}`.
* Candle pattern: `{"left": {"ind": "CANDLE", "field": "bullish_engulfing"}, "op": "==", "right": {"value": 1}}`.
* Daily trend filter: `{"left": {"ind": "PRICE"}, "op": ">", "right": {"ind": "EMA", "params": {"period": 20}, "tf": 1440}}`.
* Gap up: `{"left": {"ind": "DAILY", "field": "gap_pct"}, "op": ">", "right": {"value": 0.8}}`.
* Index trend filter on a stock: `{"left": {"ind": "PRICE", "symbol": "NSE:NIFTY50-INDEX"}, "op": ">", "right": {"ind": "EMA", "params": {"period": 50}, "symbol": "NSE:NIFTY50-INDEX"}}`.
* Relative strength vs NIFTY (the stock / index ratio, 2 standard deviations above its
  50-candle average): `{"left": {"math": "/", "a": {"ind": "PRICE"}, "b": {"ind": "PRICE", "symbol": "NSE:NIFTY50-INDEX"}, "xf": "zscore", "xn": 50}, "op": ">", "right": {"value": 2}}`.
  The same with a peer stock as `b` is a pairs spread.
* Steep cross: EMA 9 crosses above EMA 21 **and** `{"left": {"ind": "EMA", "params": {"period": 9}, "xf": "angle", "xn": 3}, "op": "between", "right": {"value": 30}, "right2": {"value": 45}}`.
* **Candle body** — `abs(close − open)`, built from two PRICE operands:
  `{"math": "-", "a": {"ind": "PRICE", "params": {"source": "close"}}, "b": {"ind": "PRICE", "params": {"source": "open"}}, "xf": "abs"}`.
  The **previous candle's** body is the same with `"offset": 1` on both `a`
  and `b`. Body smaller than the previous candle's:

  ```jsonc
  {"left":  {"math": "-", "a": {"ind": "PRICE", "params": {"source": "close"}}, "b": {"ind": "PRICE", "params": {"source": "open"}}, "xf": "abs"},
   "op": "<",
   "right": {"math": "-", "a": {"ind": "PRICE", "params": {"source": "close"}, "offset": 1}, "b": {"ind": "PRICE", "params": {"source": "open"}, "offset": 1}, "xf": "abs"}}
  ```

  Don't use `PRICE` `source: "range"` for this: `range` is **high − low**
  (the whole candle, wicks included), not the body.
* Bullish / bearish candle: `{"left": {"ind": "PRICE", "params": {"source": "close"}}, "op": ">", "right": {"ind": "PRICE", "params": {"source": "open"}}}` (`<` for bearish).
* Body at least half the candle (a strong candle, small wicks): left = the
  body above, `op` `>`, right = `{"math": "*", "a": {"ind": "PRICE", "params": {"source": "range"}}, "b": {"value": 0.5}}`.
* Upper wick on a bullish candle is `high − close` (bearish: `high − open`):
  `{"math": "-", "a": {"ind": "PRICE", "params": {"source": "high"}}, "b": {"ind": "PRICE", "params": {"source": "close"}}}`.
  There is no `max` combiner, so pair it with the bullish-candle condition
  (lower wick: `open − low` bullish, `close − low` bearish).

---

## 5. What to trade — `instrument`

* `"type": "equity"` — the symbol itself (stocks / index in cash).
* `"type": "future"` — the symbol's monthly future (`expiry` current / next).
  Backtests trade the underlying's price (basis ignored).
* `"type": "option"` — option legs. `legs_long` open on a LONG signal,
  `legs_short` on a SHORT signal. All legs of a position enter and exit
  together unless a leg has its own stop / target.

### 5.1 A leg

```jsonc
{"right": "CE", "action": "SELL", "strike": "OTM", "steps": 2, "lots": 1,
 "premium": 100, "delta": 0.2, "expiry": "next", "sl_pct": 30, "tg_pct": 80}
```

| key | values |
|---|---|
| `right` | `CE` call, `PE` put, `FUT` the underlying's future in the same position (protective put, covered call, collar) |
| `action` | `BUY` or `SELL` |
| `strike` | `ATM`; `ITM` / `OTM` with `steps` = how many strike intervals in / out of the money; `PREMIUM` = the strike whose premium is closest to `premium` ₹; `DELTA` = the strike whose \|delta\| is closest to `delta` (0.01-0.99; 0.5 ≈ ATM, 0.2 typical short strangle, 0.05 far wing) |
| `lots` | lots per set; `0` switches the leg off (the optimiser can sweep a hedge on / off) |
| `expiry` | optional: this leg's own expiry `current` / `next` (calendars, diagonals); default = the instrument's |
| `sl_pct` | optional: this leg's own stop, % of its entry premium (a SELL leg exits when its premium rises 30%; a BUY leg when it falls 30%) |
| `tg_pct` | optional: this leg's own target, % of its entry premium (SELL leg: premium falls that much; BUY leg: rises) |
| `reentries` | optional, 1-5: after this leg's own **stop** (`LEG_SL` / `LEG_COST`), open the same leg again (strike re-picked at the new spot, its own fresh stop) on the next candle close — while the position is still open and before `session.end` |
| `roll` | optional: `{"above": 0.45, "to": 0.25, "max": 2}` — on a candle close, when this open leg's \|delta\| is above `above`, close it and open the same leg at the strike nearest \|delta\| `to`; at most `max` (1-5) rolls per position, before `session.end` |

Other instrument keys:

* `expiry` `current` / `next`, `expiry_kind` `weekly` / `monthly` (only
  NIFTY and SENSEX list weeklies; others always monthly).
* `levels_on` — where `stop_loss` / `target` / `trailing` / `breakeven` /
  `scale_out` are measured: `instrument` (the option premium / futures price)
  or `underlying` (spot). **Multi-leg positions must use `underlying`** (or
  set those levels to null and use MTM, leg stops or position rules).
* `iv` — backtest option pricing: `auto` (India VIX for indices, 20-day
  realised vol for stocks), `vix`, `hv`, `fixed` with `value` (IV %), or
  `surface`: on each candle, the smile of the latest chain snapshot recorded
  at or before it (each leg at its own strike and expiry; Data → Research
  recording must have been on), `auto` on candles no snapshot covers. Check
  it beats flat IV first (Research → IV surface vs flat IV).
  Optional `put_skew` / `call_skew` (0-10): extra IV points per 1% out of the
  money, so wings price richer like the real smile (`put_skew: 0.8` = a put 2%
  below spot at IV + 1.6). Live trading uses the real chain, so it ignores them.
* `on_leg_stop` — what happens when a leg's own **stop** hits:
  `leg` (default — only that leg exits), `cost` (every other leg's stop moves
  to its entry price), `all` (exit every leg). Leg targets only close their leg.

Per-leg stops, `reentries`, `roll` and the `hedge` rule can't be combined with
`scale_out`. Re-entries need the leg's own `sl_pct` (or `on_leg_stop: cost`) and
can't follow `on_leg_stop: all` (nothing is left open). Adjusted positions need
`levels_on: underlying` (or no stop / target / trail / breakeven). In a
backtest each added leg must fit the free capital, else it is skipped and
counted (`reentry_no_capital`, `roll_no_capital`, `hedge_no_capital`). MTM %
stops / targets and `pnl_pct` stay measured against the ORIGINAL legs' premium.
New legs show `via` `REENTRY` / `ROLL` / `HEDGE` in the trade's legs; a rolled
leg exits with reason `LEG_ROLL`.

### 5.2 Margin — `margin` (optional)

```jsonc
"margin": {"model": "span", "scan_pct": null, "exposure_pct": null}
```

Decides how much capital a futures / written-option position blocks (and so
how many lots the backtest and the runner can afford):

* absent / `leverage` — contract value ÷ `portfolio.leverage`.
* `span` — an exchange-style **SPAN + exposure** estimate: the worst loss
  over ±`scan_pct`% price moves (default 6% index, 13% stock) and ±4 vol
  points, plus `exposure_pct`% (default 2% index, 3.5% stock) of contract
  value on every short option / future, plus 2% more on expiry day for
  index shorts. Bought wings cut the worst loss (hedge benefit). Bought
  options always need their full premium. **Use this for option selling** —
  at leverage 1 a written NIFTY option would block its whole contract value.

---

## 6. Exits and risk

### 6.1 Price levels

`stop_loss`, `target`, `trailing`, `breakeven` are `null` (off) or:

```jsonc
{"type": "pct", "value": 1.0, "atr_period": 14, "activate": 0}
```

* `type`: `pct` (% of the entry price), `points`, `atr` (× ATR of
  `atr_period`; on an option premium = the underlying's ATR × \|delta\|),
  and for `target` only `rr` (× the stop distance — needs a stop).
* `trailing`: follows the best price by `value`; `activate` = how far in
  profit (same units) before it starts.
* `breakeven`: once the trade is `value` in profit, the stop moves to entry.

### 6.2 Scale-out (partial exits)

```jsonc
"scale_out": [{"type": "rr", "value": 1, "qty_pct": 50}, {"type": "rr", "value": 2, "qty_pct": 25}]
```

At each level close `qty_pct`% of the ENTRY size (whole lots / shares only).
Up to 5 steps, total ≤ 100%. The rest keeps its stop / trail / target.

### 6.3 Position MTM (₹ on the whole position)

```jsonc
"mtm": {"stop": 3000, "target": 4000, "trail_start": 2000, "trail_gap": 1000,
        "stop_pct": 100, "target_pct": 50}
```

* `stop` / `target` — exit when the position's P&L reaches −₹ / +₹.
* `trail_start` + `trail_gap` — once P&L reached `trail_start`, exit if it
  gives back `trail_gap` from its peak (both needed).
* `stop_pct` / `target_pct` (options) — the same as a % of the **net premium
  at entry**: `target_pct: 50` = keep half the credit; `stop_pct: 100` = exit
  when the loss equals the credit. With both ₹ and %, the tighter applies.

### 6.4 Position rules on Greeks and premium (options)

```jsonc
"position_rules": {
  "entry": [{"metric": "abs_delta", "op": "<=", "value": 0.1},
            {"metric": "credit", "op": ">=", "value": 2000}],
  "exit":  [{"metric": "abs_delta", "op": ">", "value": 0.35},
            {"metric": "dte", "op": "<", "value": 0.2}]
}
```

* **entry** rules are checked on the exact legs about to be opened — the
  trade is skipped unless **every** rule holds.
* **exit** rules are checked on each candle's close — the position exits
  when **any** rule holds (reason `RULE_<METRIC>`).
* **hedge** (optional) `{"above": 0.6, "max": 10}` — on each candle's close,
  when the net `delta` per set is beyond ±`above`, add a future of
  −round(total delta in lots) lots (e.g. −0.6 per set × 2 sets = −1.2 → BUY 1
  lot) to bring it back toward 0; skipped when that rounds to 0. At most `max`
  hedges per position; runs until square-off.
* `op`: `>` `<` `>=` `<=`. Up to 10 rules each. Values are **per set** (one
  set = the legs at their configured lots):

| metric | meaning |
|---|---|
| `delta` | net delta in lots of the underlying (1.0 = like holding 1 lot of the future; a 0.5-delta call = 0.5) |
| `abs_delta` | \|delta\| — the usual "is it still neutral?" check |
| `gamma` | how much `delta` changes for a 1% move of the underlying |
| `theta` | ₹ the position earns (+) / loses (−) per day from time decay |
| `vega` | ₹ per 1-point rise in implied volatility |
| `credit` | net premium in ₹ (received; a debit is negative) — at entry: what the legs collect |
| `pnl_pct` | P&L as % of the entry premium (50 = half the credit kept) |
| `dte` | days to the nearest open leg's expiry |
| `iv` | average implied volatility of the open legs, % |

Backtests compute these with Black-Scholes at the backtest IV; live trading
uses implied volatility from the real option prices.

### 6.5 Per day, per symbol, time

* `daily.max_loss` / `daily.max_profit` (₹, realised + open): hit one and
  everything is flattened, no more entries that day.
* `max_trades_per_day` — entries per symbol per day (re-entries after a stop
  happen automatically while the entry condition stays true).
* `cooldown_bars` — candles to wait after an exit before re-entering.
* `max_bars` — time stop: exit after this many candles in the trade.
* `guard` (optional, live only) — `{"max_drawdown": 15000, "max_rejects": 3,
  "max_slippage_pct": 0.3, "on_breach": "pause"}`: once a minute the runner
  checks the automation's drawdown (peak of its closed P&L to now, open
  positions marked to market), entries rejected today and the median slippage
  of its last 20 live fills (needs 10+). A breach switches it OFF — audited,
  with a risk-halt alert; `pause` keeps managing its open positions,
  `flatten` closes them (reason `GUARD`). Switching it back on is yours;
  the guard then counts only from that pause on (old losses, rejects and
  fills don't trip it again a minute later). Backtests ignore it. All three
  limits are optional; set the ones you want. Sensible start: max drawdown
  ≈ 3× the worst day in the backtest, `max_rejects` 3, `max_slippage_pct`
  ≈ 2-3× the backtest's `costs.slippage_pct`.

### 6.6 Session

```jsonc
"session": {"start": "09:20", "end": "15:00", "square_off": "15:15", "days": [2, 3, 4]}
```

* `start` / `end` — entries only between these (IST). `null` = from the open /
  until square-off.
* `square_off` — everything is closed at this time; `null` = carry positions
  overnight (exit rules still apply; a futures / options position is closed at
  15:15 on its nearest leg's expiry day, `EXPIRY`). Live square-off must be ≤ 15:15.
* Must satisfy 09:15 ≤ start ≤ end ≤ square_off ≤ 15:29.
* `days` — entries only on these weekdays, 1 = Mon … 5 = Fri (absent = every day).

### 6.7 Entry order

`entry_order.type`: `market` (next candle's open), `pullback` (wait for a
price `offset_pct`% better than the signal close — limit style), `breakout`
(wait for `offset_pct`% beyond it in the trade's direction — stop style).
`valid_bars` — cancel if not filled within that many candles after the signal
(1 = only the next candle; live arms the order for that long).

`entry_order.limit` (optional, LIVE orders only) — `{"wait_s": 10, "steps": 3}`:
each live entry leg goes in as a LIMIT at the quote, is re-priced to the
latest price `steps` times over `wait_s` seconds (2-30), then turns MARKET for
whatever is left — the spread saved when it fills, never a missed entry. Paper
trades and backtests fill as before (`costs.slippage_pct`). Any order above the
exchange's freeze quantity (index options) goes out in whole-lot slices.

---

### 6.8 Pair leg — a position on two symbols (backtest only)

`"pair_leg": {"symbol": "NSE:NIFTYBANK-INDEX", "kind": "FUT", "ratio": {"type": "beta", "n": 100}}`
adds a second leg on another symbol to every position, opened at the same
candle's open and closed with it: a long signal buys the strategy symbol and
sells the pair symbol, a short does the reverse.

| key | meaning |
|---|---|
| `symbol` | the other symbol (not one the strategy trades) |
| `kind` | `FUT` its near-month future (`expiry` `current` / `next`), rounded to whole lots — under one lot the trade is skipped; `EQ` shares |
| `ratio` | ₹ of the pair leg per ₹1 of the position: `{"type": "beta", "n": 100}` the rolling hedge ratio of log prices over `n` candles at the signal candle, or `{"type": "fixed", "value": 1.0}` |

The position exits on its spread, never on one leg's price: no stop loss,
target, trailing, breakeven or scale-out (the validator rejects them); use
exit conditions, `max_bars`, and the MTM ₹ stop / target / trail, which are
checked on each candle's close (two symbols' candles can't say which leg's
high or low came first) and filled at the next open. Entries are at market.
Each trade lists both legs (each with its `symbol`) and `hedge`: the ratio
asked for and the one whole lots made of it. A strategy with a pair leg
can't be switched on yet — the live half comes later.

## 7. Sizing, portfolio and costs

* `sizing.mode` and `sizing.value`:
  * `qty` — shares (equity) · `lots` — lots (F&O; `qty` becomes `lots` for F&O)
  * `amount` — ₹ of margin per trade · `pct_equity` — % of equity per trade
  * `risk` — ₹ lost if the stop hits · `risk_pct` — % of equity lost if the
    stop hits (both need a `stop_loss`; options: a single leg with levels on
    the premium)
* `portfolio`: `capital` (₹), `leverage` (margin multiplier for equity /
  futures / written options under the leverage model; bought options always
  pay full premium), `max_positions` (open at once), `compounding` (sizes
  follow the equity curve), `max_position_pct` (no position may block more
  than this % of equity).
* `rank` (optional) — when several symbols' entries fire on the same candle,
  only the best `top` of them enter, best first (so capital and
  `max_positions` go to the strongest, not to whichever is listed first):
  `{"by": operand, "order": "desc", "top": 3}`. `by` is any indicator or
  combined value (momentum `ROC`, a z-score, strength vs NIFTY…); `desc` = the
  highest first, `asc` = the lowest. Shorts are ranked the other way round
  (with `desc`, the lowest values get the short entries). Ranked among the
  symbols whose entry conditions are true on that candle, ones already in a
  position included. Time candles only.
* `costs`: `slippage_pct` on every fill, `charges` true = Indian charges.

---

### 7.1 Point-in-time universe — `universe` (backtests)

`"universe": {"index": "nifty50"}` (also `nifty100`, `niftynext50`,
`niftybank`, `nifty500`, `midcap150`, `smallcap250`) backtests the index **as
it was each day**: every stock that was a member at any point of the range is
loaded — the ones dropped since included — and a stock can enter only on days
it was a member (ranking never picks an outsider). Today's list alone has
survivorship bias: it never holds the losers that were dropped, and holds
the winners from before they joined. Membership comes from the lists recorded
so far and the imported change log (Research → universes); the backtest
notes say from when it holds and which past members have no candles. Shares /
futures strategies only. Live trading uses `symbols` — keep them today's
members.

## 8. Backtest, optimise, automate

* **Backtest** — pick a date range (up to ~3 years), optionally hold out the
  last X% as **out-of-sample**. Results: net P&L, return, CAGR, win rate,
  profit factor, expectancy, max drawdown, Sharpe, Sortino, Calmar, t-stat,
  Kelly, SQN, PSR, VaR / CVaR, Monte Carlo (resampled paths), equity curve
  and drawdown, capital in use, P&L distribution, P&L by hour / weekday /
  month, MAE vs MFE, per-symbol table, exit reasons, every trade (CSV) with
  each leg's fill and, for options, the Greeks at entry.
* **Payoff & funds** (option builder) — payoff at expiry / today / halfway,
  breakevens, max profit / loss, chance of profit, every Greek (delta, gamma,
  theta, vega, rho, vanna, charm, vomma, speed), funds required (SPAN vs
  leverage), whether the entry rules pass at today's prices.
* **Optimise** — sweep any number in the spec (an indicator period, a stop,
  a leg's delta or lots …) over a grid (≤ 400 combinations), ranked by
  net P&L, profit factor, Sharpe, Sortino, win rate, return %, expectancy,
  Calmar or t-stat, with a minimum trade count, an out-of-sample split or a
  rolling walk-forward, a heatmap of two parameters and overfitting
  statistics (Deflated Sharpe, PBO).
* **Research check** (Optimise → "research check") — one run puts the idea
  (its rules as they are, or a grid on them) through a held-out split, a
  rolling walk-forward, the Deflated Sharpe after every try on the idea and
  PBO, and judges it against the bar: Deflated Sharpe ≥ 95%, the
  walk-forward's unseen windows and the held-out period both net positive
  on at least the minimum trades, PBO ≤ 50% when a grid was swept. It
  passes or is **killed**, and is saved as a strategy card (Research tab,
  downloadable as one markdown page). Write an idea that can pass it.
* **Pair finder** (Research tab) — up to 30 symbols against an anchor
  (stock vs index) or against each other (peers), on the timeframe the
  strategy will trade: hedge ratio, Engle-Granger cointegration on the
  first two thirds of the history, the same fit re-tested on the last third
  it never saw, half-life and today's z. Only pairs that hold on the last
  third load into the builder, as the one-leg strategy of 9.13, starting at
  as many tries as pairs were scanned.
* **IV surface vs flat IV** (Research tab) — prices every recorded option
  quote with the surface fitted a snapshot earlier and with flat IV, and
  says which priced closer (mean error, share inside the bid-ask, days won
  with a bootstrap CI). Needs 10+ recorded days to say yes.
* **Tick replay of exits** (Research tab) — the builder's backtest, then each
  fixed stop / target exit replayed on the recorded trade prints: which
  level price really touched first, and the P&L that moves.
* **Universes** (Research tab) — each index's recorded lists and change log
  (inclusions / exclusions by effective date, from NSE's announcements),
  and from when its point-in-time membership holds.
* **Nightly re-validation** (Research tab, off until switched on) — every
  weekday evening each switched-on strategy gets its research check again on
  the newest data, plus a look at its trades since its passing card; it is
  flagged when it no longer clears the bar, has lost money since its card,
  or trades LIVE without one. Flags stay open until a person clears them.
* **Compare** — saved strategies side by side on the same range.
* **Automations** — saved strategies start OFF and in **paper** mode; switch
  them on to paper-trade live, or to **LIVE** (real orders on Fyers) after
  typing `LIVE` to confirm. Every save is a new version; any version can be
  made active again.
* **Data → Research recording** (both off until switched on) — Fyers keeps
  no tick or option-chain history, so anything a future backtest needs has
  to be recorded now:
  * **Option chains** — for each named underlying (and, by default, every
    switched-on strategy's F&O underlying): the underlying, its current
    future and ATM ± N strikes of the first N expiries. Every contract's
    standing bid / ask / sizes / last / OI / volume is written on a fixed
    grid (60 s by default, 09:15–15:30, the 15:30 close always), with the
    quote's own exchange time. A snapshot at T only holds quotes from before
    T. `GET /api/algo/ticks/chain?name=NIFTY&at=<epoch>` reads one back.
  * **Keep a short list longer** — the list's symbols (and switched-on
    strategies', and their chains) are marked in each day as it is recorded.
    When a day leaves the window it is cut down to those keys instead of
    deleted: flow, footprint, prints and chains kept 365 days, every frame
    120 days (plus the near-the-money chain contracts', ATM ± N).
  * The **disk line** projects the steady-state size of all windows from
    the real size of the stored days, against the free space and the
    disk guard's floor, and says whether it fits.

### 8.1 Running automations — the live desk (Automations tab)

What happens after a strategy passes its research check. Every panel below
is on the **Automations** tab; the ₹ limits are in **Settings → Capital &
risk** (0 = off for each).

**Lifecycle — the order to do it in**

1. **Backtest → research check.** Only a strategy that passes (a card in the
   Research tab) should go further. Set `costs.slippage_pct` realistically
   (options 0.3-1%), not the 0.02% default.
2. **Save, switch on in paper.** Paper fills at the live quote — no real
   orders. Leave it running for at least 2-4 weeks.
3. **Watch drift and fills** (below) every few days. A strategy whose paper
   trades don't match its backtest is not ready, however good the backtest.
4. **Add a `guard`** (6.5) before LIVE.
5. **LIVE** — type `LIVE` to confirm; start at 1 lot. Fyers must be logged in
   that morning (the token expires daily) or entries are blocked with no
   live price.
6. **Change it through a shadow test**, never by editing the live version.

**Versions and shadow tests.** Every save is a new version; the trade log
records which version took each trade. On a saved strategy, open a version
and press **shadow-test vN**: that version runs as its own automation, in
**paper**, beside the active one, on the same live market. Its row shows
"shadow of …" with a **compare** (closed trades and net P&L of each since the
shadow started). **promote** makes the shadow's rules the parent's active
version (re-using an identical existing version, else a new one) and stops
the shadow; the parent's open positions keep the levels they were opened
with. A shadow can't have its own shadow, can't be LIVE, and is left out of
the book, the margin cap and the allocation. Promoting into a LIVE parent is
refused when its square-off is after the live limit (15:15).

**Live vs backtest drift** (Drift panel, last 1-60 days). Each version that
traded is backtested over exactly the days it traded — same engine, real
candles — and every live / paper trade is put beside the backtest's,
matched by symbol, side and an entry within one candle:

| you see | it means |
|---|---|
| matched, same reason, small gap | the backtest describes this strategy — good |
| matched, `entry_gap_pct` > 0 | live filled worse than the backtest (equity / futures; + = cost you) — raise `slippage_pct` |
| matched, different exit reason | intrabar order (stop vs target in one candle) or a quote the candles never showed |
| **live only** | a manual action, or the backtest's data differs from the live candles |
| **backtest only** (a miss) | the runner was off, no live price (Fyers token), a rejected order, a guard / book stop |

Option positions get no entry gap % (premium and spot can't be compared on
one scale) — use Fills vs quotes for them.

**Fills vs quotes** (TCA, last N days, LIVE orders only). For every live fill:
the price you got against the quote the runner decided at, per strategy and
instrument kind, signed so **+ = it cost you**, next to the `slippage_pct`
that strategy's backtests assume. Measured above assumed = the backtest
flatters the strategy; raise `costs.slippage_pct` to the measured value and
backtest again. Paper fills are the quote, so they show nothing here.

**The book** (Book panel). Every automation's open positions as one book,
per underlying: positions, **net delta** (in lots; shares for cash), margin
blocked and MTM — plus the book's day P&L (realised today + open MTM). A
delta that can't be priced shows `—`, never a guess. Shadows are left out.

| setting | what it does |
|---|---|
| `ALGO_BOOK_MAX_DAY_LOSS` ₹ | the whole book's day P&L at or below −₹X — or its realised loss alone — flattens **every** automation (reason `BOOK_MAX_LOSS`) and stops all automation entries **until tomorrow**. The halt is written to the audit log, so a restart doesn't undo it. |
| `ALGO_BOOK_MAX_MARGIN` ₹ | an entry is sized down (whole lots / sets) or skipped so the margin all automations block together stays ≤ ₹X. Per-strategy `portfolio.capital` still applies on top. |
| **Kill switch** (button) | flattens every automation's open position now (reason `BOOK_KILL`) and stops all automation entries until tomorrow. The news bot and the Trade page are untouched. |

`daily.max_loss` in a spec stops that one strategy; the book limits stop all
of them. Use both: the spec's for each idea, the book's for the account.

**Capital allocation** (`ALGO_ALLOC_CAPITAL` ₹, 0 = off). Off, each
automation sizes on its own `portfolio.capital`. On, ₹X is split once a day
across the switched-on, non-shadow automations by **inverse volatility** of
their daily net P&L over `ALGO_ALLOC_LOOKBACK_DAYS` (default 60 weekdays): a
steadier strategy gets more. One with fewer than 10 trading days borrows the
others' median volatility; one losing money over `ALGO_ALLOC_DECAY_DAYS`
(default 20) gets **half** its weight. Weights sum to 1; the split is worked
out again when the setting or the set of switched-on automations changes
(one switched on mid-day gets its share). The Allocation table shows each
weight, capital, daily vol and recent net. Wait until 3+ strategies have a
live track record before switching it on — with one or two it only adds
noise.

**Nightly re-validation flags** (Research tab, 8) don't switch anything off:
read the flag, then pause the strategy yourself or shadow-test a fix.

**Going-live checklist**

* research card passed; `slippage_pct` at least what Fills vs quotes measures;
* 2-4 weeks of paper with drift mostly "matched";
* `guard` set; `daily.max_loss` set; `ALGO_BOOK_MAX_DAY_LOSS` set for the account;
* `session.square_off` ≤ 15:15; 1 lot to start;
* Fyers logged in today; no open manual position on the same contract.

### Exit reasons you will see

`SL`, `TARGET`, `TRAIL`, `BREAKEVEN`, `SCALE_OUT_n`, `MTM_SL`, `MTM_TARGET`,
`MTM_TRAIL`, `SIGNAL` (exit conditions), `TIME_STOP` (max_bars),
`RULE_<METRIC>` (position rule), `LEG_SL` / `LEG_TARGET` / `LEG_COST`
(a leg's own stop / target / cost stop), `LEG_ROLL` (a leg rolled out),
`DAILY_MAX_LOSS`, `DAILY_TARGET`, `SQUARE_OFF`, `EOD` (a new day began),
`EXPIRY` (a carried F&O position closed at 15:15 on its contract's expiry day, or
booked at its settlement value if found already expired), `END` (end of the data), and live only `MANUAL`, `STALE_SESSION`,
`CLOSED_EXTERNAL`, `GUARD` (its guard flattened it), `BOOK_MAX_LOSS` (the
book's day loss limit), `BOOK_KILL` (the kill switch).

**A backtest where every exit is `SQUARE_OFF`** usually means the stop and
target can never be reached — e.g. `levels_on: "underlying"` with option-sized
percentages (15% of NIFTY is ~3,700 points). Use small % for spot, or
`levels_on: "instrument"` for % of the premium.

### Things the validator rejects (fix these first)

* no symbol, or an entry side with no conditions;
* an unknown indicator, field, operator or transform, or a param out of range;
* a condition timeframe that is not daily or a multiple of the strategy's;
* multi-leg options with price levels on `instrument`;
* `rr` targets, risk sizing or R scale-outs without a stop loss;
* a session outside 09:15-15:29 or in the wrong order;
* scale-out steps above 100% or more than 5, or combined with leg stops;
* `stop_pct` / `target_pct` / `position_rules` without an option instrument;
* MTM trailing with only one of `trail_start` / `trail_gap`.

---

## 9. Worked examples (each one imports as-is)

### 9.1 Equity — EMA 9/21 cross above VWAP, ATR stop, 2R target, 0.5% risk

```json
{
  "name": "EMA 9/21 cross with VWAP, 2R",
  "spec": {
    "symbols": ["NSE:SBIN-EQ", "NSE:RELIANCE-EQ"],
    "timeframe": 5,
    "direction": "both",
    "entry_long": {"logic": "AND", "conditions": [
      {"left": {"ind": "EMA", "params": {"period": 9}}, "op": "crosses_above", "right": {"ind": "EMA", "params": {"period": 21}}},
      {"left": {"ind": "PRICE"}, "op": ">", "right": {"ind": "VWAP"}}
    ]},
    "entry_short": {"logic": "AND", "conditions": [
      {"left": {"ind": "EMA", "params": {"period": 9}}, "op": "crosses_below", "right": {"ind": "EMA", "params": {"period": 21}}},
      {"left": {"ind": "PRICE"}, "op": "<", "right": {"ind": "VWAP"}}
    ]},
    "stop_loss": {"type": "atr", "value": 1.5, "atr_period": 14},
    "target": {"type": "rr", "value": 2},
    "breakeven": {"type": "atr", "value": 1, "atr_period": 14},
    "sizing": {"mode": "risk_pct", "value": 0.5},
    "portfolio": {"capital": 200000, "leverage": 5, "max_positions": 2, "compounding": true},
    "daily": {"max_loss": 3000}
  }
}
```

### 9.2 NIFTY futures — 15-minute opening-range breakout with a daily trend filter

```json
{
  "name": "NIFTY ORB 15 with daily trend",
  "spec": {
    "symbols": ["NSE:NIFTY50-INDEX"],
    "timeframe": 5,
    "direction": "both",
    "instrument": {"type": "future", "expiry": "current", "levels_on": "instrument"},
    "session": {"start": "09:30", "end": "11:30", "square_off": "15:15"},
    "entry_long": {"logic": "AND", "conditions": [
      {"left": {"ind": "PRICE"}, "op": "crosses_above", "right": {"ind": "ORB", "params": {"minutes": 15}, "field": "high"}},
      {"left": {"ind": "PRICE"}, "op": ">", "right": {"ind": "EMA", "params": {"period": 20}, "tf": 1440}}
    ]},
    "entry_short": {"logic": "AND", "conditions": [
      {"left": {"ind": "PRICE"}, "op": "crosses_below", "right": {"ind": "ORB", "params": {"minutes": 15}, "field": "low"}},
      {"left": {"ind": "PRICE"}, "op": "<", "right": {"ind": "EMA", "params": {"period": 20}, "tf": 1440}}
    ]},
    "stop_loss": {"type": "pct", "value": 0.4},
    "target": {"type": "rr", "value": 2},
    "trailing": {"type": "pct", "value": 0.3, "activate": 0.4},
    "max_trades_per_day": 1,
    "sizing": {"mode": "lots", "value": 1},
    "margin": {"model": "span"},
    "portfolio": {"capital": 300000, "max_positions": 1}
  }
}
```

### 9.3 Option buying — Supertrend flip buys an ATM call / put, stops on the premium

```json
{
  "name": "NIFTY Supertrend option buyer",
  "spec": {
    "symbols": ["NSE:NIFTY50-INDEX"],
    "timeframe": 5,
    "direction": "both",
    "instrument": {"type": "option", "expiry": "current", "expiry_kind": "weekly", "levels_on": "instrument",
                   "legs_long": [{"right": "CE", "action": "BUY", "strike": "ATM", "lots": 1}],
                   "legs_short": [{"right": "PE", "action": "BUY", "strike": "ATM", "lots": 1}]},
    "entry_long": {"logic": "AND", "conditions": [
      {"left": {"ind": "SUPERTREND", "params": {"period": 10, "multiplier": 3}, "field": "direction"}, "op": "crosses_above", "right": {"value": 0}}
    ]},
    "entry_short": {"logic": "AND", "conditions": [
      {"left": {"ind": "SUPERTREND", "params": {"period": 10, "multiplier": 3}, "field": "direction"}, "op": "crosses_below", "right": {"value": 0}}
    ]},
    "stop_loss": {"type": "pct", "value": 30},
    "target": {"type": "pct", "value": 60},
    "trailing": {"type": "pct", "value": 20, "activate": 30},
    "sizing": {"mode": "lots", "value": 1},
    "daily": {"max_loss": 5000}
  }
}
```

### 9.4 Option selling — 20-delta short strangle, 30% stop per leg, move the other to cost

```json
{
  "name": "20-delta strangle, leg SL 30%, cost on hit",
  "spec": {
    "symbols": ["NSE:NIFTY50-INDEX"],
    "timeframe": 5,
    "direction": "long",
    "session": {"start": "09:30", "end": "09:35", "square_off": "15:15", "days": [2, 3, 4]},
    "entry_long": {"logic": "AND", "conditions": [
      {"left": {"ind": "TIME", "field": "hhmm"}, "op": ">=", "right": {"value": 930}},
      {"left": {"ind": "VIX"}, "op": ">", "right": {"value": 11}}
    ]},
    "instrument": {"type": "option", "expiry": "current", "expiry_kind": "weekly", "levels_on": "underlying",
                   "on_leg_stop": "cost",
                   "legs_long": [{"right": "CE", "action": "SELL", "strike": "DELTA", "delta": 0.2, "lots": 1, "sl_pct": 30},
                                 {"right": "PE", "action": "SELL", "strike": "DELTA", "delta": 0.2, "lots": 1, "sl_pct": 30}]},
    "stop_loss": null,
    "target": null,
    "mtm": {"target_pct": 50, "stop_pct": 100},
    "max_trades_per_day": 1,
    "margin": {"model": "span"},
    "sizing": {"mode": "lots", "value": 1},
    "portfolio": {"capital": 300000, "max_positions": 1, "compounding": false}
  }
}
```

### 9.5 Iron condor — 16-delta shorts, 5-delta wings, Greek entry filters and a delta exit

```json
{
  "name": "16/5-delta iron condor with Greek rules",
  "spec": {
    "symbols": ["NSE:NIFTY50-INDEX"],
    "timeframe": 15,
    "direction": "long",
    "session": {"start": "09:30", "end": "10:30", "square_off": "15:15"},
    "entry_long": {"logic": "AND", "conditions": [
      {"left": {"ind": "TIME", "field": "hhmm"}, "op": ">=", "right": {"value": 930}},
      {"left": {"ind": "ADX", "field": "adx"}, "op": "<", "right": {"value": 25}}
    ]},
    "instrument": {"type": "option", "expiry": "current", "expiry_kind": "weekly", "levels_on": "underlying",
                   "legs_long": [{"right": "CE", "action": "SELL", "strike": "DELTA", "delta": 0.16, "lots": 1},
                                 {"right": "PE", "action": "SELL", "strike": "DELTA", "delta": 0.16, "lots": 1},
                                 {"right": "CE", "action": "BUY", "strike": "DELTA", "delta": 0.05, "lots": 1},
                                 {"right": "PE", "action": "BUY", "strike": "DELTA", "delta": 0.05, "lots": 1}]},
    "stop_loss": null,
    "target": null,
    "position_rules": {
      "entry": [{"metric": "abs_delta", "op": "<=", "value": 0.1}, {"metric": "credit", "op": ">=", "value": 1000}],
      "exit": [{"metric": "abs_delta", "op": ">", "value": 0.35}, {"metric": "pnl_pct", "op": ">=", "value": 60}]
    },
    "mtm": {"stop_pct": 150},
    "max_trades_per_day": 1,
    "margin": {"model": "span"},
    "sizing": {"mode": "pct_equity", "value": 80},
    "portfolio": {"capital": 300000, "max_positions": 1}
  }
}
```

### 9.6 IV rank — sell a straddle only when VIX is in the top 40% of its year

```json
{
  "name": "Straddle when IV rank > 60",
  "spec": {
    "symbols": ["NSE:NIFTY50-INDEX"],
    "timeframe": 5,
    "direction": "long",
    "session": {"start": "09:20", "end": "09:25", "square_off": "15:15"},
    "entry_long": {"logic": "AND", "conditions": [
      {"left": {"ind": "TIME", "field": "hhmm"}, "op": ">=", "right": {"value": 920}},
      {"left": {"ind": "VIX", "tf": 1440, "xf": "rank", "xn": 200}, "op": ">", "right": {"value": 60}}
    ]},
    "instrument": {"type": "option", "expiry": "current", "expiry_kind": "weekly", "levels_on": "underlying",
                   "legs_long": [{"right": "CE", "action": "SELL", "strike": "ATM", "lots": 1, "sl_pct": 35},
                                 {"right": "PE", "action": "SELL", "strike": "ATM", "lots": 1, "sl_pct": 35}]},
    "stop_loss": null,
    "target": null,
    "mtm": {"stop": 4000, "trail_start": 2500, "trail_gap": 1200},
    "max_trades_per_day": 1,
    "margin": {"model": "span"},
    "sizing": {"mode": "lots", "value": 1},
    "portfolio": {"capital": 300000, "max_positions": 1, "compounding": false}
  }
}
```

### 9.7 Calendar spread — sell this week's ATM call, buy next week's, close before expiry

```json
{
  "name": "ATM call calendar",
  "spec": {
    "symbols": ["NSE:NIFTY50-INDEX"],
    "timeframe": 15,
    "direction": "long",
    "session": {"start": "09:30", "end": "10:00", "square_off": null},
    "entry_long": {"logic": "AND", "conditions": [
      {"left": {"ind": "DTE"}, "op": ">", "right": {"value": 3}},
      {"left": {"ind": "TIME", "field": "hhmm"}, "op": ">=", "right": {"value": 930}}
    ]},
    "exit_long": {"logic": "OR", "conditions": [
      {"left": {"ind": "DTE"}, "op": "<", "right": {"value": 0.3}}
    ]},
    "instrument": {"type": "option", "expiry": "current", "expiry_kind": "weekly", "levels_on": "underlying",
                   "legs_long": [{"right": "CE", "action": "SELL", "strike": "ATM", "lots": 1},
                                 {"right": "CE", "action": "BUY", "strike": "ATM", "lots": 1, "expiry": "next"}]},
    "stop_loss": {"type": "pct", "value": 2},
    "target": null,
    "mtm": {"target_pct": 40},
    "max_trades_per_day": 1,
    "margin": {"model": "span"},
    "sizing": {"mode": "lots", "value": 1},
    "portfolio": {"capital": 200000, "max_positions": 1}
  }
}
```

### 9.8 Multi-timeframe pullback — daily uptrend, 15-minute RSI hook, scale out half at 1R

```json
{
  "name": "Daily trend, 15m RSI pullback, scale out",
  "spec": {
    "symbols": ["NSE:HDFCBANK-EQ", "NSE:ICICIBANK-EQ", "NSE:INFY-EQ"],
    "timeframe": 15,
    "direction": "long",
    "entry_long": {"logic": "AND", "conditions": [
      {"left": {"ind": "PRICE"}, "op": ">", "right": {"ind": "EMA", "params": {"period": 20}, "tf": 1440}},
      {"left": {"ind": "RSI", "params": {"period": 14}}, "op": "crosses_above", "right": {"value": 35}},
      {"left": {"ind": "RVOL", "params": {"days": 10}}, "op": ">", "right": {"value": 1}}
    ]},
    "stop_loss": {"type": "atr", "value": 1.5, "atr_period": 14},
    "target": {"type": "rr", "value": 3},
    "scale_out": [{"type": "rr", "value": 1, "qty_pct": 50}],
    "breakeven": {"type": "atr", "value": 1.5, "atr_period": 14},
    "sizing": {"mode": "risk_pct", "value": 0.75},
    "portfolio": {"capital": 300000, "leverage": 5, "max_positions": 3, "compounding": true}
  }
}
```

### 9.9 Mean reversion — below the lower Bollinger band 2 candles in a row, buy on a pullback

```json
{
  "name": "Bollinger snap-back",
  "spec": {
    "symbols": ["NSE:TCS-EQ"],
    "timeframe": 5,
    "direction": "long",
    "entry_long": {"logic": "AND", "conditions": [
      {"left": {"ind": "BBANDS", "params": {"period": 20, "multiplier": 2}, "field": "percent_b"}, "op": "<", "right": {"value": 0}, "for_bars": 2},
      {"left": {"ind": "RSI"}, "op": "<", "right": {"value": 30}},
      {"left": {"ind": "ADX", "field": "adx"}, "op": "<", "right": {"value": 20}}
    ]},
    "exit_long": {"logic": "OR", "conditions": [
      {"left": {"ind": "BBANDS", "field": "percent_b"}, "op": ">", "right": {"value": 0.5}}
    ]},
    "entry_order": {"type": "pullback", "offset_pct": 0.1, "valid_bars": 3},
    "stop_loss": {"type": "atr", "value": 2, "atr_period": 14},
    "target": null,
    "max_bars": 24,
    "sizing": {"mode": "pct_equity", "value": 50},
    "portfolio": {"capital": 100000, "leverage": 5, "max_positions": 1}
  }
}
```

### 9.10 Combined values — a steep EMA cross that has really separated

```json
{
  "name": "Steep EMA cross with separation",
  "spec": {
    "symbols": ["NSE:NIFTYBANK-INDEX"],
    "timeframe": 5,
    "direction": "long",
    "instrument": {"type": "future", "expiry": "current", "levels_on": "instrument"},
    "entry_long": {"logic": "AND", "conditions": [
      {"left": {"ind": "EMA", "params": {"period": 9}}, "op": "crosses_above", "right": {"ind": "EMA", "params": {"period": 21}}, "within_bars": 3},
      {"left": {"ind": "EMA", "params": {"period": 9}, "xf": "angle", "xn": 3}, "op": "between", "right": {"value": 20}, "right2": {"value": 60}},
      {"left": {"math": "pct_diff", "a": {"ind": "EMA", "params": {"period": 9}}, "b": {"ind": "EMA", "params": {"period": 21}}}, "op": ">", "right": {"value": 0.03}}
    ]},
    "stop_loss": {"type": "atr", "value": 1.5, "atr_period": 14},
    "target": {"type": "rr", "value": 2},
    "max_trades_per_day": 2,
    "margin": {"model": "span"},
    "sizing": {"mode": "lots", "value": 1},
    "portfolio": {"capital": 300000, "max_positions": 1}
  }
}
```

### 9.11 Relative strength — the bank stock pulling ahead of NIFTY while NIFTY trends up, strongest one only

```json
{
  "name": "Relative strength vs NIFTY in an index uptrend",
  "spec": {
    "symbols": ["NSE:HDFCBANK-EQ", "NSE:ICICIBANK-EQ", "NSE:AXISBANK-EQ"],
    "timeframe": 15,
    "direction": "long",
    "entry_long": {"logic": "AND", "conditions": [
      {"left": {"ind": "PRICE", "symbol": "NSE:NIFTY50-INDEX"}, "op": ">", "right": {"ind": "EMA", "params": {"period": 20}, "symbol": "NSE:NIFTY50-INDEX"}},
      {"left": {"math": "/", "a": {"ind": "PRICE"}, "b": {"ind": "PRICE", "symbol": "NSE:NIFTY50-INDEX"}, "xf": "zscore", "xn": 20},
       "op": "crosses_above", "right": {"value": 1.5}}
    ]},
    "rank": {"by": {"math": "/", "a": {"ind": "PRICE"}, "b": {"ind": "PRICE", "symbol": "NSE:NIFTY50-INDEX"}, "xf": "zscore", "xn": 20},
             "order": "desc", "top": 1},
    "stop_loss": {"type": "atr", "value": 1.5, "atr_period": 14},
    "target": {"type": "rr", "value": 2},
    "sizing": {"mode": "risk_pct", "value": 0.5},
    "portfolio": {"capital": 300000, "leverage": 5, "max_positions": 2, "compounding": true}
  }
}
```

### 9.12 Adjusted short strangle — 20-delta legs re-sold after a stop, rolled when tested, delta-hedged with the future

```json
{
  "name": "Adjusted 20-delta strangle",
  "spec": {
    "symbols": ["NSE:NIFTY50-INDEX"],
    "timeframe": 5,
    "direction": "long",
    "session": {"start": "09:30", "end": "14:30", "square_off": "15:15"},
    "entry_long": {"logic": "AND", "conditions": [
      {"left": {"ind": "TIME", "field": "hhmm"}, "op": ">=", "right": {"value": 930}}
    ]},
    "instrument": {"type": "option", "expiry": "current", "expiry_kind": "weekly", "levels_on": "underlying",
                   "iv": {"source": "auto", "value": 15, "put_skew": 0.8, "call_skew": 0.2},
                   "legs_long": [{"right": "CE", "action": "SELL", "strike": "DELTA", "delta": 0.2, "lots": 1,
                                  "sl_pct": 40, "reentries": 1, "roll": {"above": 0.4, "to": 0.2, "max": 1}},
                                 {"right": "PE", "action": "SELL", "strike": "DELTA", "delta": 0.2, "lots": 1,
                                  "sl_pct": 40, "reentries": 1, "roll": {"above": 0.4, "to": 0.2, "max": 1}}]},
    "stop_loss": null,
    "target": null,
    "position_rules": {"hedge": {"above": 0.6, "max": 4}},
    "mtm": {"stop_pct": 150, "target_pct": 50},
    "max_trades_per_day": 1,
    "margin": {"model": "span"},
    "sizing": {"mode": "lots", "value": 1},
    "portfolio": {"capital": 400000, "max_positions": 1, "compounding": false}
  }
}
```

### 9.13 Pairs — HDFC Bank against Bank NIFTY, one leg, back to the fitted spread

Trade only the stock; the index is the signal. Long when HDFC Bank falls 2
deviations below where its 40-hour fit on Bank NIFTY puts it, short when it
rises as far above; out once the gap is back inside 0.5, after 40 candles,
or at a 3% stop. Square-off is off: a spread can take days to close.

```json
{
  "name": "Pair HDFCBANK vs NIFTYBANK",
  "spec": {
    "symbols": ["NSE:HDFCBANK-EQ"],
    "timeframe": 60,
    "direction": "both",
    "entry_long": {"logic": "AND", "conditions": [{"left": {"math": "pair_z", "a": {"ind": "PRICE"}, "b": {"ind": "PRICE", "symbol": "NSE:NIFTYBANK-INDEX"}, "n": 40}, "op": "crosses_below", "right": {"value": -2.0}}]},
    "exit_long": {"logic": "AND", "conditions": [{"left": {"math": "pair_z", "a": {"ind": "PRICE"}, "b": {"ind": "PRICE", "symbol": "NSE:NIFTYBANK-INDEX"}, "n": 40}, "op": ">", "right": {"value": -0.5}}]},
    "entry_short": {"logic": "AND", "conditions": [{"left": {"math": "pair_z", "a": {"ind": "PRICE"}, "b": {"ind": "PRICE", "symbol": "NSE:NIFTYBANK-INDEX"}, "n": 40}, "op": "crosses_above", "right": {"value": 2.0}}]},
    "exit_short": {"logic": "AND", "conditions": [{"left": {"math": "pair_z", "a": {"ind": "PRICE"}, "b": {"ind": "PRICE", "symbol": "NSE:NIFTYBANK-INDEX"}, "n": 40}, "op": "<", "right": {"value": 0.5}}]},
    "stop_loss": {"type": "pct", "value": 3.0},
    "target": null,
    "max_bars": 40,
    "session": {"start": "09:15", "end": "15:15", "square_off": null},
    "sizing": {"mode": "pct_equity", "value": 20}
  }
}
```

### 9.14 Pairs, hedged — the same signal, short Bank NIFTY futures against each long

9.13 with a pair leg: every long in HDFC Bank also sells β × its value of
Bank NIFTY futures (and every short buys them), so a market-wide move
washes out of the position and what's left is the spread. Out on the spread
closing, after 40 candles, or at a ₹15,000 loss on the whole position.

```json
{
  "name": "Pair HDFCBANK vs NIFTYBANK, hedged",
  "spec": {
    "symbols": ["NSE:HDFCBANK-EQ"],
    "timeframe": 60,
    "direction": "both",
    "entry_long": {"logic": "AND", "conditions": [{"left": {"math": "pair_z", "a": {"ind": "PRICE"}, "b": {"ind": "PRICE", "symbol": "NSE:NIFTYBANK-INDEX"}, "n": 40}, "op": "crosses_below", "right": {"value": -2.0}}]},
    "exit_long": {"logic": "AND", "conditions": [{"left": {"math": "pair_z", "a": {"ind": "PRICE"}, "b": {"ind": "PRICE", "symbol": "NSE:NIFTYBANK-INDEX"}, "n": 40}, "op": ">", "right": {"value": -0.5}}]},
    "entry_short": {"logic": "AND", "conditions": [{"left": {"math": "pair_z", "a": {"ind": "PRICE"}, "b": {"ind": "PRICE", "symbol": "NSE:NIFTYBANK-INDEX"}, "n": 40}, "op": "crosses_above", "right": {"value": 2.0}}]},
    "exit_short": {"logic": "AND", "conditions": [{"left": {"math": "pair_z", "a": {"ind": "PRICE"}, "b": {"ind": "PRICE", "symbol": "NSE:NIFTYBANK-INDEX"}, "n": 40}, "op": "<", "right": {"value": 0.5}}]},
    "pair_leg": {"symbol": "NSE:NIFTYBANK-INDEX", "kind": "FUT", "ratio": {"type": "beta", "n": 100}},
    "stop_loss": null,
    "target": null,
    "mtm": {"stop": 15000},
    "max_bars": 40,
    "session": {"start": "09:15", "end": "15:15", "square_off": null},
    "sizing": {"mode": "pct_equity", "value": 40},
    "portfolio": {"capital": 1000000, "leverage": 5}
  }
}
```

### 9.15 Point in time — NIFTY 50 as it was, momentum, out of the way of results

Backtested on the NIFTY 50 of each day (past members included, entries only
while a member); live it trades the five listed. Long on a close back above
the 50-candle EMA, but not within 3 days after the stock's results came out
(`FILING` counts a filing only once it was public by the candle's close);
when several fire on one candle, the two with the strongest 20-candle ROC.

```json
{
  "name": "NIFTY 50 momentum, out of the way of results",
  "spec": {
    "symbols": ["NSE:RELIANCE-EQ", "NSE:HDFCBANK-EQ", "NSE:ICICIBANK-EQ", "NSE:INFY-EQ", "NSE:TCS-EQ"],
    "universe": {"index": "nifty50"},
    "timeframe": 15,
    "direction": "long",
    "entry_long": {"logic": "AND", "conditions": [
      {"left": {"ind": "PRICE"}, "op": "crosses_above", "right": {"ind": "EMA", "params": {"period": 50}}},
      {"left": {"ind": "FILING", "params": {"event": "RESULTS", "days": 3}, "field": "count"}, "op": "==", "right": {"value": 0}}
    ]},
    "rank": {"by": {"ind": "ROC", "params": {"period": 20}}, "order": "desc", "top": 2},
    "stop_loss": {"type": "atr", "value": 2, "atr_period": 14},
    "target": {"type": "rr", "value": 2},
    "sizing": {"mode": "pct_equity", "value": 20},
    "portfolio": {"capital": 500000, "max_positions": 4}
  }
}
```

### 9.16 EMA 5 / 24 cross with RSI + ADX, small-body candle — buy a call or a put

Buy side only: an ATM call when EMA 5 crosses above EMA 24 with RSI above 55
and ADX above 25, on a candle whose body is smaller than the previous
candle's (a quiet cross, not a spike); an ATM put on the mirror image (cross
below, RSI below 45). Stop / target on NIFTY spot (0.3% / 0.6%), so they can
actually be reached; 0.5% slippage because it trades options; a guard for
when it goes live.

```json
{
  "name": "EMA 5/24 RSI+ADX small body - buy CE or PE",
  "spec": {
    "symbols": ["NSE:NIFTY50-INDEX"],
    "timeframe": 5,
    "direction": "both",
    "entry_long": {"logic": "AND", "conditions": [
      {"left": {"ind": "EMA", "params": {"period": 5}}, "op": "crosses_above", "right": {"ind": "EMA", "params": {"period": 24}}},
      {"left": {"ind": "RSI", "params": {"period": 14}}, "op": ">", "right": {"value": 55}},
      {"left": {"ind": "ADX", "params": {"period": 14}, "field": "adx"}, "op": ">", "right": {"value": 25}},
      {"left":  {"math": "-", "a": {"ind": "PRICE", "params": {"source": "close"}}, "b": {"ind": "PRICE", "params": {"source": "open"}}, "xf": "abs"},
       "op": "<",
       "right": {"math": "-", "a": {"ind": "PRICE", "params": {"source": "close"}, "offset": 1}, "b": {"ind": "PRICE", "params": {"source": "open"}, "offset": 1}, "xf": "abs"}}
    ]},
    "entry_short": {"logic": "AND", "conditions": [
      {"left": {"ind": "EMA", "params": {"period": 5}}, "op": "crosses_below", "right": {"ind": "EMA", "params": {"period": 24}}},
      {"left": {"ind": "RSI", "params": {"period": 14}}, "op": "<", "right": {"value": 45}},
      {"left": {"ind": "ADX", "params": {"period": 14}, "field": "adx"}, "op": ">", "right": {"value": 25}},
      {"left":  {"math": "-", "a": {"ind": "PRICE", "params": {"source": "close"}}, "b": {"ind": "PRICE", "params": {"source": "open"}}, "xf": "abs"},
       "op": "<",
       "right": {"math": "-", "a": {"ind": "PRICE", "params": {"source": "close"}, "offset": 1}, "b": {"ind": "PRICE", "params": {"source": "open"}, "offset": 1}, "xf": "abs"}}
    ]},
    "instrument": {"type": "option", "expiry": "current", "expiry_kind": "weekly", "levels_on": "underlying",
                   "legs_long":  [{"right": "CE", "action": "BUY", "strike": "ATM", "lots": 1}],
                   "legs_short": [{"right": "PE", "action": "BUY", "strike": "ATM", "lots": 1}]},
    "stop_loss": {"type": "pct", "value": 0.3},
    "target": {"type": "pct", "value": 0.6},
    "daily": {"max_loss": 5000},
    "session": {"start": "09:20", "end": "13:30", "square_off": "15:13"},
    "max_trades_per_day": 3,
    "cooldown_bars": 2,
    "sizing": {"mode": "lots", "value": 1},
    "portfolio": {"capital": 150000, "max_positions": 1, "compounding": false},
    "costs": {"slippage_pct": 0.5, "charges": true},
    "guard": {"max_drawdown": 15000, "max_rejects": 3, "max_slippage_pct": 1.5, "on_breach": "pause"}
  }
}
```

Note the put side: `direction` "short" only names which signal fired — the
leg is still a BUY (of a put). This is a template, not a proven edge: on
NIFTY 5-minute, Jan-Jul 2026, both sides together made 30 trades, 30% won,
net −₹7,000 (profit factor 0.85); the call side alone (`"direction": "long"`)
made 20 trades, net +₹5,000 — too few trades to mean anything. Put it through
the research check before you paper-trade it.

---

## 10. The Builder, field by field

Every input on the **Builder** tab, top to bottom, with the JSON key it
writes (so a JSON from an AI and the screen always mean the same thing),
its allowed values and its default. "empty / off" = the box left blank.

### 10.0 From a basic strategy to a complex one — the order to build in

Add one layer at a time and backtest after each; keep a layer only if it
helps out of sample.

1. **Basic** — one symbol, one entry condition, a stop and a target.
   *EMA 9 crosses above EMA 21 → buy shares, 1% stop, 2R target.* (9.1)
2. **Filters** — add AND conditions: a trend filter on a higher timeframe
   (`tf: 1440`), a time window (`TIME`), RSI / ADX strength, a candle-body
   check (4.5). (9.2, 9.8, 9.16)
3. **Exits** — exit conditions, a trailing stop, move-stop-to-cost, a time
   stop, scale-out of part of the position. (9.8, 9.9)
4. **Risk & sizing** — risk-% sizing from the stop, daily max loss, max
   trades per day, cooldown, the session window and entry days.
5. **Many symbols** — a list of symbols sharing one capital pool,
   `max_positions`, `rank` to take only the strongest, `universe` for an
   honest backtest. (9.11, 9.15)
6. **Derivatives** — futures, or option legs (buy a call / put, spreads,
   straddles, condors), levels on premium or spot, MTM ₹ stops, SPAN margin.
   (9.3-9.7)
7. **Option management** — per-leg stops / targets, `on_leg_stop`,
   re-entries, rolls, delta-hedging, Greek / premium position rules. (9.12)
8. **Cross-symbol & pairs** — operands on another symbol, spreads and
   z-scores, a hedged pair leg. (9.10, 9.13, 9.14)
9. **Proof** — research check, optimise with walk-forward, Monte Carlo,
   then paper, drift, guard, LIVE (8, 8.1).

### 10.1 Strategy (top card)

| field | JSON | values / default | notes |
|---|---|---|---|
| **⬇ export** | — | — | downloads `{name, spec}` as `<name>.json` |
| **⬆ import** | — | a `.json` file | checked by the validator; an error says which value is wrong |
| **📋 paste JSON** | — | text | paste what an AI wrote (a json code block or text around it is fine). **Import** checks it; on an error, **load anyway** opens it as written so you can fix it in the builder; **cancel** closes the box. Paste the error back to the AI to fix it |
| **name** | `name` | text | the strategy's name; saved strategies need a unique one |
| **template** | — | 39 ready-made strategies | loads one into the builder (asks before replacing unsaved work). Equity: EMA cross, VWAP + RSI, opening range, Bollinger reversion, gap-up, Turtle Donchian, squeeze, Ichimoku, Heikin-Ashi + ADX, linear-regression slope, PSAR, NR7, z-score, gap fade, Stoch-RSI pullback, steep cross, held spread, scale-out. Options: Supertrend CE / PE buy, 9:20 straddle, expiry-day strangle, 20Δ strangle, 16Δ condor, per-leg-stop strangle, 9:30 condor, credit spreads, long straddle on a squeeze, delta-surge buy, CPR stock options. Futures: BANKNIFTY EMA + trend, BANKNIFTY ORB. Order flow: delta-confirmed cross, CVD divergence, absorption, initiative pressure, value-area rejection, 80% rule, RVOL breakout, absorption at the extreme |
| **candles** | `bars.type`, `timeframe`, `bars.per_day` | `time` + 1m … daily (default 5m) · `volume` / `turnover ₹` + ~N per day (5-1000, default 50) | time = a candle every N minutes; volume / turnover = a candle closes once that much has traded (sized so a typical day — the median of the 20 before the start — has about N) |
| **direction** | `direction` | long / bullish only · short / bearish only · both | which signal(s) are used; for options it only names the signal, the legs decide buy / sell |
| **symbols** | `symbols` | 1-50 | search a stock or index; **+ NIFTY / BANKNIFTY / FINNIFTY / MIDCPNIFTY / SENSEX** quick-adds; paste a comma / space separated list to add many; Backspace in the empty box removes the last; **clear** removes all; **×** on a chip removes one |
| **versions** v1 v2 … | — | saved strategies only | click to load that version (✓ = the one Automations runs); **make vN active** switches Automations to it; **shadow-test vN** runs it in paper beside the active one (8.1) |

Unsaved work is kept in this browser as a draft and comes back on reload.

### 10.2 What to trade

| field | JSON | values / default | notes |
|---|---|---|---|
| **Equity / index (cash)** · **Futures** · **Options** | `instrument.type` | equity · future · option | the header shows the symbol's F&O lot and whether it has weekly expiries. Switching sets sizing to shares (equity) or lots (F&O) |
| **expiry** | `instrument.expiry` | current · next | F&O only |
| expiry kind | `instrument.expiry_kind` | weekly (NIFTY, SENSEX) · monthly | options only; stocks and most indices are monthly |
| **levels on** | `instrument.levels_on` | the option premium / the futures price (`instrument`) · the underlying spot (`underlying`) | where stop / target / trailing / breakeven are measured. Multi-leg positions must use the underlying (the premium choice is disabled) |
| **▸ presets (19)** | `legs_long` / `legs_short` | leg setups | buy ATM CE / PE, buy 1-OTM, sell ATM (writer), bull call / bear put spread, short straddle, short strangle (3 OTM), iron fly, 20Δ strangle, credit spreads, long straddle / strangle, 16Δ / 5Δ iron condor, 1×2 ratio, butterfly, calendar, jade lizard, iron condor (2 / 4 OTM), protective put / call, covered call / put, collar. Multi-leg presets switch levels to the underlying |
| **On a LONG / bullish signal** / **On a SHORT / bearish signal** | `legs_long` / `legs_short` | list of legs | **+ leg** adds BUY CE ATM 1 lot; ✕ removes one |

**One leg (a row):**

| field | JSON | values / default | notes |
|---|---|---|---|
| BUY / SELL | `action` | BUY · SELL | |
| CE / PE / FUT | `right` | CE (call) · PE (put) · FUT (future) | a FUT leg + option legs = protective put, covered call, collar |
| strike | `strike`, `steps` | ATM · ITM 1-20 · OTM 1-20 · closest to premium ₹ · closest to delta Δ | counted in the underlying's strike steps (ITM 2 = two strikes in the money) |
| ₹ | `premium` | default 100 | with "closest to premium": the strike whose premium is nearest ₹X |
| Δ | `delta` | 0.01-0.99, default 0.3 | with "closest to delta": 0.5 ≈ ATM, 0.2 a typical short-strangle strike, 0.05 a far wing |
| expiry | `expiry` | strategy expiry (absent) · current · next | a leg's own expiry: sell this week + buy next week = a calendar |
| **lots** | `lots` | 0+, default 1 | per set; **0 = leg off** (the optimiser can sweep a hedge on / off) |
| **SL %** | `sl_pct` | empty = off | this leg's own stop: its premium moves this % against it (up for a SELL, down for a BUY) |
| **TG %** | `tg_pct` | empty = off | this leg's own target; closes only this leg |
| **re-enter ×** | `reentries` | 0-5, empty = 0 | after this leg's own stop, open it again (strike re-picked at the new spot) on the next candle close, up to N times per position. Needs `sl_pct` (or `on_leg_stop: cost`) |
| **roll \|Δ\| >** … **→** … | `roll: {above, to, max}` | above between 0 and 1, to below it (default ≤ 0.2), max 1 | when the leg's \|delta\| goes above `above` on a candle close, close it and open the same leg at the strike nearest `to` (once per position) |

**Below the legs:**

| field | JSON | values / default | notes |
|---|---|---|---|
| **when a leg's stop hits** | `instrument.on_leg_stop` | only that leg exits (`leg`, default) · move the other legs' stops to cost (`cost`) · exit every leg (`all`) | shown once any leg has an SL %; can't be combined with scale-out |
| **backtest IV** | `instrument.iv.source`, `.value` | auto · India VIX · 20-day realised vol · fixed % (value, default 15) · recorded IV surface | backtests only; live uses real premiums. auto = VIX for indices, realised vol for stocks |
| **put skew** / **call skew** | `instrument.iv.put_skew` / `call_skew` | 0-10, empty = flat IV | extra IV points per 1% out of the money (NIFTY puts ≈ 0.5-1) |
| **margin** | `margin.model` | contract value ÷ leverage (absent, default) · SPAN + exposure (`span`) | what a futures / written-option position blocks — decides how many sets fit |
| **price scan %** | `margin.scan_pct` | empty = 6% index, 13% stock | SPAN only: the worst-case price move scanned |
| **exposure %** | `margin.exposure_pct` | empty = 2% index, 3.5% stock | SPAN only: on short options and futures |
| **pair leg** (equity / futures only) | `pair_leg` | off · symbol, as its future (whole lots) or shares, ratio β over N bars (10-500, default 100) or fixed × (default 1) | 6.8. Turning it on clears price stops / targets / trailing / breakeven / scale-out and sets the entry to market |

### 10.3 Payoff & funds required (options and futures)

A live what-if of the legs, before any backtest.

| item | meaning |
|---|---|
| **spot** / **IV %** boxes | what-if: price the position at another spot / implied volatility (empty = today's spot, the backtest's IV source) |
| chart | P&L at expiry, today and halfway, against spot; the shaded band is the expected move; the dashed line is spot |
| **Max profit / Max loss** | at the first expiry over any price; "unlimited" = a naked short call (or short future) with no cap |
| **Breakevens** | spots where the expiry P&L crosses zero |
| **Chance of profit** | probability the expiry P&L is above 0 (lognormal at this IV) — selling wins often but small, buying rarely but big |
| **Return on funds** | max profit ÷ funds required |
| **Expected move ±1σ** | one standard deviation of the underlying to the first expiry at this IV |
| **Δ delta** | ₹ gained per 1-point rise of the underlying (whole position) |
| **Γ gamma** | how fast delta changes per point; negative = a writer, moves hurt more the further they go |
| **Θ theta / day** | ₹ earned (+) or bled (−) per day from time decay alone |
| **ν vega / vol pt** | ₹ per 1-point rise in IV |
| **ρ rho / 1% rate** | ₹ per 1% rise in interest rates |
| **vanna** | change of delta for a 1-point IV rise — why a short strangle's delta drifts when volatility jumps |
| **charm / day** | change of delta per day with price unchanged — delta decay into expiry |
| **vomma** | change of vega (₹) for a 1-point IV rise |
| **speed** | change of gamma per point — how fast gamma risk grows as price moves |
| **Funds required** | SPAN est. or contract value ÷ leverage; the bar splits it into **SPAN** (worst-scenario loss), **exposure** and **premium paid**; **Each leg on its own** = no offsets; **hedge benefit** = what offsetting legs save |
| **use the SPAN estimate** | switches `margin.model` to `span` |
| **Sets from capital** | how many sets `portfolio.capital` can hold at once (0 = it can't afford one) |
| **per set: delta / gamma (lots)** | the position's Greeks per set, in lots of the underlying |
| **Entry rules at this spot** | whether each `position_rules.entry` rule passes right now |
| legs table | per leg: side, qty, price, Δ per unit, pos Δ (₹ per point, whole leg), θ / day, vega, **P(ITM)** (risk-neutral chance it expires in the money), **λ** (% the premium moves for a 1% move of the underlying), SL / TG as premium prices, margin **if bought** (its premium) and **if sold** (SPAN + exposure) |

### 10.4 Entry & exit conditions

Conditions are checked on each **completed** candle of the underlying.

| field | JSON | values | notes |
|---|---|---|---|
| **LONG / bullish signal when** | `entry_long` | a group | the long entry |
| **Exit long when** · **+ add exit conditions** / **remove** | `exit_long` | a group or null | extra exits; without them, exits are stop / target / trailing / MTM / square-off only |
| **SHORT / bearish signal when**, **Exit short when** | `entry_short`, `exit_short` | the same for shorts | |
| **ALL of (AND)** / **ANY of (OR)** | `logic` | AND · OR | how this group's lines combine |
| **+ condition** | `conditions[]` | adds `PRICE > EMA(20)` | |
| **+ group** | a nested `{logic, conditions}` | up to 2 levels deep | adds an OR group, e.g. *(RSI > 60 OR ADX > 25)* inside an AND |
| ✕ | — | — | removes that line or group; an empty group never fires |

**A condition (a line):** `left` · operator · `right` (· `right2`).

| part | JSON | values | notes |
|---|---|---|---|
| left operand | `left` | any indicator, or **ƒ Combine two values** | never a plain number |
| operator | `op` | crosses above · crosses below · crosses · `>` `<` `>=` `<=` `==` · is rising for (bars) · is falling for (bars) · is between · is outside | rising / falling take a bar count (≥ 1) on the right; between / outside take a low and a high (`right`, `right2`) |
| right operand | `right` | a **Number** or any indicator / combined value | |
| **⋯** → **held N bars** | `for_bars` | 2+ | true only when it has held N bars in a row |
| **⋯** → **within last N bars** | `within_bars` | 2+ | true if it happened at least once in the last N bars (a cross 3 bars ago still counts) |
| **⋯** → **NOT** | `negate` | true | the condition must be FALSE (shows **NOT** in front) |

**An operand (an indicator box):**

| part | JSON | values | notes |
|---|---|---|---|
| indicator list | `ind` | grouped list (see Reference); **Number** = `{"value": n}` | hover an indicator for what it is |
| output | `field` | e.g. MACD `macd` / `signal` / `hist`, BBANDS `upper` / `middle` / `lower`, ADX `adx` / `plus_di` / `minus_di` | only for indicators with several outputs |
| settings (period, mult, source, event …) | `params` | per indicator (see Reference) | `source` picks the price: close, open, high, low, hl2, hlc3, ohlc4, **range** (= high − low), volume …; `event` for FILING |
| **on** | `tf` | base candle · a higher timeframe that is a multiple of the base · daily | only that timeframe's completed candles are used |
| **of** | `symbol` | empty = this symbol · an index or a stock | compute it on another symbol's candles (not for VIX, DTE, TIME, order flow) |
| **ago** | `offset` | 0+ | 1 = the previous candle's value |
| **as** (transform) | `xf`, `xn` | value · angle ° · slope / bar · change · % change · highest · lowest · average · z-score · absolute · rank 0-100 · percentile, over N bars (default 3) | 4.4. `absolute` takes no N |
| **ƒ** → **×** / **+** | `mult`, `add` | default 1 / 0 | scale or offset: `× 1.02` = 2% above, `+ 20` = 20 points above |

**ƒ Combine two values:** `(A op B)` → `{"math": op, "a": A, "b": B}`.

| op | JSON | meaning |
|---|---|---|
| − + × ÷ | `-` `+` `*` `/` | arithmetic (A − B = a spread, or a candle body with `xf: abs`) |
| % gap | `pct_diff` | how far A is from B, in % of B |
| spread z vs | `pair_z` (+ `n`) | least-squares fit of log A on log B over the last n bars (10-500, default 100): how many deviations A is from its fitted value |
| spread % vs | `spread` (+ `n`) | the same in % |
| hedge ratio β vs | `beta` (+ `n`) | the fitted hedge ratio |
| return corr. with | `corr` (+ `n`) | correlation of their one-bar returns |

A combined value takes its own **as** transform; **✕** turns it back into a
single value. Combinations nest 2 deep. Put another symbol on B for pairs.

### 10.5 Stop loss, target & risk

| field | JSON | values / default | notes |
|---|---|---|---|
| **stop loss** | `stop_loss: {type, value, atr_period}` | off · % of price · points · × ATR (period, default 14) | measured per "levels on". ATR on an option premium = the underlying's ATR × the option's delta |
| **target** | `target` | off · % · points · × ATR · **× risk (R:R)** | `rr` = a multiple of the stop distance (needs a stop) |
| **trailing stop** | `trailing: {…, activate}` | off · % · points · × ATR; **after** = profit before it starts (same unit, 0 = at once) | follows the best price by this gap |
| **move stop to cost after** | `breakeven` | off · % · points · × ATR | once this far in profit, the stop moves to the entry price |
| **Scale out: close X% at V** | `scale_out: [{qty_pct, value, type, atr_period}]` | up to 5 steps; type × risk (R) · % · points · × ATR; **+ step** adds 50% at step-number R | partial exits; each closes X% of the ENTRY size, rounded down to whole shares / lots (under one lot = skipped); the rest keeps its stop / trail / target; each part is its own trade row `SCALE_OUT_n`. Steps can't total over 100%; R steps need a stop; not with per-leg stops |
| **Position MTM: stop ₹** | `mtm.stop` | empty = off | exit when the position's P&L falls to −₹X |
| **target ₹** | `mtm.target` | empty = off | exit at +₹X |
| **trail after ₹** … **by ₹** | `mtm.trail_start`, `mtm.trail_gap` | both or neither | once P&L reaches ₹X, exit if it gives back ₹Y from its peak |
| **stop % of premium** (options) | `mtm.stop_pct` | empty = off | the loss reaches this % of the net premium at entry (100 = lost as much as the credit); with a ₹ stop too, the tighter applies |
| **target % of premium** (options) | `mtm.target_pct` | empty = off | the profit reaches this % of the net premium (50 = keep half the credit) |
| **Option Greeks & premium rules** — Enter only if ALL hold / Exit when ANY holds | `position_rules.entry` / `.exit` `[{metric, op, value}]` | metrics in 6.4 (delta, abs_delta, gamma, theta, vega, credit, pnl_pct, dte, iv …), ops `>` `<` `>=` `<=`; **+ entry rule** / **+ exit rule** | per set; entry rules are checked on the legs about to open, exit rules on each candle close (reason `RULE_<METRIC>`) |
| **delta-hedge with the future when \|Δ\| per set >** X, **at most** N **hedges** | `position_rules.hedge: {above, max}` | above ≥ 0.05 (default 0.6, keep ≥ 0.5), max 1-50 (default 10) | on each close, buy / sell whole future lots per set to bring net delta back toward 0 |
| **Per day: max loss ₹** | `daily.max_loss` | empty = off | realised + open P&L for the day; hit it → flatten, no more entries today (`DAILY_MAX_LOSS`) |
| **max profit ₹** | `daily.max_profit` | empty = off | lock the day in (`DAILY_TARGET`) |
| **max trades / symbol** | `max_trades_per_day` | ≥ 1, default 3 | entries per symbol per day |
| **re-entry cooldown** | `cooldown_bars` | ≥ 0, default 0 | candles to wait after an exit before re-entering the same symbol |
| **time stop** | `max_bars` | empty = off, ≥ 1 | exit a position open this many candles (`TIME_STOP`) |
| **Guard: drawdown ₹ / rejected orders today / median slippage %**, **then keep managing / then flatten** | `guard` | each empty = off; `on_breach` pause · flatten | live only (6.5) |
| **entries from** / **entries until** / **square off** (tick + time) | `session.start` / `end` / `square_off` | 09:15-15:29; unticked = from the open / until square-off / **carry forward overnight** | live square-off ≤ 15:15 |
| **entry days** Mon-Fri | `session.days` | all ticked = every day (absent) | new entries only on ticked days; open positions are still managed and squared off daily; the last day can't be unticked |
| **entry order** | `entry_order.type` | at market · on a pullback · on a breakout | market = next candle's open; pullback = wait for a price X% better (limit-style); breakout = X% further in the trade's direction (stop-style) |
| X **% better than / beyond the signal close** | `entry_order.offset_pct` | default 0.1 | pullback / breakout only |
| **cancel after** N **bars** | `entry_order.valid_bars` | ≥ 1, default 3 | unfilled → counted as `entry_expired` |
| **live: limit at the quote**, **chase** N **times over** S **s, then market** | `entry_order.limit: {steps, wait_s}` | steps 1-10 (default 3), wait 2-30 s (default 10) | live orders only (6.7) |

### 10.6 Sizing & portfolio

| field | JSON | values / default | notes |
|---|---|---|---|
| **quick setup: Conservative** | sizing + portfolio + daily | 0.5% risk per trade (10% of equity without a stop), 3 open, ≤ 25% per position, day stop −1.5%, no compounding | fills the fields below; edit after |
| **Balanced** | | 1% risk (or 20% of equity), 5 open, ≤ 40% per position, day stop −3%, compounding | |
| **Aggressive** | | 2% risk (or 35% of equity), 10 open, ≤ 60% per position, day stop −5%, compounding | |
| **each trade is**: fixed shares · fixed lots · ₹ amount · % of equity · ₹ risk · % risk | `sizing.mode` | qty · lots · amount · pct_equity · risk · risk_pct | risk modes need a stop (greyed out without one); the unit beside the box follows the mode; F&O shows "× lot per lot" |
| value | `sizing.value` | default 1 | shares, lots, ₹ per trade, % of equity, ₹ lost if the stop hits, % of equity lost if the stop hits |
| **capital ₹** | `portfolio.capital` | default 1,00,000 | one pool for every symbol (or the allocation's share, 8.1) |
| **max open positions** | `portfolio.max_positions` | ≥ 1, default 10 | extra signals are skipped (`max_positions`) |
| **max % per position** | `portfolio.max_position_pct` | empty = no cap | no single position may block more than this share of equity |
| **leverage ×** | `portfolio.leverage` | default 1 | intraday margin multiplier: equity MIS ≈ 5, futures / written options ≈ 6-9 (or use SPAN); bought options always pay the full premium |
| **compound profits** | `portfolio.compounding` | default on | sizes follow the equity curve; off = sizes stay on the starting capital |
| **on the same candle, enter only the best** N **by** … **highest / lowest first** | `rank: {top, by, order}` | top 1-50 (default 1), by = any operand (default ROC 20), desc · asc | shown for 2+ symbols; time candles only (7) |
| **backtest on**: the symbols above · NIFTY 50 / 100 / Next 50 / Bank / 500 / Midcap 150 / Smallcap 250 as it was each day | `universe.index` | absent · nifty50 · nifty100 · niftynext50 · niftybank · nifty500 · midcap150 · smallcap250 | equity / futures without a pair leg (7.1) |
| **slippage %** | `costs.slippage_pct` | default 0.02 | on every fill. Realistic: liquid stocks 0.02-0.05, index futures 0.02, options 0.3-1 |
| **Indian charges** | `costs.charges` | default on | brokerage, STT, exchange, SEBI, GST, stamp duty, per segment |
| summary line | — | — | the whole sizing block in one sentence — read it to check |

### 10.7 Run bar

| field | meaning |
|---|---|
| **from** / **to** | the backtest range (up to ~3 years); **30d 90d 180d 1y 2y** set it to end today |
| **out-of-sample %** | hold out the last X% (5-80, 0 = off; a smaller number is rounded up to 5) |
| **▶ Run backtest** | downloads missing candles first, then runs. "The settings changed since the results below were run" = press it again |
| **N tries so far** | backtests + optimiser combinations run on this idea — the Deflated Sharpe gets stricter with every one; kept with the draft and the saved strategy |
| **Save as vN** | saved strategy: keeps every earlier version, the new one becomes active |
| **Save as new strategy** | a new saved strategy (OFF, paper) |

### 10.8 Optimise (below the results)

| field | meaning |
|---|---|
| **+ parameter** | any number in the spec: indicator settings, condition numbers, × multipliers, stops, targets, leg lots / delta, scale-out steps … each with a list of values (≤ 400 combinations in all) |
| **rank by** | net P&L, profit factor, Sharpe, Sortino, win rate, return %, expectancy, Calmar, t-stat |
| **check: one out-of-sample split** | ranks on the earlier part; each row shows how it did on the held-out end (**hold out last X%**, 5-80) |
| **check: rolling walk-forward** | **windows** (2-12 out-of-sample windows), **in-sample ×** (0.5-10 windows of history per window), **anchored** (in-sample always starts at the range start) or rolling; each window is traded with the winner of the in-sample before it |
| **check: research check → strategy card** | 8: pass / killed, saved as a card |
| **min trades** | combinations with fewer trades are not ranked (a 1-trade 100% win rate means nothing) |
| table | params, trades, win %, net, PF, Sharpe, max DD, OOS trades / PF / net; **apply** sends that row's values back to the builder |
| heatmap | two swept parameters against the metric; a lone bright cell = luck, a broad bright area = robust |
| **Overfitting check** line | Deflated Sharpe after N trials (< 95% = the top row may be luck) and PBO (> 50% = the in-sample winner usually loses out of sample) |
| walk-forward result | each window (ranked on → traded unseen, its params, IS metric, OOS trades / PF / net), **Out-of-sample net** (every window's unseen trades stitched), **WF efficiency** (out-of-sample net per unit of time as a % of the in-sample winners' net per unit of time; ≥ 50% good, — when in-sample lost), OOS trades / win rate / profit factor / Sharpe / max DD, the OOS equity curve |

---

## 11. Reading the backtest results

Top to bottom. A number shown green / red is on the good / bad side of its
usual bar.

**Notes and warnings** (above the numbers): `⚠ symbol: …` = data gaps, a
symbol with no candles, a universe member missing, etc. **Order flow: X%
real ticks** = the rest of the order-flow values were estimated per minute.
**Signals not taken: N × reason** —

| reason | means |
|---|---|
| no capital | the position didn't fit the free capital / margin — raise capital or leverage, or size smaller |
| max positions | `max_positions` were already open |
| daily halt | the day's max loss / profit had stopped trading |
| entry expired | a pullback / breakout order not filled within `valid_bars` |
| entry rules | an option `position_rules.entry` rule failed |
| no stop | the stop couldn't be set (an ATR stop before its ATR has enough candles, or a flat ATR of 0) — the strategy never enters without its stop, live included |
| expires today | a carried (square-off off) strategy's entry after 15:15 into a contract expiring that day — it would be closed at once (`EXPIRY`) |
| reentry / roll / hedge no capital | an option adjustment didn't fit (5.1) |

**Performance**

| metric | meaning | rule of thumb |
|---|---|---|
| Net P&L | after charges and slippage | |
| Return | net ÷ starting capital | |
| CAGR | annualised return over the tested range (needs 30+ days) | |
| Win rate | % of trades that made money | means little alone — read it with the payoff ratio |
| Profit factor | gross wins ÷ gross losses | > 1.3 decent, > 1.7 good, < 1 loses |
| Expectancy | average net per trade | must clear costs comfortably |
| Max drawdown | worst peak-to-trough fall of equity (₹ and %) | size capital for ~2× this |
| Sharpe | daily P&L mean ÷ sd over every session of the range (a day without a trade counts as 0), annualised (√252); Sortino, PSR, VaR, skew use the same days | > 1 decent, > 2 strong (intraday) |
| Sortino | Sharpe counting only down days as risk | |
| Calmar | CAGR ÷ max drawdown % | > 1 good |
| Charges | brokerage, STT, exchange, SEBI, GST, stamp, by segment | close to gross P&L = the edge is eaten by costs |
| Gross P&L | before charges (slippage is already in the fills) | |
| Avg win / loss | average winning / losing trade | |
| Median win / loss | the typical win / loss (ignores outliers) | far from the average = a few trades drive the result |
| Largest win / loss | the single best / worst trade | one trade > 30% of net = fragile |
| Best / worst day | the best / worst day's net | |
| Streaks W / L | longest run of wins / losses | plan to survive 2× the losing streak |
| Profitable days | winning days / trading days | |
| Avg bars held | candles per trade | |
| t-stat (mean trade) | mean trade ÷ its standard error | ≥ 2 = unlikely to be luck alone (before counting tries) |
| Payoff ratio | average win ÷ average loss | with win rate W: an edge if W × payoff > 1 − W |
| Kelly % | W − (1 − W) ÷ payoff | ≤ 0 = no edge; trade ¼-½ of it at most |
| Avg R | net per trade in units of the initial stop risk | needs a stop |
| Recovery factor | net ÷ max drawdown | > 3 good |
| Ulcer index | RMS of % drawdown along the curve — depth AND duration of the pain | lower is better |
| Avg MAE / MFE | how far trades went against / for you before the exit, % of entry | MFE far beyond the exits = targets too tight; MAE near the stop = stop too tight |
| Exposure | share of candles with a position open | |
| Avg hold | minutes per trade | |
| Buy & hold | the underlying's own move over the window (average across symbols) | the benchmark to beat |

**Risk & confidence**

| metric | meaning | bar |
|---|---|---|
| PSR | Probabilistic Sharpe: the chance the true Sharpe is above 0, given the number of days and their skew / fat tails | ≥ 95% |
| DSR · N tries | Deflated Sharpe: PSR against the best Sharpe luck alone would give after N backtests / optimiser combinations | ≥ 95% — the real bar |
| SQN | √n × mean ÷ sd of trade P&L (n ≤ 100) | 1.6-2 average, 2-3 good, > 3 excellent |
| VaR 95% (1 day) | a day worse than this happened on 5% of days (needs 20+ days) | |
| CVaR 95% | the average of those worst 5% of days — what a bad day really costs | keep it below your daily max loss |
| Tail ratio | best 5% day ÷ worst 5% day | > 1 = wins bigger than losses at the extremes |
| Skew / ex-kurtosis | the shape of daily P&L: negative skew = rare big losing days; kurtosis > 0 = fatter tails than normal | option selling shows negative skew |

**In-sample vs out-of-sample** (with an OOS %): trades, win rate, net,
profit factor, Sharpe, expectancy, max DD %, t-stat for each part, split at
the date shown. The out-of-sample column is the one that counts; a strategy
that only works in-sample is overfit.

**Monte Carlo** (resampled orderings of the same trades): net P&L bad case
(5%) / median / good case (95%), max drawdown median / bad case (95%),
chance of ending at a loss, chance equity halves at some point. Size capital
for the 95% drawdown, not the one history happened to give.

**Charts and tables**

| panel | read it for |
|---|---|
| Equity curve + drawdown | a steady slope vs a few jumps; long flat / underwater stretches |
| Capital in use | margin blocked over time: **Peak in use** (size the account for this), **Avg per position**, **Return on peak capital** (net ÷ peak margin — how hard the money actually needed worked) |
| P&L distribution | trades per net-P&L bucket: a fat left tail = rare big losses |
| P&L by entry hour (IST) | hours that lose → narrow `session.start` / `end` |
| Excursions (MAE vs MFE) | each trade's worst / best % against its result — tune the stop and target |
| Monthly returns | net ₹ per month (darker = bigger): consistency vs one lucky month |
| underlying chart | the last candles of the chart symbol with ▲▼ entries and ● exits (indicator overlays; order-flow volume coloured by delta) |
| By symbol | trades, win %, net, PF, max DD, buy & hold per symbol — drop symbols that never work |
| By exit reason | count and net per reason (list in 8): mostly `SQUARE_OFF` = the levels are never reached; many `SL` and few `TARGET` = the stop is too tight |
| By month / By weekday | net per month / weekday → `session.days` |
| Trades (newest first) | instrument, signal, qty, entry / exit time and price (the underlying for multi-leg — hover a row for its legs), reason, bars, MAE / MFE %, R, and for options IV %, Δ, Θ / day and credit per set at entry; charges, net. **⬇ trades CSV** downloads every trade |

## 12. The other tabs, in one line each

* **Automations** — every saved strategy: **On** (switch it on / off),
  **Mode** (paper · LIVE, LIVE needs `LIVE` typed and a logged-in Fyers
  account), **Version** (the one that runs — switch any time), symbols,
  timeframe, trades, open / closed, realised ₹; **edit** loads it into the
  builder, **square off** closes its open positions now, **delete** (only
  once it is flat). **Show** ticks filter the tables below to those
  strategies (**show all** clears it). Then **Open positions** (live MTM,
  stop / target / trail, the chart link), **Trades**, **Runner log** (each
  signal, order, fill, reject, guard and book event), and the live-desk
  panels of 8.1 (Book, Allocation, Drift, Fills vs quotes).
* **Compare** — several saved strategies (or the builder draft) run on the
  same range, side by side.
* **Research** — strategy cards, pair finder, IV surface vs flat IV, tick
  replay, universes, nightly re-validation (8).
* **Data** — **Market data**: what the local 1-minute candle store holds per
  symbol (from / to / candles); download N days for symbols from Fyers, or
  sync the last 5 days of every strategy symbol (every timeframe is built
  from these candles). **Research recording** (option chains, short list,
  disk line — 8). **Order-flow collector**: its own Fyers socket that
  classifies every trade print buy / sell for footprint, CVD and the
  order-flow indicators — the symbols on it, days kept, raw frames for
  hand-picked symbols, the disk guard, futures (next month always or only
  in the roll window) and options (strikes per side), and today's frames /
  prints / buy-sell volume per key.
