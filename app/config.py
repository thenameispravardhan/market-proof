"""Application configuration loaded from environment / .env file."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed application settings.

    Reads from process env and (if present) a `.env` file in the working
    directory. Sibling tracks should depend on this object — never read
    `os.environ` directly.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------- Trading core ----------
    TRADING_MODE: Literal["paper", "live"] = "paper"

    # ---------- DeepSeek ----------
    DEEPSEEK_API_KEY: str = ""

    # ---------- FYERS ----------
    FYERS_APP_ID: str = ""
    FYERS_SECRET_KEY: str = ""
    FYERS_ACCESS_TOKEN: str = ""
    FYERS_REDIRECT_URI: str = "http://localhost:8000/api/fyers/callback"
    # HMAC-SHA256 secret for inbound Fyers postback webhooks. When
    # `scripts/register_fyers_webhook.py` runs, it picks this up
    # automatically and writes it to the `webhooks.secret` column for
    # the Fyers webhook row. Leave blank to accept unsigned Fyers
    # payloads (NOT recommended — Fyers' signature is your only proof
    # the postback is real).
    FYERS_POSTBACK_SECRET: str = ""

    # ---------- Storage ----------
    # Ignored if TESTING=1 (in-memory sqlite is used).
    DATABASE_URL: str = "sqlite:///./data/trading.db"

    # ---------- Server ----------
    BIND_HOST: str = "127.0.0.1"
    BIND_PORT: int = 8000
    LOG_LEVEL: str = "INFO"
    # How often each monitor re-polls NSE/BSE. Lower = faster detection
    # of a fresh filing, but poll too aggressively and the exchange CDNs
    # start returning 403s / rate-limit your IP (the scraper then falls
    # back to the slower Playwright path or stalls entirely). Accepts
    # fractional seconds (e.g. 2.5). 1s is the floor for a single local
    # operator; raise it if you start seeing `monitor.retry` 403/429 spam.
    POLL_INTERVAL_SECONDS: float = 1.0

    # ---------- News sources (multi-channel detection racing) ----------
    # Each source is an independent monitor; enabled flags are read live
    # every loop iteration, so UI toggles apply within one interval.
    # Detection = min(lag of enabled sources); dedupe collapses repeats.
    NSE_API_ENABLED: bool = True
    BSE_API_ENABLED: bool = True
    # The NSE RSS racer (nsearchives Online_announcements.xml) was
    # measured publishing filings BEFORE the corporate-announcements
    # API. Default OFF: new behavior ships behind a toggle that
    # preserves current behavior until the operator opts in.
    NSE_RSS_ENABLED: bool = False
    # Per-source poll interval for the RSS racer (seconds). The RSS feed
    # is a plain CDN with conditional-GET (304s are ~free), so it can be
    # polled faster than the Akamai-fronted APIs without a ban risk.
    # 0 = follow the global POLL_INTERVAL_SECONDS.
    NSE_RSS_POLL_SECONDS: float = 1.0

    # ---------- Mover model (offline-trained P(mover), scored live) ----
    # The artifact is AIdataset/model/live_model.json; scoring is a dot
    # product in pure Python (app/services/mover_model.py) so nothing new
    # is installed on the server. Two independent switches, on purpose:
    #
    #   MODEL_ENABLED       compute the score and attach it to the decision
    #                       context. Pure telemetry — it can never block.
    #   MODEL_GATE_ENABLED  additionally allow a low score to VETO a trade.
    #
    # Both default OFF, so shipping this changes nothing until the operator
    # opts in (non-destructive evolution, PROJECT.txt §25). The gate is the
    # one that needs the argument: Phase 5 measured the pooled headroom over
    # the base rate at under 1pp, so a hard pre-filter is a real risk of
    # throwing away trades for a model that cannot see much.
    MODEL_ENABLED: bool = False
    MODEL_GATE_ENABLED: bool = False
    # Which trained variant to score with. Empty = the artifact's own
    # default_variant. The Model page lists every key with its holdout AUC.
    MODEL_VARIANT: str = ""
    # A signal scoring below this P(mover) is vetoed when the GATE is on.
    # Calibrate against the percentile table on the Model page, not by feel —
    # the same_session base rate is ~10.7%, so 0.15 is already selective.
    MODEL_MIN_PROBABILITY: float = 0.15
    # Refuse to gate on a score built from too little live data. The hot path
    # cannot always populate price/market-cap, and a 20%-covered score is
    # mostly the training mean wearing a probability. 0 = never abstain.
    MODEL_MIN_COVERAGE: float = 0.5

    # ---------- Host resources (RAM / disk watchdog) ------------------
    # The box is a 2 GB Lightsail instance. Both of its exhaustible
    # resources have already cost a trading day: an OOM kill on
    # 2026-08-15 (uvicorn at 1.5 GB anon-rss) and three SQLite
    # corruptions. These two thresholds drive the Dashboard's Resources
    # section AND the 09:05 preflight alarm, so the warning arrives
    # before the open rather than during it. 0 disables a check.
    RESOURCE_WARN_MEM_PCT: float = 85.0
    RESOURCE_WARN_DISK_PCT: float = 85.0

    # ---------- Risk defaults (per-strategy overrides in DB) ----------
    # Per-trade capital-at-risk cap. RISK.md targets 0.75%; the graduated
    # ramp (RISK_RAMP_*) starts a fresh account lower and works up to this.
    MAX_CAPITAL_RISK_PCT: float = 0.75
    DAILY_MAX_LOSS_PCT: float = 2.5
    # Max simultaneous open positions. RISK.md caps this at 3 for the
    # speed-news strategy (clustering risk on correlated names).
    MAX_CONCURRENT_POSITIONS: int = 3
    MAX_SINGLE_POSITION_PCT: float = 20.0
    MIN_LIQUIDITY_CRORE: float = 5.0
    MAX_SIGNALS_PER_DAY: int = 20

    # ---------- Trading / trade management ----------
    # Paper-trading starting capital; the risk engine sizes positions
    # against this when there are no open positions to mark-to-market.
    PORTFOLIO_VALUE: float = 1_000_000.0
    # Default stop-loss distance (% of entry) when the analysis
    # doesn't carry explicit levels, and the reward:risk multiple
    # used to derive the target from the stop distance.
    #
    # NOTE on the 6% default: position notional as a share of the
    # portfolio works out to (MAX_CAPITAL_RISK_PCT / DEFAULT_SL_PCT).
    # With the 1% risk and 20% single-position defaults, the stop must
    # be >= 5% or *every* trade would be blocked by the notional cap.
    # 6% keeps a high-conviction trade comfortably inside the cap
    # (~17% notional) while leaving headroom. Operators can tighten it.
    DEFAULT_SL_PCT: float = 6.0
    DEFAULT_TARGET_RR: float = 3.0
    # How often the quote feed refreshes watched symbols (seconds).
    QUOTE_REFRESH_SECONDS: int = 5

    # ---------- Speed-trading / freshness / hold rules ----------
    # Maximum age (seconds) of an announcement, measured against its
    # `filed_at`, before the analyzer treats it as STALE and refuses
    # to call the LLM. This is the "no trades on old queued news"
    # rule. The analyzer also pre-marks every announcement older than
    # this on startup so a process restart never replays the backlog.
    # Default 45s gives ~30s of slack inside the 20-second trade
    # budget for downstream latency (LLM + Fyers + fill).
    MAX_NEWS_AGE_SECONDS: int = 45
    # WHICH CLOCK the staleness gate above measures against.
    #
    # False (default, legacy): age = now - filed_at. `filed_at` is the
    #   EXCHANGE's stated filing time, which includes the exchange's own
    #   publish lag — the gap between a company submitting a filing and
    #   NSE/BSE actually exposing it on the API. Measured on live data
    #   that lag is a MEDIAN ~35s (p90 8-16 min), so this clock rejects a
    #   large share of filings for a delay the bot did not cause.
    #
    # True: age = now - received_at (the bot's own reaction time — how
    #   long it has sat on news it can actually see). Alpha decays from
    #   PUBLICATION, not submission: until the exchange publishes it, no
    #   participant could trade it, so the price has not moved yet and
    #   the edge is intact. `received_at` ≈ publication + one poll wait.
    #   Paired with MAX_NEWS_AGE_ABSOLUTE_SECONDS below so genuinely
    #   ancient filings are still rejected.
    NEWS_AGE_FROM_RECEIPT: bool = False
    # Absolute ceiling on `now - filed_at`, applied ONLY when
    # NEWS_AGE_FROM_RECEIPT is on. This is the safety net that the
    # receipt clock alone cannot provide: if the monitor was DOWN for an
    # hour, every backlog row is "received just now" and would look
    # fresh — this ceiling rejects it. Also catches an exchange
    # re-publishing genuinely old filings. 0 = no ceiling (not advised).
    MAX_NEWS_AGE_ABSOLUTE_SECONDS: int = 1800
    # Maximum time (seconds) the trade manager holds an open position
    # regardless of stop-loss / target. Captures the "20-30 min spike
    # capture" rule — when the window closes we exit at the last
    # price with reason TIME_EXIT. Per-position values are read live
    # so an operator can shorten / extend the window without a code
    # change. Default 1080s = 18 minutes (RISK.md §3 — news alpha decays
    # fast; holding longer only adds mean-reversion risk).
    MAX_HOLD_SECONDS: int = 1080
    # Realtime Fyers WebSocket feed (data socket = live prices, order
    # socket = fill tracking). When True (default) it supersedes per-symbol
    # REST quote polling for any symbol the socket is actively streaming;
    # set False to fall back to pure REST polling.
    FYERS_STREAMING_ENABLED: bool = True

    # ---------- Risk management framework (RISK.md) ----------
    # §1 Pre-trade filters. Some of these gate on market data we don't
    # yet source (India VIX, live bid/ask spread); those filters NO-OP
    # (skip + log) rather than fake a pass until a feed is wired — see
    # app/risk/market_clock.py and the engine's filter seams.
    MIN_SENTIMENT_CONFIDENCE: float = 0.7      # block weaker-conviction LLM calls
    INDIA_VIX_MAX: float = 30.0                # suspend entries above this (needs feed)
    MAX_SPREAD_PCT: float = 0.1               # max bid/ask spread % (needs L1 feed)
    SHORTING_ENABLED: bool = True             # allow negative-news shorts (intraday)
    # When True, an unknown ADV blocks the trade in LIVE mode (fail
    # closed). Default False because the Fyers quote does not yet carry
    # real volume — flipping this on before a volume feed is wired would
    # block every live trade. Turn it on once ADV is sourced for real.
    REQUIRE_KNOWN_LIQUIDITY: bool = False
    # §2 Position sizing. The risk-based qty and the notional cap are
    # both computed; the SMALLER wins. A fresh account ramps its
    # per-trade risk from RISK_RAMP_START_PCT up to MAX_CAPITAL_RISK_PCT
    # over the first RISK_RAMP_TRADES filled trades.
    RISK_RAMP_TRADES: int = 100
    RISK_RAMP_START_PCT: float = 0.5
    # §2b Intraday leverage. Fyers MIS gives ~5x buying power on equity
    # intraday (20% margin, SEBI peak-margin rules). NOTIONAL caps — the
    # sizing clamp, single-name cap (R7), sector cap (R9) — are computed
    # against buying power = equity × INTRADAY_LEVERAGE. Everything
    # denominated in REAL money stays on raw equity: risk-per-trade
    # (MAX_CAPITAL_RISK_PCT), daily/weekly/monthly loss breakers. 1.0 =
    # unleveraged sizing.
    INTRADAY_LEVERAGE: float = 5.0
    # §3 Stops / targets / trailing (R = initial risk per share).
    SCALE_OUT_ENABLED: bool = True            # partial at target R, trail the rest
    SCALE_OUT_R: float = 2.0                  # take-profit multiple of initial risk
    TRAIL_ACTIVATE_R: float = 1.5             # arm the trailing stop at +1.5R
    TRAIL_DISTANCE_R: float = 0.5             # trail the high/low by 0.5R
    DEFAULT_SL_MIN_PCT: float = 1.0           # floor stop distance (1% of entry)
    DEFAULT_SL_SMALLCAP_PCT: float = 1.5      # 1.5% for stocks under SMALLCAP_PRICE
    SMALLCAP_PRICE: float = 200.0
    # §3b Volatility-aware (ATR) initial stop. When ATR is available the
    # stop distance is ATR×ATR_STOP_MULT (clamped to [floor, ATR_MAX_STOP_PCT]
    # of entry); otherwise we fall back to the percentage stop above. Wider
    # stops on volatile names cut whipsaw; tighter stops on quiet names cut
    # the loss when wrong. The ATR feed is wired fail-safe — until intraday
    # candles are sourced the provider returns None and the % stop applies.
    ATR_ENABLED: bool = True
    ATR_PERIOD: int = 14                      # Wilder period (intraday)
    ATR_STOP_MULT: float = 2.0               # stop distance = ATR × this
    ATR_MAX_STOP_PCT: float = 8.0            # cap the ATR stop at this % of entry
    # §4 Portfolio circuit breakers. Loss limits are % of the relevant
    # anchor equity (day-start, week-peak, month-start). Tripping the
    # daily/monthly breaker flattens + halts; weekly throttles risk.
    WEEKLY_MAX_LOSS_PCT: float = 5.0
    MONTHLY_MAX_DRAWDOWN_PCT: float = 8.0
    WEEKLY_BREACH_RISK_PCT: float = 0.25      # risk/trade after a weekly breach
    MAX_CONSECUTIVE_LOSERS: int = 4
    CONSECUTIVE_LOSER_PAUSE_MINUTES: int = 45
    CONSECUTIVE_LOSER_RESUME_CONFIDENCE: float = 0.85
    MAX_TRADES_PER_DAY: int = 12
    # §4b Clustering control. News often hits correlated names at once (e.g.
    # every PSU bank on one policy headline). The whole-portfolio sector cap
    # (SECTOR_CONCENTRATION_PCT) limits exposure; MAX_POSITIONS_PER_SECTOR
    # limits the COUNT of names per sector; and the cluster window realises
    # "take only the single best when several names move on the same event"
    # by blocking a second entry in a sector within
    # SECTOR_CLUSTER_WINDOW_SECONDS of the first (NSE/BSE filings are
    # per-company, so same-sector + short window ≈ same news event).
    SECTOR_CONCENTRATION_PCT: float = 25.0
    MAX_POSITIONS_PER_SECTOR: int = 2
    SECTOR_CLUSTER_WINDOW_SECONDS: int = 120
    # §5 Execution safeguards. Auto entries use an IOC marketable-limit
    # (last ± ENTRY_BUFFER_PCT) cancelled after ORDER_FILL_TIMEOUT; a
    # fill worse than MAX_SLIPPAGE_PCT off the intended entry is rejected.
    ENTRY_BUFFER_PCT: float = 0.2
    MAX_SLIPPAGE_PCT: float = 0.25
    ORDER_FILL_TIMEOUT_SECONDS: float = 1.5
    # §5b Entry state machine (app/execution/entry_manager.py).
    # Anti-chase drift gate: block the entry when the live price has
    # moved more than ENTRY_MAX_DRIFT_PCT AGAINST the signal price; the
    # signal then sits in RETRACEMENT_WATCH for RETRACEMENT_WINDOW_SECONDS
    # (polled every RETRACEMENT_POLL_SECONDS) and re-arms if the price
    # pulls back inside the band — else it expires untraded.
    ENTRY_MAX_DRIFT_PCT: float = 1.5
    RETRACEMENT_WINDOW_SECONDS: int = 120
    RETRACEMENT_POLL_SECONDS: float = 1.0
    # Symbol mutex: a second signal for a symbol already in ORDER_ROUTING
    # waits this long for the lock, then rejects (SYMBOL_LOCKED) — stops
    # double entries when NSE + BSE publish the same news.
    SYMBOL_LOCK_WAIT_SECONDS: float = 0.5
    # Dual-confirmation fill window: the order-WS fill event is the fast
    # path; REST /orders is polled at the ORDER_FILL_TIMEOUT mark (above)
    # and the attempt gives up at ENTRY_FILL_TIMEOUT_SECONDS (zero fill →
    # EXPIRED, partial → keep the fill, cancel the remainder).
    # BROKER_ORDER_TIMEOUT_SECONDS caps the place-order call itself; on
    # timeout the attempt is NEVER retried (duplicate-order risk).
    ENTRY_FILL_TIMEOUT_SECONDS: float = 2.0
    BROKER_ORDER_TIMEOUT_SECONDS: float = 3.0
    # §5c Exit execution (TradeManager → broker). Live exits go out as a
    # marketable limit (bid/ask ∓ EXIT_BUFFER_PCT); anything unfilled at
    # EXIT_FILL_TIMEOUT is cancelled and chased with a MARKET order. A
    # rejected exit retries at market up to EXIT_RETRY_MAX times; a
    # broker call that hangs past EXIT_BROKER_TIMEOUT gets ONE market
    # retry, then the position is flagged critical for the operator
    # (system.error → Telegram) and re-tried on the next cycle.
    EXIT_BUFFER_PCT: float = 0.2
    EXIT_FILL_TIMEOUT_SECONDS: float = 2.0
    EXIT_BROKER_TIMEOUT_SECONDS: float = 3.0
    EXIT_RETRY_MAX: int = 3
    # §3b Exit rules (TradeManager). Breakeven: once profit reaches
    # BREAKEVEN_AT_PCT, lock the stop at entry ± BREAKEVEN_LOCK_PCT so a
    # winner can no longer turn into a loser. Consolidation: profit in
    # [min, max] band with the price pinned inside CONSOLIDATION_RANGE_PCT
    # for the whole window → take the small profit, free the capital.
    # Stall: a bigger winner whose rate-of-change collapses below
    # STALL_ROC_PCT over the window → capture before the reversion.
    BREAKEVEN_ENABLED: bool = True
    BREAKEVEN_AT_PCT: float = 2.0
    BREAKEVEN_LOCK_PCT: float = 0.2
    CONSOLIDATION_EXIT_ENABLED: bool = True
    CONSOLIDATION_WINDOW_SECONDS: int = 120
    CONSOLIDATION_RANGE_PCT: float = 0.5
    CONSOLIDATION_MIN_PROFIT_PCT: float = 1.0
    CONSOLIDATION_MAX_PROFIT_PCT: float = 2.5
    STALL_EXIT_ENABLED: bool = True
    STALL_WINDOW_SECONDS: int = 90
    STALL_ROC_PCT: float = 0.3
    STALL_MIN_PROFIT_PCT: float = 3.0
    STALL_MAX_PROFIT_PCT: float = 6.0
    # Realtime exit safety: price-triggered exits (stop / target / trail /
    # consolidation / stall) only fire on a LIVE tick younger than
    # STALE_QUOTE_SECONDS — a frozen feed must never fire an exit at a
    # stale price (time-based exits still run). Position reconciliation
    # compares the broker's book with ours every
    # POSITION_RECONCILE_SECONDS and marks externally-closed positions
    # CLOSED_EXTERNAL (manual square-off in the Fyers app, margin call).
    STALE_QUOTE_SECONDS: float = 5.0
    POSITION_RECONCILE_SECONDS: int = 60
    # ---------- Performance-weighted sizer (app/risk/perf_sizer.py) ----
    # Tier the per-trade risk %% on each event type's realised track
    # record (win rate + avg R over the last LOOKBACK closed trades).
    # Fail-safe: fewer than MIN_TRADES closed trades → the base
    # MAX_CAPITAL_RISK_PCT applies unchanged. Demotion (LOW tier) needs
    # only ONE weak metric; promotion (HIGH tier) needs BOTH strong.
    PERF_SIZER_ENABLED: bool = True
    PERF_SIZER_MIN_TRADES: int = 10
    PERF_SIZER_LOOKBACK_TRADES: int = 200
    PERF_SIZER_HIGH_WIN_RATE: float = 0.60
    PERF_SIZER_HIGH_AVG_R: float = 1.5
    PERF_SIZER_HIGH_RISK_PCT: float = 1.0
    PERF_SIZER_LOW_WIN_RATE: float = 0.50
    PERF_SIZER_LOW_AVG_R: float = 1.0
    PERF_SIZER_LOW_RISK_PCT: float = 0.5
    # ---------- Sentiment decay curve (target sizing vs news age) ------
    # News alpha decays in seconds: the target multiple shrinks with the
    # news age at ORDER time (the stop is never widened). <FULL_SECONDS →
    # full RR; <PARTIAL_SECONDS → PARTIAL_MULT; older → STALE_MULT.
    SENTIMENT_DECAY_ENABLED: bool = True
    SENTIMENT_DECAY_FULL_SECONDS: float = 2.0
    SENTIMENT_DECAY_PARTIAL_SECONDS: float = 5.0
    SENTIMENT_DECAY_PARTIAL_MULT: float = 0.8
    SENTIMENT_DECAY_STALE_MULT: float = 0.6
    # ---------- Daily health report (app/services/health_report.py) ----
    # Compiled just after the EOD square-off and pushed through every
    # notification channel whose events filter includes "report".
    HEALTH_REPORT_ENABLED: bool = True
    HEALTH_REPORT_TIME_IST: str = "15:45"
    # Pre-open preflight, fired by the same service. Publishes on
    # `system.error` ONLY when something is wrong, so a silent morning
    # means "good to trade". Guards the two failures that have each cost
    # a whole trading day without showing up until the market was
    # already running: an expired Fyers token (every entry blocks
    # NO_LIVE_PRICE) and AI analysis left switched off (skipped filings
    # are placeholder-marked and never re-analysed). Empty = off.
    HEALTH_REPORT_PREFLIGHT_TIME_IST: str = "09:05"
    LLM_TIMEOUT_SECONDS: float = 12.0         # discard the opportunity past this
    # Hard cap on LLM completion tokens. Generation latency scales almost
    # linearly with output length, and a signal JSON needs only a few
    # hundred tokens — so capping here (clamped against each template's
    # own max_tokens at call time) shaves seconds off every analysis.
    # Paired with the brevity instruction in `render_system_prompt` so the
    # model stays well inside this. Measured on the live pipeline
    # (2026-07-04): a full signal JSON for a real filing ran 165
    # completion tokens, so 300 leaves ~1.8× headroom while cutting
    # generation time further. Don't drop below ~250 — a truncated
    # response fails to parse → lost signal. Editable in Settings.
    LLM_MAX_TOKENS: int = 300
    # LLM retry policy. The old 3-retry / 1s-2s-4s ladder could add 7s+
    # of dead time to a failing call — fatal for a speed-news strategy.
    # One fast retry (0.5s, doubling per attempt) then give up: by the
    # time a second retry would land, the spike is gone anyway and the
    # staleness/deadline gates would discard the trade. Env-configurable;
    # picked up when the analyzer (re)builds its client at startup.
    LLM_MAX_RETRIES: int = 1
    LLM_RETRY_BACKOFF_SECONDS: float = 0.5
    # ---------- Which model reads the filing ----------
    # "deepseek" (default) = the hosted API, behaviour unchanged. "slm" =
    # our own fine-tuned model served behind an OpenAI-compatible
    # /chat/completions endpoint (vLLM). Same prompt templates, same
    # response schema, same rules/risk path — only the endpoint moves, so
    # the two are directly comparable on the Outcomes and Dataset pages.
    # Non-destructive: "slm" with a blank endpoint falls back to DeepSeek
    # rather than blocking a signal.
    LLM_PROVIDER: Literal["deepseek", "slm"] = "deepseek"
    LLM_SLM_ENDPOINT: str = ""       # http://host:8000/v1/chat/completions
    LLM_SLM_MODEL: str = "tradebot-slm-v1"
    # Optional. Blank = the endpoint is unauthenticated (vLLM started
    # without --api-key). Secret, so it is .env-only — never returned by
    # GET /api/settings.
    LLM_SLM_API_KEY: str = ""
    # Hard end-to-end deadline (seconds from `filed_at` to signal). If a
    # fully-analysed announcement is older than this by the time the
    # signal would be created, the analysis is still stored (data for
    # Phase 4) but the signal is BLOCKED with reason pipeline_deadline —
    # a late entry is worse than no entry. 0 = disabled (default, so the
    # legacy behavior is untouched until the operator opts in from the
    # Settings page; recommended ~15s once the pipeline is fast).
    PIPELINE_DEADLINE_SECONDS: int = 0
    # Pre-LLM noise filter: drop clearly-administrative filings (trading-
    # window notices, compliance certificates, newspaper publications, …)
    # BEFORE spending an LLM call on them. Saves cost and — because the
    # analyzer processes announcements serially — stops the queue backing
    # up behind junk so a real market-mover isn't stuck waiting. The
    # denylist is deliberately conservative; see app/analyzer/prompts.py.
    PRE_LLM_FILTER_ENABLED: bool = True
    # Master switch for the AI (LLM) analysis of incoming news. When OFF,
    # announcements are still monitored and stored, but nothing is sent to
    # DeepSeek — no analyses, no signals, no auto trades. Manual trading
    # from the Trade page is unaffected. Toggleable from the Dashboard.
    AI_ANALYSIS_ENABLED: bool = True
    # Extracted-text mode (Settings toggle). OFF (default) = the legacy
    # behavior: DeepSeek gets the pdf_url + headline metadata only. ON =
    # the analyzer downloads the filing PDF, extracts the relevant pages
    # (Hindi half dropped, keyword-scored page selection), and sends the
    # actual text to DeepSeek. Any download/extraction failure falls back
    # to the legacy path — this mode can degrade but never block a signal.
    SEND_EXTRACTED_TEXT: bool = False
    # Budget knobs for extracted-text mode. The fetch timeout is deliberately
    # short with no retries (the news spike decays in seconds); the char cap
    # keeps the prompt near ~6k tokens so DeepSeek stays fast.
    PDF_FETCH_TIMEOUT_SECONDS: float = 4.0
    PDF_MAX_TEXT_CHARS: int = 24_000
    # Deterministic fast track (Settings toggle). OFF (default) = every
    # filing takes the LLM track (legacy behavior). ON = a few unambiguous
    # high-conviction headline shapes (order win with explicit Rs-crore
    # value, KMP resignation, buyback with value) skip the LLM and go
    # straight to the rules engine — signal in milliseconds. Non-matches
    # always fall through to the LLM track. Includes the HYBRID path:
    # order-context headline without a value → value parsed from the
    # filing PDF text (still no LLM). See app/analyzer/fast_track.py.
    FAST_TRACK_ENABLED: bool = True
    # Phase 4 outcome logger: records the Fyers price at signal time and
    # +5m/+30m for EVERY signal (approved and blocked) into
    # signal_outcomes. Pure telemetry — no trading influence — so it
    # defaults ON; it is the win-rate report and the future ML dataset.
    OUTCOME_LOGGER_ENABLED: bool = True
    # ---------- Dataset builder (app/services/dataset_builder.py) ------
    # Enriches every signal_outcomes row with 1-minute-candle reaction
    # features + horizon targets once the reaction window has elapsed.
    # Pure telemetry (no trading influence). HORIZON is the label
    # horizon in minutes; FLAT_THRESHOLD is the |return| below which
    # the horizon label is FLAT; SPIKE_THRESHOLD feeds the
    # time-to-first-spike reaction-speed features. Rows older than
    # MAX_AGE_DAYS are marked too_old instead of fetched (Fyers 1-min
    # history is finite); PARTIAL/no-candle rows retry MAX_ATTEMPTS
    # times before the builder stops trying.
    DATASET_BUILDER_ENABLED: bool = True
    DATASET_HORIZON_MINUTES: int = 15
    DATASET_ENRICH_INTERVAL_SECONDS: float = 120.0
    DATASET_FLAT_THRESHOLD_PCT: float = 0.3
    DATASET_SPIKE_THRESHOLD_PCT: float = 1.0
    DATASET_MAX_ATTEMPTS: int = 3
    DATASET_MAX_AGE_DAYS: int = 30
    # Pause before each candle-history fetch (cache misses only) so a
    # full-history backfill stays far inside Fyers' rate limits.
    DATASET_FETCH_DELAY_SECONDS: float = 0.25
    # Once per trading day after the close, pull the day's 1-minute candles
    # for every symbol the dataset is still waiting on, then fill prices,
    # AI labels and company metadata. Without it those columns stay NULL
    # forever — the candle store is a static export, so a filing made after
    # it has nothing to be priced against. Default OFF: new behaviour ships
    # behind a toggle, and this one costs a Fyers history call per symbol.
    DATASET_EOD_ENABLED: bool = False
    # After the 15:30 close, with room for the last candles to settle.
    DATASET_EOD_TIME_IST: str = "15:45"
    # How often the real-time pass runs. It prices the day's announcements as
    # soon as they are priceable — px_60m needs 60 minutes of trading to exist,
    # so that delay is the data's, not the pipeline's. Small by construction:
    # only symbols with fresh unfilled rows, a couple of days of candles each.
    DATASET_AUTOFILL_MINUTES: int = 10
    # Market session (IST, "HH:MM"). Entry window excludes the first
    # 15 min after open and the last 30 min before close; all intraday
    # positions are force-squared-off at SQUARE_OFF_TIME.
    MARKET_OPEN_IST: str = "09:15"
    MARKET_CLOSE_IST: str = "15:30"
    ENTRY_WINDOW_START_IST: str = "09:30"
    ENTRY_WINDOW_END_IST: str = "15:00"
    SQUARE_OFF_TIME_IST: str = "15:10"
    # Skip the market-hours / session gate (handy for paper testing or
    # backtests where wall-clock time shouldn't block entries).
    ENFORCE_MARKET_HOURS: bool = True

    # ---------- Internal ----------
    TESTING: int = 0
    APP_VERSION: str = "0.1.0"

    # ----- Validators -----

    @field_validator("BIND_PORT")
    @classmethod
    def _port_in_range(cls, v: int) -> int:
        if not (1 <= v <= 65535):
            raise ValueError("BIND_PORT must be 1..65535")
        return v

    @field_validator("LOG_LEVEL")
    @classmethod
    def _log_level(cls, v: str) -> str:
        v_up = v.upper()
        if v_up not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"LOG_LEVEL must be one of DEBUG/INFO/WARNING/ERROR/CRITICAL, got {v!r}")
        return v_up

    @field_validator(
        "MAX_CAPITAL_RISK_PCT",
        "DAILY_MAX_LOSS_PCT",
        "MAX_SINGLE_POSITION_PCT",
        "DEFAULT_SL_PCT",
        "ATR_MAX_STOP_PCT",
        "SECTOR_CONCENTRATION_PCT",
    )
    @classmethod
    def _pct_in_range(cls, v: float) -> float:
        if not (0 < v <= 100):
            raise ValueError("percent value must be in (0, 100]")
        return v

    @field_validator("PORTFOLIO_VALUE", "DEFAULT_TARGET_RR", "ATR_STOP_MULT")
    @classmethod
    def _strictly_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("value must be > 0")
        return v

    @field_validator("INTRADAY_LEVERAGE")
    @classmethod
    def _leverage_in_range(cls, v: float) -> float:
        # 1x = unleveraged; Fyers MIS equity intraday is ~5x. Anything
        # past 10x is a typo, not a broker product.
        if not (1.0 <= v <= 10.0):
            raise ValueError("INTRADAY_LEVERAGE must be between 1 and 10")
        return v

    @field_validator("QUOTE_REFRESH_SECONDS")
    @classmethod
    def _refresh_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("QUOTE_REFRESH_SECONDS must be >= 1")
        return v

    @field_validator("POLL_INTERVAL_SECONDS", "LLM_MAX_TOKENS", "ATR_PERIOD")
    @classmethod
    def _ge_one(cls, v: int) -> int:
        if v < 1:
            raise ValueError("value must be >= 1")
        return v

    @field_validator("MAX_NEWS_AGE_SECONDS", "MAX_HOLD_SECONDS")
    @classmethod
    def _positive_seconds(cls, v: int) -> int:
        if v < 1:
            raise ValueError("seconds value must be >= 1")
        return v

    @field_validator(
        "MAX_CONCURRENT_POSITIONS",
        "MAX_SIGNALS_PER_DAY",
        "MAX_POSITIONS_PER_SECTOR",
        "SECTOR_CLUSTER_WINDOW_SECONDS",
        "LLM_MAX_RETRIES",
        "PIPELINE_DEADLINE_SECONDS",
    )
    @classmethod
    def _positive_int(cls, v: int) -> int:
        if v < 0:
            raise ValueError("count must be non-negative")
        return v

    @field_validator("MIN_LIQUIDITY_CRORE")
    @classmethod
    def _positive_float(cls, v: float) -> float:
        if v < 0:
            raise ValueError("min liquidity must be non-negative")
        return v

    @field_validator(
        "MIN_SENTIMENT_CONFIDENCE",
        "CONSECUTIVE_LOSER_RESUME_CONFIDENCE",
        "RISK_RAMP_START_PCT",
    )
    @classmethod
    def _conf_in_unit_or_pct(cls, v: float) -> float:
        # MIN_SENTIMENT_CONFIDENCE / resume confidence are 0..1 (LLM
        # confidence scale); RISK_RAMP_START_PCT is a small percent. All
        # three must be > 0 — a zero floor would disable the check.
        if v <= 0:
            raise ValueError("value must be > 0")
        return v

    @field_validator(
        "MARKET_OPEN_IST",
        "MARKET_CLOSE_IST",
        "ENTRY_WINDOW_START_IST",
        "ENTRY_WINDOW_END_IST",
        "SQUARE_OFF_TIME_IST",
    )
    @classmethod
    def _hhmm(cls, v: str) -> str:
        parts = v.split(":")
        if len(parts) != 2 or not (parts[0].isdigit() and parts[1].isdigit()):
            raise ValueError(f"time must be 'HH:MM', got {v!r}")
        hh, mm = int(parts[0]), int(parts[1])
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError(f"time out of range: {v!r}")
        return f"{hh:02d}:{mm:02d}"

    # ----- Derived helpers -----

    @property
    def is_testing(self) -> bool:
        return bool(self.TESTING)

    @property
    def is_live(self) -> bool:
        return self.TRADING_MODE == "live"

    @property
    def is_paper(self) -> bool:
        return self.TRADING_MODE == "paper"

    @property
    def deepseek_configured(self) -> bool:
        return bool(self.DEEPSEEK_API_KEY.strip())

    @property
    def fyers_configured(self) -> bool:
        return bool(self.FYERS_APP_ID.strip() and self.FYERS_SECRET_KEY.strip())

    @property
    def bind_public(self) -> bool:
        """True if bound to a non-loopback interface — used for the
        'no auth, no public bind' startup warning."""
        return self.BIND_HOST not in ("127.0.0.1", "localhost", "::1")

    def effective_database_url(self) -> str:
        if self.is_testing:
            return "sqlite:///:memory:"
        return self.DATABASE_URL


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor. Tests can call `get_settings.cache_clear()`
    after mutating the environment."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the lru_cache — used by tests after env mutation."""
    get_settings.cache_clear()
