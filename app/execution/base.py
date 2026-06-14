"""TradingBackend protocol + the value types the execution layer
passes around.

The four methods every backend implements:

    place_order(signal, symbol) -> OrderResult
        Submit an order derived from a signal. For BUY signals we
        open or add to a long; for SELL we close or short (v1 is
        long-only; the backend treats SELL as "close long").

    cancel_order(broker_order_id) -> bool
        Best-effort cancel. True if the broker acknowledged.

    get_positions() -> list[Position]
        Current open positions. Each position carries a
        strategy_id (so the manager can attribute P&L to a
        strategy) and a last_price / unrealized_pnl that the
        paper backend updates from the MarketDataBus.

    get_order_status(broker_order_id) -> OrderStatus
        Status of a single order. Used by the manager to poll
        PENDING orders that didn't fill within their timeout.

The protocol is duck-typed — there is no `runtime_checkable`
metaclass — so callers can pass any object with these four async
methods. Tests use a `StubBackend` to assert the manager calls the
right method with the right arguments.

OrderResult carries enough state to be persisted to a `trades` row
without the manager having to look up the signal again. OrderStatus
is the return shape of get_order_status — the manager polls it
while the order is in `PENDING`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, Protocol, runtime_checkable


# ---- Order + position enums ---------------------------------------------


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_LOSS = "STOP_LOSS"
    STOP_LOSS_MARKET = "SL-M"


class ProductType(str, Enum):
    """Broker product / settlement code.

    Misnamed-vs-Fyers note: Fyers calls this `productType` on the
    API; the values below are the union of what the supported
    brokers accept. `INTRADAY` is the default for the auto-pipeline;
    manual trades pick the right one explicitly.
    """
    INTRADAY = "INTRADAY"     # MIS — square off intraday
    DELIVERY = "DELIVERY"     # CNC — actual delivery to demat
    NORMAL = "NORMAL"         # F&O NRML — carry forward
    MARGIN = "MARGIN"         # F&O NRML with margin benefit
    CO = "CO"                 # Cover order
    BO = "BO"                 # Bracket order (Dhan-style)


class OrderState(str, Enum):
    """The state machine for a placed order.

    PENDING   — submitted to the broker; no fill yet (market data
                hasn't crossed the trigger, or LIMIT not yet hit).
    FILLED    — fully executed. `trades.status = 'filled'`.
    CANCELLED — broker acknowledged cancel before fill.
    REJECTED  — broker refused the order (validation, account, etc.).
    EXPIRED   — timed out (default 30s for market, configurable).
    """
    PENDING = "PENDING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


# ---- Return shapes ------------------------------------------------------


@dataclass
class OrderResult:
    """The result of `place_order` (initial submit).

    The manager persists a `trades` row with these fields. For
    PENDING orders the manager polls `get_order_status` until the
    state moves out of PENDING (or the timeout expires).
    """
    broker_order_id: str                 # unique per broker
    state: OrderState
    symbol: str
    side: OrderSide
    quantity: int
    order_type: OrderType
    filled_quantity: int = 0
    average_price: Optional[float] = None
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    raw: dict[str, Any] = field(default_factory=dict)
    submitted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error: Optional[str] = None

    @property
    def is_terminal(self) -> bool:
        """True if the order won't change state again."""
        return self.state in {
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        }


@dataclass
class OrderStatus:
    """The shape of `get_order_status` — used to poll a PENDING order."""
    broker_order_id: str
    state: OrderState
    filled_quantity: int = 0
    average_price: Optional[float] = None
    raw: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class Position:
    """A single open position. Unique per symbol."""
    symbol: str
    quantity: int
    average_price: float
    last_price: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    strategy_id: Optional[int] = None
    broker_account_id: Optional[int] = None
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def mark_to_market(self, last_price: float) -> None:
        self.last_price = float(last_price)
        self.unrealized_pnl = (float(last_price) - self.average_price) * self.quantity
        self.updated_at = datetime.now(timezone.utc)


# ---- The protocol -------------------------------------------------------


@runtime_checkable
class TradingBackend(Protocol):
    """Every broker adapter implements these four methods."""

    name: str
    broker_account_id: Optional[int]

    async def place_order(
        self,
        *,
        signal: Any,
        symbol: str,
        side: OrderSide,
        quantity: int,
        order_type: OrderType = OrderType.MARKET,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        product_type: "ProductType" = ...,
    ) -> OrderResult:
        """Submit an order. Return the initial OrderResult.

        `product_type` defaults to whatever the backend considers
        the standard product for the segment (INTRADAY for Fyers
        cash by default). Callers that need a specific settlement
        (CNC, NRML, CO, etc.) MUST pass it explicitly. Manual
        trades from the UI always do.
        """
        ...

    async def cancel_order(self, broker_order_id: str) -> bool:
        """Best-effort cancel. True on broker ack."""
        ...

    async def get_positions(self) -> list[Position]:
        """Return all open positions for this account."""
        ...

    async def get_order_status(self, broker_order_id: str) -> OrderStatus:
        """Status of a single order (for polling PENDING fills)."""
        ...


# ---- Helpers ------------------------------------------------------------


def qty_to_lots(qty: int, lot_size: int = 1) -> int:
    """Round a quantity DOWN to the nearest lot. Lot size 1 (default)
    is a no-op; Fyers NSE cash segment is 1 share per lot."""
    if lot_size < 1:
        raise ValueError(f"lot_size must be >= 1, got {lot_size}")
    return (qty // lot_size) * lot_size


def safe_float(x: Any, default: float = 0.0) -> float:
    """Coerce to float; NaN / inf → default. Defensive against bad
    LLM numerics making it into a sizing call."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if math.isnan(v) or math.isinf(v):
        return default
    return v


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
