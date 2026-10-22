# Algo Lab guide — everything the Builder & Backtest can do

This guide is written for two readers: you, and an AI assistant you hand it
to. Give it to any AI chat (ChatGPT, Claude, Gemini…), describe the strategy
you want in plain words, and ask for the JSON. Then import that JSON in the
Algo Lab (**⬆ import** for a file, **📋 paste JSON** for text) — the builder
fills in, you backtest, tweak, save and automate.

The last part of this file (*Reference*) is generated from the running app,
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
* **Everything is flat by the square-off time** (unless square-off is
  switched off — then positions carry overnight until an exit rule closes them).
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
`session.days`, `mtm.stop_pct` / `mtm.target_pct`, `instrument.on_leg_stop`
and the per-leg keys `delta`, `expiry`, `sl_pct`, `tg_pct`.

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
  `flatten` closes them (reason `GUARD`). Switching it back on is yours.
  Backtests ignore it.

### 6.6 Session

```jsonc
"session": {"start": "09:20", "end": "15:00", "square_off": "15:15", "days": [2, 3, 4]}
```

* `start` / `end` — entries only between these (IST). `null` = from the open /
  until square-off.
* `square_off` — everything is closed at this time; `null` = carry positions
  overnight (exit rules still apply). Live square-off must be ≤ 15:15.
* Must satisfy 09:15 ≤ start ≤ end ≤ square_off ≤ 15:29.
* `days` — entries only on these weekdays, 1 = Mon … 5 = Fri (absent = every day).

### 6.7 Entry order

`entry_order.type`: `market` (next candle's open), `pullback` (wait for a
price `offset_pct`% better than the signal close — limit style), `breakout`
(wait for `offset_pct`% beyond it in the trade's direction — stop style).
`valid_bars` — cancel if not filled within that many candles.

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

### Exit reasons you will see

`SL`, `TARGET`, `TRAIL`, `BREAKEVEN`, `SCALE_OUT_n`, `MTM_SL`, `MTM_TARGET`,
`MTM_TRAIL`, `SIGNAL` (exit conditions), `TIME_STOP` (max_bars),
`RULE_<METRIC>` (position rule), `LEG_SL` / `LEG_TARGET` / `LEG_COST`
(a leg's own stop / target / cost stop), `DAILY_MAX_LOSS`, `DAILY_TARGET`,
`SQUARE_OFF`, `EOD` (a new day began), `END` (end of the data), and live
only `MANUAL`, `STALE_SESSION`, `CLOSED_EXTERNAL`.

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
