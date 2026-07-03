# Risk Management Framework

The bot targets explosive intra-day moves on NSE/BSE corporate announcements
— **long on strong positive news, short on strong negative news, always
intraday**. Risk is controlled before entry, during the trade, and across the
whole portfolio.

**Status legend:** ✅ active · 🟡 implemented but inert until a data feed is
wired (fails safe / logs a skip — never a phantom guard) · 📋 planned.

All thresholds live in `Settings` (`app/config.py`) and are tunable via `.env`
or the Settings UI without code changes.

---

## 1. Pre-trade filters — `app/risk/engine.py`, `app/risk/market_clock.py`
| Filter | Status | Notes |
|---|---|---|
| Sentiment-confidence floor (`MIN_SENTIMENT_CONFIDENCE=0.7`) | ✅ | engine R2b blocks below the floor |
| Event-category whitelist | ✅ | default signal rules (`default_rules.py`) |
| Market-hours entry window (excl. first 15 / last 30 min) | ✅ | `market_clock.is_entry_window`, IST. **Enforced in EVERY mode** (intraday-only, no carry-forward): an off-hours entry can't be squared off the same session — the feed is frozen, SL/target can never fire, and the position risks surviving overnight. `ENFORCE_MARKET_HOURS=false` remains the explicit escape hatch for off-hours testing |
| Order-time staleness re-check (`MAX_NEWS_AGE_SECONDS=90`) | ✅ | re-measured at order time, after the LLM call |
| Liquidity / ADV (`MIN_LIQUIDITY_CRORE=5`) | 🟡 | Fyers quote carries no volume yet; `REQUIRE_KNOWN_LIQUIDITY=false` so unknown ADV warns rather than blocks. Flip on once a volume feed exists |
| Bid-ask spread (`MAX_SPREAD_PCT=0.1`) | ✅🟡 | engine R12 blocks a too-wide quoted spread. Fail-safe: when the quote has no bid/ask (today) it SKIPS + warns. Switches on the moment an L1 feed populates them |
| India VIX suspend (`INDIA_VIX_MAX=30`) | ✅ | gate + §2 size throttle read India VIX from the Fyers index quote (`FyersVolatilityRegime`, TTL-cached). Fail-safe: no Fyers account → no VIX → no gate |
| Per-event confidence floor | ✅ | the event matrix raises the conviction bar above the global floor for priced-in events (dividends, splits) — `app/risk/event_profiles.py` |

## 2. Position sizing — `app/risk/position_sizer.py`
- ✅ **Equity ledger** = starting capital + realised P&L + unrealised P&L
  (replaces the old gross-exposure number that shrank as capital deployed).
- ✅ **qty = min(risk-based, notional-cap)** — `MAX_CAPITAL_RISK_PCT=0.75`,
  `MAX_SINGLE_POSITION_PCT=20`.
- ✅ **Graduated risk** — `RISK_RAMP_START_PCT=0.5` for the first
  `RISK_RAMP_TRADES=100` fills, then up to the cap.
- ✅ **High-VIX throttle** — `resolve_risk_pct` scales per-trade risk
  down in an elevated-VIX regime (0.75× / 0.5×) using the Fyers India-VIX
  feed. Sizing only — never touches conviction. Fail-safe: 1.0× (no-op)
  when no VIX is available (no Fyers account).

## 3. Stops, targets & exits — `app/execution/trade_manager.py`
- ✅ **Trailing stop** — arms at `+TRAIL_ACTIVATE_R=1.5`R, trails by
  `TRAIL_DISTANCE_R=0.5`R.
- ✅ **Explicit levels win** — when a position has a target set, that hard
  target + stop govern the exit (a configured target always triggers a FULL
  exit and is never silently overridden). Scale-out / trailing only manage
  a position with **no** explicit target.
- ✅ **Scale-out** (open-ended positions only) — with no hard target, takes
  half at `SCALE_OUT_R=2`R, moves the rest to breakeven and trails it (no
  hard cap, to keep the fat tail). Toggle the half-take with
  `SCALE_OUT_ENABLED`.
- ✅ **Time exit** — `MAX_HOLD_SECONDS=1080` (18 min).
- ✅ **ATR-based initial stop** — `app/risk/volatility.py` sizes the stop
  at `ATR_STOP_MULT`×ATR (clamped to `[DEFAULT_SL_MIN_PCT, ATR_MAX_STOP_PCT]`
  of entry). Volatility-adaptive: wider on fast movers (cuts whipsaw),
  tighter on quiet names (cuts the loss). Live ATR comes from Fyers history
  candles (`FyersCandleVolatilityProvider`, TTL-cached, warmed before
  sizing), wired for every mode; with no connected Fyers account it returns
  nothing and the stop falls back to the percentage stop.
  (`TickWindowVolatilityProvider` is a candle-free estimator available to
  opt into, not the active default.) Because the trailing stop trails by
  R = the initial (ATR) risk, trailing is ATR-aware too.
- ✅ **Event-type matrix** — `app/risk/event_profiles.py` tunes the stop
  multiplier, target R:R, and hold window per event type (order wins run
  wider/longer; priced-in dividends/splits run tighter/shorter). Size is
  deliberately NOT event-scaled — sizing stays risk-math driven.

## 4. Portfolio circuit breakers — `app/risk/circuit_breakers.py`
Persistent state in the `risk_state` table (survives restarts).
| Breaker | Trigger | Action | Status |
|---|---|---|---|
| Daily loss | equity ↓ `DAILY_MAX_LOSS_PCT=2.5%` from day start | flatten + halt for the day | ✅ |
| Weekly loss | `WEEKLY_MAX_LOSS_PCT=5%` peak→trough | throttle risk to `0.25%` | ✅ |
| Monthly drawdown | `MONTHLY_MAX_DRAWDOWN_PCT=8%` from month start | flatten + disable | ✅ |
| Consecutive losers | `MAX_CONSECUTIVE_LOSERS=4` | 45-min pause; resume needs conf ≥ 0.85 | ✅ |
| Max trades/day | `MAX_TRADES_PER_DAY=12` | no new entries | ✅ |
| Max concurrent | `MAX_CONCURRENT_POSITIONS=3` | block new entries | ✅ |
| Sector value cap | `SECTOR_CONCENTRATION_PCT=25%` (was 30) | block (engine R9) | ✅ |
| Sector name cap | `MAX_POSITIONS_PER_SECTOR=2` | block (engine R13) — limits the COUNT of names per sector | ✅ |
| One-name-per-event | `SECTOR_CLUSTER_WINDOW_SECONDS=120` | block a 2nd same-sector entry inside the window — take only the best of a correlated news cluster | ✅ |

## 5. Execution safeguards — `app/execution/manager.py`, `fyers_live.py`
- ✅ **IOC marketable-limit entry** — `ENTRY_BUFFER_PCT=0.2`, immediate-or-
  cancel, always INTRADAY. Bounds entry slippage to ~the buffer.
- ✅ **Slippage recorded** per fill (`trades.slippage_pct`); warns above
  `MAX_SLIPPAGE_PCT=0.25`.
- ✅ **LLM latency cap** — discard the opportunity past
  `LLM_TIMEOUT_SECONDS=18` (`app/analyzer/service.py`).
- ✅ **EOD square-off** — all positions flattened at `SQUARE_OFF_TIME_IST=15:10`.

## 6. Emergency controls — `app/api/risk.py`
- ✅ **Kill switch** — `POST /api/risk/kill` flattens everything + disables
  trading; `POST /api/risk/resume` clears it; `GET /api/risk/state` reports it.
- ✅ Breaker / kill events published on the bus (`risk.kill_switch`,
  `breaker.tripped`) for the notification fan-out.
- 📋 Front-end kill button (API + backend done).

## 7. Continuous improvement
- ✅ Each closed trade logs slippage and **R-multiple** (`trades` table).
- ✅ Graduated per-trade risk for a young account (§2).
- 📋 Backtesting on historical news — needs timestamped news + intraday tick
  data; validate forward in **paper mode** until then.

---

**Honest summary:** the account-protecting layers (sizing, circuit breakers,
kill switch, IOC entry, square-off, staleness) are live, and the **edge**
layers are now too — volatility-aware (ATR) stops, the per-event trade
matrix, the anti-chase entry gate, and the clustering controls (sector
value/name caps + one-name-per-event). The Fyers feeds for **ATR** (history
candles) and **India VIX** (index quote) are wired live behind TTL-cached
providers, and **bid/ask spread** is enforced for symbols the realtime
socket streams. All remain **fail-safe** — with no connected Fyers account
they return no data and the risk layer falls back (% stop, no VIX gate,
spread skipped), never faking a pass. Still pending a feed: real **ADV**
(the Fyers quote carries no average volume), so `REQUIRE_KNOWN_LIQUIDITY`
stays off. Position size stays risk-math driven and decoupled from the LLM's
conviction by design.
