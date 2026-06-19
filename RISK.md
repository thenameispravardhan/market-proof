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
| Market-hours entry window (excl. first 15 / last 30 min) | ✅ | `market_clock.is_entry_window`, IST |
| Order-time staleness re-check (`MAX_NEWS_AGE_SECONDS=90`) | ✅ | re-measured at order time, after the LLM call |
| Liquidity / ADV (`MIN_LIQUIDITY_CRORE=5`) | 🟡 | Fyers quote carries no volume yet; `REQUIRE_KNOWN_LIQUIDITY=false` so unknown ADV warns rather than blocks. Flip on once a volume feed exists |
| Bid-ask spread (`MAX_SPREAD_PCT=0.1`) | 🟡 | needs an L1 (bid/ask) feed — not in the quote today |
| India VIX suspend (`INDIA_VIX_MAX=30`) | 🟡 | needs a VIX feed |

## 2. Position sizing — `app/risk/position_sizer.py`
- ✅ **Equity ledger** = starting capital + realised P&L + unrealised P&L
  (replaces the old gross-exposure number that shrank as capital deployed).
- ✅ **qty = min(risk-based, notional-cap)** — `MAX_CAPITAL_RISK_PCT=0.75`,
  `MAX_SINGLE_POSITION_PCT=20`.
- ✅ **Graduated risk** — `RISK_RAMP_START_PCT=0.5` for the first
  `RISK_RAMP_TRADES=100` fills, then up to the cap.

## 3. Stops, targets & exits — `app/execution/trade_manager.py`
- ✅ **Trailing stop** — arms at `+TRAIL_ACTIVATE_R=1.5`R, trails by
  `TRAIL_DISTANCE_R=0.5`R.
- ✅ **Scale-out** — takes half at `SCALE_OUT_R=2`R, moves the rest to
  breakeven and trails it (no hard cap, to keep the fat tail). Toggle with
  `SCALE_OUT_ENABLED`; off = classic hard target.
- ✅ **Time exit** — `MAX_HOLD_SECONDS=1080` (18 min).
- 🟡 **ATR-based initial stop** — falls back to a percentage stop
  (`DEFAULT_SL_MIN_PCT=1`, 1.5% under ₹200) until intraday candles are wired.

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
kill switch, IOC entry, square-off, staleness) are live. The filters that need
market data the Fyers quote doesn't yet provide (VIX, spread, real ADV, ATR)
are wired as real code paths that **fail safe** — they never fake a pass — and
switch on the moment their feed is added.
