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
    POLL_INTERVAL_SECONDS: int = 5

    # ---------- Risk defaults (per-strategy overrides in DB) ----------
    MAX_CAPITAL_RISK_PCT: float = 1.0
    DAILY_MAX_LOSS_PCT: float = 2.0
    MAX_CONCURRENT_POSITIONS: int = 5
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
    )
    @classmethod
    def _pct_in_range(cls, v: float) -> float:
        if not (0 < v <= 100):
            raise ValueError("percent value must be in (0, 100]")
        return v

    @field_validator("PORTFOLIO_VALUE", "DEFAULT_TARGET_RR")
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

    @field_validator("MAX_CONCURRENT_POSITIONS", "MAX_SIGNALS_PER_DAY")
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
