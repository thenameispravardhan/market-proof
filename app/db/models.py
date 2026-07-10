"""SQLAlchemy ORM models for the trading bot.

Table list (14):

  Core (6):
    announcements, analyses, signals, trades, positions, risk_events

  Advanced (8):
    strategies, broker_accounts, signal_rules, prompt_templates,
    prompt_history, webhooks, webhook_deliveries, notification_channels,
    notification_log, backtest_runs, audit_log

Naming convention: every table has an explicit `__tablename__` and the
columns mirror the schema in `t1-infra` task spec.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base
from app.db.infra_models import AppSetting  # noqa: F401 — registers app_settings table


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# =========================================================================
# Core: announcements (raw PDF events scraped from BSE / NSE)
# =========================================================================


class Announcement(Base):
    __tablename__ = "announcements"
    __table_args__ = (UniqueConstraint("content_hash", name="uq_announcements_content_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # SHA-256 hex digest of (exchange|symbol|filed_at|pdf_url) — the scraper
    # computes this and we use it to dedupe repeat filings. Stored as TEXT
    # (64-char hex) so it's indexable + UNIQUE. Nullable because rows
    # inserted by older code paths / migrations may not have it; the
    # scraper (T2) is expected to populate it going forward.
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    symbol: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    exchange: Mapped[str] = mapped_column(String(8), default="BSE", nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    headline: Mapped[str] = mapped_column(String(512), nullable=False)
    body: Mapped[Optional[str]] = mapped_column(Text)
    pdf_url: Mapped[Optional[str]] = mapped_column(String(1024))
    source: Mapped[Optional[str]] = mapped_column(String(64))
    filed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    analyses: Mapped[list["Analysis"]] = relationship(
        back_populates="announcement", cascade="all, delete-orphan"
    )


def compute_content_hash(
    *,
    exchange: str,
    symbol: str,
    filed_at: Optional[datetime],
    pdf_url: Optional[str],
) -> str:
    """Canonical SHA-256 hex digest used to dedupe `announcements`.

    T2 (scraper) calls this when inserting a new row. Stable across
    process restarts because the inputs are normalised:

    - exchange / symbol are upper-cased
    - pdf_url is stripped
    - filed_at is converted to ISO-8601 UTC

    Returns a 64-character lowercase hex string.
    """
    import hashlib

    norm_exchange = (exchange or "").strip().upper()
    norm_symbol = (symbol or "").strip().upper()
    norm_url = (pdf_url or "").strip()
    if filed_at is not None:
        if filed_at.tzinfo is None:
            filed_at = filed_at.replace(tzinfo=timezone.utc)
        norm_filed = filed_at.astimezone(timezone.utc).isoformat()
    else:
        norm_filed = ""
    payload = f"{norm_exchange}|{norm_symbol}|{norm_filed}|{norm_url}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# =========================================================================
# Core: analyses (DeepSeek output on an announcement)
# =========================================================================


class Analysis(Base):
    __tablename__ = "analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    announcement_id: Mapped[int] = mapped_column(
        ForeignKey("announcements.id", ondelete="CASCADE"), index=True, nullable=False
    )
    model: Mapped[str] = mapped_column(String(64), default="deepseek-chat", nullable=False)
    sentiment: Mapped[Optional[str]] = mapped_column(String(16))           # positive | neutral | negative
    sentiment_score: Mapped[Optional[float]] = mapped_column(Float)         # -100..100
    confidence: Mapped[Optional[float]] = mapped_column(Float)             # 0..1
    recommendation: Mapped[Optional[str]] = mapped_column(String(16))      # buy | sell | hold
    rationale: Mapped[Optional[str]] = mapped_column(Text)
    deal_value_inr_crore: Mapped[Optional[float]] = mapped_column(Float)
    stake_change_pct: Mapped[Optional[float]] = mapped_column(Float)
    dividend_per_share: Mapped[Optional[float]] = mapped_column(Float)
    buyback_value_inr_crore: Mapped[Optional[float]] = mapped_column(Float)
    raw_response: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    announcement: Mapped["Announcement"] = relationship(back_populates="analyses")
    signals: Mapped[list["Signal"]] = relationship(
        back_populates="analysis", cascade="all, delete-orphan"
    )


# =========================================================================
# Core: signals (post-rule-engine action plan)
# =========================================================================


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    analysis_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("analyses.id", ondelete="SET NULL"), index=True
    )
    strategy_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("strategies.id", ondelete="SET NULL"), index=True
    )
    rule_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("signal_rules.id", ondelete="SET NULL")
    )
    symbol: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    action: Mapped[str] = mapped_column(String(8), nullable=False)         # BUY | SELL | HOLD | BLOCK
    confidence: Mapped[Optional[float]] = mapped_column(Float)
    position_size_pct: Mapped[Optional[float]] = mapped_column(Float)
    rationale: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    analysis: Mapped[Optional["Analysis"]] = relationship(back_populates="signals")
    trades: Mapped[list["Trade"]] = relationship(back_populates="signal")


# =========================================================================
# Core: signal outcomes (what the stock actually did after each signal)
# =========================================================================
#
# Written passively by app/services/outcome_logger.py for EVERY signal
# (approved or blocked — blocked ones are exactly the counterfactuals a
# win-rate review needs). Price moves at +5m / +30m vs the price at
# signal time, measured off the Fyers quote feed. This table is the
# labeled dataset Phase 5 (materiality pre-filter) and Phase 6
# (fine-tuning) train on — see plan.txt.


class SignalOutcome(Base):
    __tablename__ = "signal_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Loose reference (no FK): outcomes are telemetry / training data and
    # must survive signal rows being pruned or arriving out of band.
    signal_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    symbol: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    action: Mapped[str] = mapped_column(String(8), nullable=False)
    signal_status: Mapped[Optional[str]] = mapped_column(String(16))  # pending/blocked at signal time
    price_at_signal: Mapped[Optional[float]] = mapped_column(Float)
    price_5m: Mapped[Optional[float]] = mapped_column(Float)
    price_30m: Mapped[Optional[float]] = mapped_column(Float)
    move_5m_pct: Mapped[Optional[float]] = mapped_column(Float)
    move_30m_pct: Mapped[Optional[float]] = mapped_column(Float)
    # "ok" | "no_quote_at_signal" | "no_quote_5m" | ... — data-quality tag
    # so a market-closed sample isn't mistaken for a flat move.
    note: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


# =========================================================================
# Core: dataset features (the ML training-set enrichment per signal)
# =========================================================================
#
# Written by app/services/dataset_builder.py: once a signal's reaction
# window has fully elapsed, the builder fetches 1-minute Fyers candles
# around the signal time and computes the price-trajectory features
# (T+1..T+5 — safe to use as model inputs) and the horizon targets
# (T+15 high/close/drawdown — training labels ONLY, using them as
# features is look-ahead bias). Everything lands in the `features`
# JSON blob so new feature keys never need a schema migration; the
# /api/dataset column catalog documents each key's feature-vs-target
# role.


class DatasetFeature(Base):
    __tablename__ = "dataset_features"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Loose references (no FK) — like signal_outcomes, this is training
    # telemetry and must survive source rows being pruned.
    # Two row sources share this table:
    #   signal rows — outcome_id set (one per signal_outcomes row)
    #   shadow rows — outcome_id NULL, announcement_id set: analyzed
    #     filings that never produced a signal (noise-filtered, HOLD,
    #     stale). These are the NEGATIVE examples a materiality
    #     pre-filter model trains on.
    outcome_id: Mapped[Optional[int]] = mapped_column(Integer, unique=True, index=True)
    announcement_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    signal_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    symbol: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    signal_time: Mapped[Optional[datetime]] = mapped_column(DateTime)
    # complete | partial (early candles but no horizon candle — e.g. a
    # signal near market close) | no_candles | too_old | error
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    features: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    note: Mapped[Optional[str]] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )


# =========================================================================
# Core: trades (executed orders)
# =========================================================================


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("signals.id", ondelete="SET NULL"), index=True
    )
    broker_account_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="SET NULL"), index=True
    )
    symbol: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)           # BUY | SELL
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), default="market", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="placed", nullable=False)
    broker_order_id: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    pnl: Mapped[Optional[float]] = mapped_column(Float)
    # Realised-trade analytics (RISK.md §7): % the fill slipped from the
    # intended entry, and the exit's reward-to-risk multiple. Populated
    # on entry fill (slippage) and on close (r_multiple).
    slippage_pct: Mapped[Optional[float]] = mapped_column(Float)
    r_multiple: Mapped[Optional[float]] = mapped_column(Float)
    executed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    signal: Mapped[Optional["Signal"]] = relationship(back_populates="trades")


# =========================================================================
# Core: positions (current open positions)
# =========================================================================


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    average_price: Mapped[float] = mapped_column(Float, nullable=False)
    last_price: Mapped[Optional[float]] = mapped_column(Float)
    unrealized_pnl: Mapped[Optional[float]] = mapped_column(Float)
    # Managed exit levels, persisted so the trade manager can re-arm
    # stop-loss / target after a restart (the in-memory book is empty
    # then). Nullable — a position may have no managed exits.
    stop_loss: Mapped[Optional[float]] = mapped_column(Float)
    target: Mapped[Optional[float]] = mapped_column(Float)
    strategy_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("strategies.id", ondelete="SET NULL")
    )
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )


# =========================================================================
# Core: risk_events
# =========================================================================


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), default="warning", nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    halted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


# =========================================================================
# Core: risk_state (singleton — portfolio circuit-breaker + kill-switch)
# =========================================================================


class RiskState(Base):
    """Persistent, account-wide risk state. Exactly one row (id=1).

    Backs the circuit breakers and the kill switch: the execution
    manager refuses to place new orders while `trading_disabled` is
    set or `halted_until` is in the future, and the breaker checker
    flattens + sets these when a limit trips. Anchors are captured on
    the first evaluation of each new day / week / month so equity
    drawdown can be measured against a stable baseline.
    """

    __tablename__ = "risk_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    # Kill switch / daily / monthly halt.
    trading_disabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    disabled_reason: Mapped[Optional[str]] = mapped_column(String(256))
    # Consecutive-loser cooldown — entries refused until this passes.
    halted_until: Mapped[Optional[datetime]] = mapped_column(DateTime)
    # Per-trade risk override (e.g. throttled after a weekly-loss breach,
    # or the graduated ramp on a young account). None → use settings.
    current_risk_pct: Mapped[Optional[float]] = mapped_column(Float)
    # Drawdown anchors (the date the window opened + the equity then).
    day_anchor: Mapped[Optional[date]] = mapped_column(Date)
    day_start_equity: Mapped[Optional[float]] = mapped_column(Float)
    week_anchor: Mapped[Optional[date]] = mapped_column(Date)        # Monday of the week
    week_peak_equity: Mapped[Optional[float]] = mapped_column(Float)  # running peak for peak→trough
    month_anchor: Mapped[Optional[date]] = mapped_column(Date)       # 1st of the month
    month_start_equity: Mapped[Optional[float]] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )


# =========================================================================
# Advanced: strategies
# =========================================================================


class Strategy(Base):
    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    config: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)  # risk overrides
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    rules: Mapped[list["SignalRule"]] = relationship(
        back_populates="strategy", cascade="all, delete-orphan", order_by="SignalRule.priority"
    )


# =========================================================================
# Advanced: broker_accounts
# =========================================================================


class BrokerAccount(Base):
    __tablename__ = "broker_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    broker: Mapped[str] = mapped_column(String(32), nullable=False)
    app_id: Mapped[Optional[str]] = mapped_column(String(128))
    secret_key: Mapped[Optional[str]] = mapped_column(Text)              # never log
    access_token: Mapped[Optional[str]] = mapped_column(Text)            # never log
    redirect_uri: Mapped[Optional[str]] = mapped_column(String(512))
    paper_mode: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )


# =========================================================================
# Advanced: signal_rules
# =========================================================================


class SignalRule(Base):
    __tablename__ = "signal_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_id: Mapped[int] = mapped_column(
        ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    conditions: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    action: Mapped[str] = mapped_column(String(8), nullable=False)         # BUY|SELL|HOLD|BLOCK
    action_params: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    strategy: Mapped["Strategy"] = relationship(back_populates="rules")

    __table_args__ = (Index("ix_signal_rules_strategy_priority", "strategy_id", "priority"),)


# =========================================================================
# Advanced: prompt_templates
# =========================================================================


class PromptTemplate(Base):
    __tablename__ = "prompt_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    user_template: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(64), default="deepseek-chat", nullable=False)
    temperature: Mapped[float] = mapped_column(Float, default=0.2, nullable=False)
    max_tokens: Mapped[int] = mapped_column(Integer, default=2000, nullable=False)
    # DeepSeek v4 inference controls (T5 prompt page). reasoning_effort is
    # one of low|medium|high and tunes how much the model "thinks" when
    # thinking is enabled. thinking_enabled=False sends the
    # extra_body={"thinking": {"type": "disabled"}} flag (faster/cheaper,
    # no reasoning trace). stream toggles SSE streaming on the call.
    reasoning_effort: Mapped[str] = mapped_column(String(8), default="medium", nullable=False)
    thinking_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    stream: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )
    updated_by: Mapped[Optional[str]] = mapped_column(String(16))

    history: Mapped[list["PromptHistory"]] = relationship(
        back_populates="template", cascade="all, delete-orphan", order_by="PromptHistory.version.desc()"
    )


# =========================================================================
# Advanced: prompt_history
# =========================================================================


class PromptHistory(Base):
    __tablename__ = "prompt_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[int] = mapped_column(
        ForeignKey("prompt_templates.id", ondelete="CASCADE"), index=True, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    user_template: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    temperature: Mapped[float] = mapped_column(Float, nullable=False)
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    # Snapshot of the v4 inference controls at this version (mirrors
    # PromptTemplate). See that model for semantics.
    reasoning_effort: Mapped[str] = mapped_column(String(8), default="medium", nullable=False)
    thinking_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    stream: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    change_note: Mapped[Optional[str]] = mapped_column(Text)

    template: Mapped["PromptTemplate"] = relationship(back_populates="history")


# =========================================================================
# Advanced: webhooks
# =========================================================================


class Webhook(Base):
    __tablename__ = "webhooks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)       # in | out
    event_filter: Mapped[Optional[str]] = mapped_column(String(512))       # csv or "*"
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    secret: Mapped[Optional[str]] = mapped_column(Text)                    # HMAC key for out, bearer for in
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    deliveries: Mapped[list["WebhookDelivery"]] = relationship(
        back_populates="webhook", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_webhooks_direction_enabled", "direction", "enabled"),)


# =========================================================================
# Advanced: webhook_deliveries
# =========================================================================


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    webhook_id: Mapped[int] = mapped_column(
        ForeignKey("webhooks.id", ondelete="CASCADE"), nullable=False
    )
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    event_type: Mapped[Optional[str]] = mapped_column(String(64))
    payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    status_code: Mapped[Optional[int]] = mapped_column(Integer)
    response_body: Mapped[Optional[str]] = mapped_column(Text)
    error: Mapped[Optional[str]] = mapped_column(Text)
    attempted_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    webhook: Mapped["Webhook"] = relationship(back_populates="deliveries")

    __table_args__ = (Index("ix_webhook_deliveries_webhook_time", "webhook_id", "attempted_at"),)


# =========================================================================
# Advanced: notification_channels
# =========================================================================


class NotificationChannel(Base):
    __tablename__ = "notification_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)        # telegram | discord | email | webhook
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    events_filter: Mapped[Optional[str]] = mapped_column(String(256))     # csv: signal,trade,risk_halt,error
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    log: Mapped[list["NotificationLog"]] = relationship(
        back_populates="channel", cascade="all, delete-orphan"
    )


# =========================================================================
# Advanced: notification_log
# =========================================================================


class NotificationLog(Base):
    __tablename__ = "notification_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("notification_channels.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), nullable=False)       # sent | failed | skipped
    error: Mapped[Optional[str]] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    channel: Mapped["NotificationChannel"] = relationship(back_populates="log")

    __table_args__ = (Index("ix_notification_log_channel_time", "channel_id", "sent_at"),)


# =========================================================================
# Advanced: backtest_runs
# =========================================================================


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[Optional[str]] = mapped_column(String(128))
    strategy_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("strategies.id", ondelete="SET NULL"), index=True
    )
    broker_account_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="SET NULL")
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    initial_capital: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    config: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    results: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


# =========================================================================
# Advanced: audit_log
# =========================================================================


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(16), nullable=False)        # ui | api | system
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[Optional[str]] = mapped_column(String(128))
    before: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    after: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    __table_args__ = (Index("ix_audit_log_action_time", "action", "created_at"),)


# -------------------------------------------------------------------------
# Re-export all model classes so siblings can do `from app.db.models import *`
# -------------------------------------------------------------------------
__all__ = [
    "Announcement",
    "Analysis",
    "Signal",
    "DatasetFeature",
    "Trade",
    "Position",
    "RiskEvent",
    "RiskState",
    "Strategy",
    "BrokerAccount",
    "SignalRule",
    "PromptTemplate",
    "PromptHistory",
    "Webhook",
    "WebhookDelivery",
    "NotificationChannel",
    "NotificationLog",
    "BacktestRun",
    "AuditLog",
]
