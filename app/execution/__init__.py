"""Execution + risk layer for the AI News Trading Bot.

Subscribes to `signals.new` (produced by T3's analyzer), runs the full
risk engine against each signal, and routes approved signals to the
right TradingBackend based on the strategy's broker_account and the
global TRADING_MODE.

Public API:
    TradingBackend    Protocol every broker adapter implements.
    PaperBackend      In-process simulator. Subscribes to market data
                      for SL / LIMIT triggers. Persists trades +
                      positions in SQLite.
    FyersLiveBackend  Thin httpx wrapper for the Fyers REST API. 30s
                      default timeout. Retries on 5xx, raises on 401.
    Manager           Multi-account router. Owns the event-bus loop
                      and the per-broker backend instances.
    RiskEngine        Pure-function risk checker with the full rule
                      set. Returns a RiskDecision that the manager
                      honours — risk overrides signals, no soft
                      override anywhere.
    MarketDataBus     Last-price / quote cache that the paper backend
                      uses to trigger pending SL / LIMIT orders.
    fyers_auth        One-shot CLI that prints the Fyers OAuth URL
                      (`python -m app.execution.fyers_auth`).

The execution layer never calls out to the network in tests; sibling
backends are constructed with stubbed transports / fake market data.
"""
from app.execution.base import (
    OrderResult,
    OrderStatus,
    Position,
    TradingBackend,
)
from app.execution.market_data import MarketDataBus, Quote
from app.execution.paper import PaperBackend
from app.execution.fyers_live import FyersLiveBackend
from app.execution.fyers_auth import build_authorize_url, exchange_code_for_token
from app.execution.manager import (
    CHANNEL_RISK_BLOCKED,
    CHANNEL_TRADE_EXECUTED,
    Manager,
)
from app.risk.engine import (
    RiskDecision,
    RiskEngine,
    StrategyOverrides,
    SizingResult,
    load_sector_map,
)

__all__ = [
    "OrderResult",
    "OrderStatus",
    "Position",
    "TradingBackend",
    "MarketDataBus",
    "Quote",
    "PaperBackend",
    "FyersLiveBackend",
    "build_authorize_url",
    "exchange_code_for_token",
    "CHANNEL_RISK_BLOCKED",
    "CHANNEL_TRADE_EXECUTED",
    "Manager",
    "RiskDecision",
    "RiskEngine",
    "StrategyOverrides",
    "SizingResult",
    "load_sector_map",
]
