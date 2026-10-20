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
  India VIX (indices), the stock's realised volatility, or a fixed number —
  option backtests are good estimates, not exact fills. Live trading uses the
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
      "iv": {"source": "auto", "value": 15}    // backtest option pricing: auto | vix | hv | fixed (value %)
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

**A combined value:** `{"math": "-", "a": operand, "b": operand}` with
`math` one of `+ - * /` or `pct_diff` (`(a - b) / b × 100`, the % gap).
Combined values take `mult` / `add` / `xf` too and nest 3 deep.

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

Other instrument keys:

* `expiry` `current` / `next`, `expiry_kind` `weekly` / `monthly` (only
  NIFTY and SENSEX list weeklies; others always monthly).
* `levels_on` — where `stop_loss` / `target` / `trailing` / `breakeven` /
  `scale_out` are measured: `instrument` (the option premium / futures price)
  or `underlying` (spot). **Multi-leg positions must use `underlying`** (or
  set those levels to null and use MTM, leg stops or position rules).
* `iv` — backtest option pricing: `auto` (India VIX for indices, 20-day
  realised vol for stocks), `vix`, `hv`, or `fixed` with `value` (IV %).
* `on_leg_stop` — what happens when a leg's own **stop** hits:
  `leg` (default — only that leg exits), `cost` (every other leg's stop moves
  to its entry price), `all` (exit every leg). Leg targets only close their leg.

Per-leg stops can't be combined with `scale_out`.

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

---

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
* `costs`: `slippage_pct` on every fill, `charges` true = Indian charges.

---

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
* **Compare** — saved strategies side by side on the same range.
* **Automations** — saved strategies start OFF and in **paper** mode; switch
  them on to paper-trade live, or to **LIVE** (real orders on Fyers) after
  typing `LIVE` to confirm. Every save is a new version; any version can be
  made active again.

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
