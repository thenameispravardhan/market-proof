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
    # back to the slower Playwright path or stalls entirely). 2s is a
    # balance for a single local operator; raise it if you start seeing
    # `monitor.retry` 403/429 spam in the logs.
    POLL_INTERVAL_SECONDS: int = 2

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
    # Default 90s gives ~70s of slack inside the 20-second trade
    # budget for downstream latency (LLM + Fyers + fill).
    MAX_NEWS_AGE_SECONDS: int = 90
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
    LLM_TIMEOUT_SECONDS: float = 18.0         # discard the opportunity past this
    # Hard cap on LLM completion tokens. Generation latency scales almost
    # linearly with output length, and a signal JSON needs only a few
    # hundred tokens — so capping here (clamped against each template's
    # own max_tokens at call time) shaves seconds off every analysis.
    # Paired with the brevity instruction in `render_system_prompt` so the
    # model stays well inside this. 1024 keeps the latency win over the
    # 2000-token template default while leaving 2-3× headroom over the
    # expected JSON length, so a slightly verbose response can't truncate
    # its JSON mid-string (a truncated response fails to parse → lost
    # signal). Don't drop this below the size a full signal JSON needs.
    LLM_MAX_TOKENS: int = 1024
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
