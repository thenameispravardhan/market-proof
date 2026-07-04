"""Account equity + position sizing (RISK.md §2, §7).

Two jobs the risk engine delegates here:

  1. **Equity** — a real ledger, not gross exposure. The old engine
     summed open-position market values, so "portfolio value" *shrank*
     as you deployed capital and every downstream cap drifted. Here:

         equity = starting capital
                + realised P&L (closed trades)
                + unrealised P&L (open positions, marked to market)

  2. **Sizing** — the order quantity is the SMALLER of:
       - the risk-based qty: floor(equity * risk% / |entry - stop|)
       - the notional cap qty: floor(equity * max_position% / entry)
     so a tiny stop can never blow past the single-name notional cap.

  Plus the graduated-risk ramp (§7): a young account risks
  `RISK_RAMP_START_PCT` per trade for its first `RISK_RAMP_TRADES`
  filled trades, then steps up to the configured cap. A weekly-loss
  breach can throttle this further via `RiskState.current_risk_pct`.

The risk-based math itself still lives in `RiskEngine._compute_qty`
(kept stable for its direct unit tests); this module adds the equity
ledger, the risk-% resolution, and the notional cap on top.
"""
from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Position as PositionRow, RiskState, Trade as TradeRow
from app.logging_config import get_logger

if TYPE_CHECKING:
    from app.execution.market_data import MarketDataBus

log = get_logger(__name__)

FundsFetch = Callable[[], Awaitable[Optional[float]]]


class FyersFundsProvider:
    """Live account equity from the Fyers funds API, cached with a TTL.

    In LIVE mode sizing should anchor to the REAL money in the Fyers
    account, not the static PORTFOLIO_VALUE setting. `equity()` is sync
    (returns the cached value or None); `refresh()` is awaited by the
    risk engine before sizing. Fail-safe — any fetch error leaves the
    value None and the equity ledger falls back to PORTFOLIO_VALUE, so
    a broken funds call can never fake a bigger (or smaller) account.
    """

    def __init__(self, fetch_funds: FundsFetch, *, ttl_s: float = 30.0) -> None:
        self._fetch = fetch_funds
        self._ttl = float(ttl_s)
        self._value: Optional[float] = None
        self._ts: float = 0.0

    def equity(self) -> Optional[float]:
        return self._value

    async def refresh(self) -> None:
        now = time.monotonic()
        if self._value is not None and (now - self._ts) < self._ttl:
            return
        try:
            v = await self._fetch()
        except Exception:  # noqa: BLE001
            log.debug("position_sizer.funds_fetch_failed")
            return
        if v is not None and float(v) > 0:
            self._value = float(v)
            self._ts = now


def compute_equity(
    session: Session,
    md: "Optional[MarketDataBus]" = None,
    *,
    override: Optional[float] = None,
    live_anchor: Optional[float] = None,
) -> float:
    """Return account equity (starting capital + realised + unrealised).

    `override` short-circuits to that exact value — used by tests and
    backtests that supply a fixed starting capital (and to keep the
    risk engine's existing `portfolio_value=...` contract intact).

    `live_anchor` replaces the *starting-capital + realised* part with
    the broker's real funds balance (which already includes realised
    P&L); only unrealised P&L on open positions is added on top. Used
    in LIVE mode when the Fyers funds feed is available.

    Never returns a non-positive number (that would break sizing).
    """
    settings = get_settings()
    if override is not None:
        return float(override)

    if live_anchor is not None and float(live_anchor) > 0:
        starting = float(live_anchor)
        realised = 0.0  # already inside the broker balance
    else:
        starting = float(settings.PORTFOLIO_VALUE)
        realised = float(
            session.execute(
                select(func.coalesce(func.sum(TradeRow.pnl), 0.0)).where(
                    TradeRow.pnl.is_not(None)
                )
            ).scalar_one()
            or 0.0
        )

    unrealised = 0.0
    rows = session.query(PositionRow).filter(PositionRow.quantity != 0).all()
    for r in rows:
        last = r.last_price
        if (last is None or last <= 0) and md is not None:
            q = md._quotes.get(r.symbol)  # type: ignore[attr-defined]
            if q is not None:
                last = float(q.last_price)
        if last is None or last <= 0:
            last = float(r.average_price)
        unrealised += (float(last) - float(r.average_price)) * int(r.quantity)

    equity = starting + realised + unrealised
    return equity if equity > 0 else max(1.0, starting)


def filled_trade_count(session: Session) -> int:
    """How many trades have filled — drives the graduated-risk ramp."""
    return int(
        session.execute(
            select(func.count(TradeRow.id)).where(TradeRow.status == "filled")
        ).scalar_one()
        or 0
    )


def resolve_risk_pct(
    session: Session,
    *,
    cap_pct: float,
    ramp: bool = True,
    extra_mult: float = 1.0,
) -> float:
    """Effective per-trade risk %, after the graduated ramp, any
    `RiskState` throttle, and an optional volatility-regime multiplier.

    `cap_pct` is the strategy/settings ceiling. While ramping (first
    `RISK_RAMP_TRADES` fills) the result is held down to
    `RISK_RAMP_START_PCT`. A `RiskState.current_risk_pct` (e.g. set to
    0.25 after a weekly-loss breach) always takes the lower value.

    `extra_mult` scales the result after the caps — used for the high-VIX
    throttle (`< 1.0` shrinks risk in a volatile regime). Defaults to 1.0
    (no-op), so the VIX seam is inert until the feed is wired.

    `ramp=False` skips the graduation step (used when an explicit
    portfolio override is supplied, to keep sizing deterministic for
    tests/backtests).
    """
    settings = get_settings()
    base = float(cap_pct)

    if ramp:
        n = filled_trade_count(session)
        if n < int(settings.RISK_RAMP_TRADES):
            base = min(base, float(settings.RISK_RAMP_START_PCT))

    rs = session.get(RiskState, 1)
    if rs is not None and rs.current_risk_pct is not None:
        base = min(base, float(rs.current_risk_pct))

    base = base * max(0.0, float(extra_mult))
    return max(0.0, base)


def notional_cap_qty(
    *, entry: float, equity: float, max_single_position_pct: float
) -> int:
    """Largest qty whose notional (qty * entry) stays within the
    single-name cap. Returns 0 on non-positive inputs."""
    if entry <= 0 or equity <= 0 or max_single_position_pct <= 0:
        return 0
    cap_value = float(equity) * (float(max_single_position_pct) / 100.0)
    return int(math.floor(cap_value / float(entry)))
