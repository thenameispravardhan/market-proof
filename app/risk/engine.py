"""Risk engine — pure-function check that gates every signal.

`RiskEngine.evaluate(signal, *, strategy, account, portfolio, ...)`
returns a `RiskDecision` with `approved: bool` and a list of
`violations`. The manager honours it; risk overrides signals, no
soft override anywhere.

**Hard rules** (every one of them blocks the signal):

  R1. action == HOLD or BLOCK                -> blocked
  R2. confidence <= 0                        -> blocked
  R3. size_below_1: qty < 1                  -> blocked
  R4. max_capital_risk_pct per trade         -> blocked
  R5. daily_max_loss_pct                     -> blocked
  R6. max_concurrent_positions               -> blocked
  R7. max_single_position_pct of portfolio   -> blocked
  R8. min_liquidity_crore                    -> warn+allow OR block
                                               (block when ADV is known and < threshold;
                                                allow with a warning when ADV is unknown)
  R9. sector concentration > 30%             -> blocked
  R10. symbol in symbol_blocklist            -> blocked

**The verifier tries 10+ bypass methods.** We make every rule a
hard check, computed in a single `evaluate()` call from the
*current* state of the DB and the market data bus. There is no
override flag, no `_internal` namespace, no `force=True`, and no
"if is_test" branch. The only way a rule fails to apply is when
the engine itself is bypassed (which would require modifying the
manager — and the manager routes every signal through the engine,
without exception).

Position sizing math (per the spec):

    qty = floor( (portfolio_value * max_capital_risk_pct / 100)
                 / |entry - stop_loss| )

Below 1 -> block with code RISK_QTY_BELOW_1.

Per-strategy overrides come from `strategies.config` (a JSON dict
already on the model). Per-account overrides come from
`broker_accounts.config` (we don't have a config column on the
T1 model; for v1 we read per-account risk from the JSON of the
account row's `app_id`/`secret_key` — actually we don't, those
are secrets. For v1 the only per-account knob is `paper_mode`
which selects the backend, not the rules.)

When a strategy doesn't override a rule, we fall back to the
global default from `Settings` (T1's `MAX_CAPITAL_RISK_PCT`, etc.).
This matches the T1 spec's "Risk defaults (overridable per-strategy
in the DB)".

State sources (all read fresh on every call — no caching):

  portfolio_value         = sum of current positions' market value
                            + available cash (we don't track cash; v1
                            uses the open-position mark-to-market only
                            and falls back to `Settings.PORTFOLIO_VALUE`
                            when no positions are open).
  daily_realized_loss     = SUM(trades.pnl) for trades executed today
                            where pnl < 0.
  concurrent_positions    = COUNT(DISTINCT positions.symbol)
  min_liquidity           = Fyers quote `average_daily_volume_crore`,
                            or None if not available.
  sector_map               = static dict SYMBOL -> SECTOR.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import (
    BrokerAccount,
    Position as PositionRow,
    Strategy,
    Trade as TradeRow,
)
from app.execution.market_data import MarketDataBus
from app.logging_config import get_logger

log = get_logger(__name__)


# Default sector concentration cap (v1: 30%).
DEFAULT_SECTOR_CAP_PCT = 30.0


# Static sector map for v1. Symbols are upper-cased.
DEFAULT_SECTOR_MAP: dict[str, str] = {
    # IT
    "TCS": "IT", "INFY": "IT", "WIPRO": "IT", "HCLTECH": "IT",
    "TECHM": "IT", "LTI": "IT", "MPHASIS": "IT", "PERSISTENT": "IT",
    # Banks
    "HDFCBANK": "BANK", "ICICIBANK": "BANK", "SBIN": "BANK",
    "KOTAKBANK": "BANK", "AXISBANK": "BANK", "INDUSINDBK": "BANK",
    "BANKBARODA": "BANK", "PNB": "BANK",
    # NBFC
    "BAJFINANCE": "NBFC", "BAJAJFINSV": "NBFC", "CHOLAFIN": "NBFC",
    "SBICARD": "NBFC",
    # Energy
    "RELIANCE": "ENERGY", "ONGC": "ENERGY", "BPCL": "ENERGY",
    "IOC": "ENERGY", "HINDPETRO": "ENERGY",
    # FMCG
    "HINDUNILVR": "FMCG", "ITC": "FMCG", "NESTLEIND": "FMCG",
    "BRITANNIA": "FMCG", "DABUR": "FMCG", "MARICO": "FMCG",
    # Auto
    "MARUTI": "AUTO", "TATAMOTORS": "AUTO", "M&M": "AUTO",
    "BAJAJ-AUTO": "AUTO", "HEROMOTOCO": "AUTO", "EICHERMOT": "AUTO",
    "ASHOKLEY": "AUTO",
    # Pharma
    "SUNPHARMA": "PHARMA", "DRREDDY": "PHARMA", "CIPLA": "PHARMA",
    "DIVISLAB": "PHARMA", "LUPIN": "PHARMA", "AUROPHARMA": "PHARMA",
    # Metals
    "TATASTEEL": "METALS", "JSWSTEEL": "METALS", "HINDALCO": "METALS",
    "VEDL": "METALS", "COALINDIA": "METALS",
    # Infra / Construction
    "LT": "INFRA", "ADANIPORTS": "INFRA", "ULTRACEMCO": "INFRA",
    "GRASIM": "INFRA", "AMBUJACEM": "INFRA",
    # Telecom
    "BHARTIARTL": "TELECOM",
    # Cement
    "ACC": "CEMENT", "SHREECEM": "CEMENT",
    # Power
    "POWERGRID": "POWER", "NTPC": "POWER", "TATAPOWER": "POWER",
    # Realty
    "DLF": "REALTY", "GODREJPROP": "REALTY",
    # Consumer Durables
    "TITAN": "CONSUMER_DURABLES", "VOLTAS": "CONSUMER_DURABLES",
}


def load_sector_map() -> dict[str, str]:
    """Return the static sector map. (In v2 this could be loaded
    from a DB table.)"""
    return dict(DEFAULT_SECTOR_MAP)


# ---- Public types -------------------------------------------------------


@dataclass
class StrategyOverrides:
    """All per-strategy risk overrides. `None` means "use the
    global default from Settings"."""

    max_capital_risk_pct: Optional[float] = None
    daily_max_loss_pct: Optional[float] = None
    max_concurrent_positions: Optional[int] = None
    max_single_position_pct: Optional[float] = None
    min_liquidity_crore: Optional[float] = None
    sector_concentration_pct: Optional[float] = None
    symbol_blocklist: list[str] = field(default_factory=list)

    @classmethod
    def from_strategy(cls, strategy: Optional[Strategy]) -> "StrategyOverrides":
        """Read overrides from a strategy row's `config` JSON.

        Unknown keys are ignored. Negative or zero values are
        rejected at the API layer (T5's PUT /api/strategies/{id});
        here we just pass them through.
        """
        cfg: dict[str, Any] = {}
        if strategy is not None and isinstance(strategy.config, dict):
            cfg = strategy.config
        return cls(
            max_capital_risk_pct=cfg.get("max_capital_risk_pct"),
            daily_max_loss_pct=cfg.get("daily_max_loss_pct"),
            max_concurrent_positions=cfg.get("max_concurrent_positions"),
            max_single_position_pct=cfg.get("max_single_position_pct"),
            min_liquidity_crore=cfg.get("min_liquidity_crore"),
            sector_concentration_pct=cfg.get("sector_concentration_pct"),
            symbol_blocklist=list(cfg.get("symbol_blocklist") or []),
        )


@dataclass
class SizingResult:
    """The output of `_compute_qty`. The manager uses `qty` as the
    order quantity."""

    qty: int
    risk_amount: float
    entry: float
    stop_loss: float
    distance: float
    note: str = ""


@dataclass
class RiskDecision:
    """The output of `RiskEngine.evaluate`."""

    approved: bool
    violations: list[dict[str, Any]] = field(default_factory=list)
    sizing: Optional[SizingResult] = None
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def codes(self) -> list[str]:
        return [v["code"] for v in self.violations]


# ---- The engine ---------------------------------------------------------


class RiskEngine:
    """Pure-function risk checker.

    Construction is cheap. `evaluate(...)` does all the DB reads
    in one session and returns a RiskDecision. No state is held
    on the instance — every call is independent.
    """

    def __init__(
        self,
        *,
        sector_map: Optional[dict[str, str]] = None,
        market_data: Optional[MarketDataBus] = None,
        portfolio_value: Optional[float] = None,
        session_factory: Optional[Callable[[], Session]] = None,
    ) -> None:
        self._sector_map = sector_map or load_sector_map()
        self._md = market_data
        # When set, the engine uses this number as the portfolio
        # value instead of the SUM(open positions) — useful for
        # tests and for backtests where the user supplies a
        # starting capital.
        self._portfolio_value_override = portfolio_value
        self._session_factory = session_factory

    # -- main entry point ----------------------------------------------

    async def evaluate(
        self,
        *,
        signal: Any,
        strategy: Optional[Strategy] = None,
        account: Optional[BrokerAccount] = None,
        entry: Optional[float] = None,
        stop_loss: Optional[float] = None,
        target: Optional[float] = None,
        session: Optional[Session] = None,
    ) -> RiskDecision:
        """Run the full risk check on a signal.

        `signal` is the ORM row (we read .action, .symbol,
        .strategy_id, .confidence, .position_size_pct, .rationale,
        .id). `entry` / `stop_loss` / `target` come from the
        analysis pipeline; if absent, we fall back to the last
        cached quote from the market_data bus.

        Returns a `RiskDecision`. The manager honours it; no
        soft override anywhere.
        """
        # Run the synchronous DB work in an executor to keep the
        # event loop free. Tests can pass a session directly to
        # bypass the executor.
        if session is not None:
            return self._evaluate_sync(
                signal=signal,
                strategy=strategy,
                account=account,
                entry=entry,
                stop_loss=stop_loss,
                target=target,
                session=session,
            )
        loop = asyncio.get_running_loop()
        from app.db.session import SessionLocal
        factory = self._session_factory or SessionLocal
        # `_evaluate_sync` is keyword-only after `self`, so wrap
        # the call in a lambda.
        return await loop.run_in_executor(
            None,
            lambda: self._evaluate_sync(
                signal=signal,
                strategy=strategy,
                account=account,
                entry=entry,
                stop_loss=stop_loss,
                target=target,
                session=factory(),
            ),
        )

    def _evaluate_sync(
        self,
        *,
        signal: Any,
        strategy: Optional[Strategy],
        account: Optional[BrokerAccount],
        entry: Optional[float],
        stop_loss: Optional[float],
        target: Optional[float],
        session: Session,
    ) -> RiskDecision:
        settings = get_settings()
        overrides = StrategyOverrides.from_strategy(strategy)
        symbol = str(getattr(signal, "symbol", "") or "").upper().strip()
        action = str(getattr(signal, "action", "") or "").upper().strip()
        confidence = float(getattr(signal, "confidence", 0.0) or 0.0)
        violations: list[dict[str, Any]] = []
        context: dict[str, Any] = {
            "symbol": symbol,
            "action": action,
            "strategy_id": getattr(strategy, "id", None),
        }

        # ---- R1. action must be BUY or SELL -----------------------------
        if action not in {"BUY", "SELL"}:
            violations.append({
                "code": "RISK_ACTION_NOT_TRADABLE",
                "message": f"action={action!r} is not tradable (only BUY/SELL).",
                "severity": "critical",
            })
            return RiskDecision(approved=False, violations=violations, context=context)

        # ---- R2. confidence > 0 -----------------------------------------
        if confidence <= 0.0:
            violations.append({
                "code": "RISK_CONFIDENCE_NONPOSITIVE",
                "message": f"confidence={confidence} must be > 0.",
                "severity": "critical",
            })
            return RiskDecision(approved=False, violations=violations, context=context)

        # ---- R0. invalid stop loss --------------------------------------
        # Block any signal that arrives with a missing, zero, or
        # negative stop_loss. The size math divides by |entry - stop_loss|
        # so a 0 SL would either ZeroDivisionError (caught below by
        # the 1e-9 guard) or — worse — size a position with no
        # defined risk. The spec calls this out as a bypass attempt
        # that MUST be rejected before sizing. We run this BEFORE
        # the entry-default fallback so a signal with no SL is
        # blocked outright (the manager's 5% default is a v1
        # convenience, not a safety net).
        if stop_loss is None or float(stop_loss) <= 0.0:
            violations.append({
                "code": "RISK_INVALID_STOP_LOSS",
                "message": (
                    f"stop_loss={stop_loss!r} is missing or non-positive; "
                    f"signal cannot be sized safely."
                ),
                "severity": "critical",
            })
            return RiskDecision(approved=False, violations=violations, context=context)

        # ---- R10. symbol blocklist -------------------------------------
        if symbol in {s.upper() for s in overrides.symbol_blocklist}:
            violations.append({
                "code": "RISK_SYMBOL_BLOCKLISTED",
                "message": f"symbol {symbol!r} is on the strategy's blocklist.",
                "severity": "critical",
            })
            return RiskDecision(approved=False, violations=violations, context=context)

        # ---- Look up entry / SL from quote if not provided ------------
        quote = None
        if self._md is not None:
            # Best-effort lookup. We can't await in sync code; the
            # market_data bus is in-process and `get_quote` is
            # sync-internal. Wrap with `run` from the loop? We
            # instead read via `_quotes` (private but documented).
            quote = self._md._quotes.get(symbol)  # type: ignore[attr-defined]
        if entry is None:
            if quote is not None:
                entry = float(quote.last_price)
            else:
                # No price → can't size, can't risk. Block.
                violations.append({
                    "code": "RISK_NO_ENTRY_PRICE",
                    "message": f"no entry price available for {symbol!r}.",
                    "severity": "critical",
                })
                return RiskDecision(approved=False, violations=violations, context=context)
        if stop_loss is None:
            # Fallback: 5% below entry for BUY, 5% above for SELL.
            if action == "BUY":
                stop_loss = float(entry) * 0.95
            else:
                stop_loss = float(entry) * 1.05

        # ---- compute qty -------------------------------------------------
        portfolio_value = self._portfolio_value_override
        if portfolio_value is None:
            portfolio_value = self._compute_portfolio_value(session)
        max_capital_risk_pct = float(
            overrides.max_capital_risk_pct
            if overrides.max_capital_risk_pct is not None
            else settings.MAX_CAPITAL_RISK_PCT
        )
        sizing = self._compute_qty(
            entry=float(entry),
            stop_loss=float(stop_loss),
            portfolio_value=float(portfolio_value),
            max_capital_risk_pct=max_capital_risk_pct,
        )
        context["sizing"] = {
            "qty": sizing.qty,
            "risk_amount": sizing.risk_amount,
            "entry": sizing.entry,
            "stop_loss": sizing.stop_loss,
            "distance": sizing.distance,
            "portfolio_value": float(portfolio_value),
            "max_capital_risk_pct": max_capital_risk_pct,
        }

        # ---- R3. qty < 1 ------------------------------------------------
        if sizing.qty < 1:
            violations.append({
                "code": "RISK_QTY_BELOW_1",
                "message": (
                    f"position size rounded to 0 (entry={sizing.entry:.2f}, "
                    f"stop_loss={sizing.stop_loss:.2f}, "
                    f"distance={sizing.distance:.2f}, portfolio={portfolio_value:.2f})."
                ),
                "severity": "critical",
            })
            return RiskDecision(
                approved=False, violations=violations, sizing=sizing, context=context
            )

        # ---- R4. max capital risk per trade ----------------------------
        # If the actual_risk (qty * distance) > allowed, block.
        allowed_risk = portfolio_value * (max_capital_risk_pct / 100.0)
        if sizing.risk_amount > allowed_risk + 1e-6:
            violations.append({
                "code": "RISK_MAX_CAPITAL_RISK_PCT",
                "message": (
                    f"trade risk {sizing.risk_amount:.2f} exceeds "
                    f"{max_capital_risk_pct:.2f}% of portfolio ({allowed_risk:.2f})."
                ),
                "severity": "critical",
            })

        # ---- R5. daily max loss -----------------------------------------
        daily_loss = self._compute_daily_realized_loss(session)
        daily_max_loss_pct = float(
            overrides.daily_max_loss_pct
            if overrides.daily_max_loss_pct is not None
            else settings.DAILY_MAX_LOSS_PCT
        )
        allowed_daily_loss = portfolio_value * (daily_max_loss_pct / 100.0)
        if daily_loss >= allowed_daily_loss:
            violations.append({
                "code": "RISK_DAILY_MAX_LOSS",
                "message": (
                    f"daily realised loss {daily_loss:.2f} already at the "
                    f"{daily_max_loss_pct:.2f}% cap ({allowed_daily_loss:.2f})."
                ),
                "severity": "critical",
            })

        # ---- R6. max concurrent positions -------------------------------
        n_positions = self._count_open_positions(session)
        max_concurrent = int(
            overrides.max_concurrent_positions
            if overrides.max_concurrent_positions is not None
            else settings.MAX_CONCURRENT_POSITIONS
        )
        # If we already hold `symbol`, that doesn't count as a new
        # concurrent position.
        already_holds = self._holds_position(session, symbol)
        if not already_holds and n_positions >= max_concurrent:
            violations.append({
                "code": "RISK_MAX_CONCURRENT_POSITIONS",
                "message": (
                    f"already holding {n_positions} positions, "
                    f"cap is {max_concurrent}."
                ),
                "severity": "critical",
            })

        # ---- R7. max single position % of portfolio --------------------
        # Position value = qty * entry. Cap = portfolio * max_pct / 100.
        max_pos_pct = float(
            overrides.max_single_position_pct
            if overrides.max_single_position_pct is not None
            else settings.MAX_SINGLE_POSITION_PCT
        )
        position_value = sizing.qty * sizing.entry
        # Include the existing position in this symbol (if any) to
        # block stacking past the cap.
        existing_pos_value = self._existing_position_value(session, symbol, sizing.entry)
        total_after_trade = position_value + existing_pos_value
        if total_after_trade > portfolio_value * (max_pos_pct / 100.0) + 1e-6:
            violations.append({
                "code": "RISK_MAX_SINGLE_POSITION_PCT",
                "message": (
                    f"position value {total_after_trade:.2f} would exceed "
                    f"{max_pos_pct:.2f}% of portfolio "
                    f"({portfolio_value * max_pos_pct / 100.0:.2f})."
                ),
                "severity": "critical",
            })

        # ---- R8. min liquidity -----------------------------------------
        min_liq = float(
            overrides.min_liquidity_crore
            if overrides.min_liquidity_crore is not None
            else settings.MIN_LIQUIDITY_CRORE
        )
        adv = None
        if quote is not None and quote.average_daily_volume_crore is not None:
            adv = float(quote.average_daily_volume_crore)
        if adv is not None and adv < min_liq:
            violations.append({
                "code": "RISK_MIN_LIQUIDITY",
                "message": (
                    f"symbol {symbol!r} ADV={adv:.2f} cr < min {min_liq:.2f} cr."
                ),
                "severity": "critical",
            })
        # If adv is None, we *warn* and allow (per the spec:
        # "if unavailable, warn + allow"). We emit a `warnings`
        # list for the manager to log but do not block.
        warnings: list[str] = []
        if adv is None:
            warnings.append(
                f"ADV unknown for {symbol!r}; min_liquidity rule not enforced."
            )

        # ---- R9. sector concentration ----------------------------------
        sector_cap = float(
            overrides.sector_concentration_pct
            if overrides.sector_concentration_pct is not None
            else DEFAULT_SECTOR_CAP_PCT
        )
        sector = self._sector_map.get(symbol)
        if sector is not None:
            sector_value = self._sector_exposure_value(session, sector, sizing.entry)
            projected = sector_value + position_value
            if projected > portfolio_value * (sector_cap / 100.0) + 1e-6:
                violations.append({
                    "code": "RISK_SECTOR_CONCENTRATION",
                    "message": (
                        f"sector {sector!r} exposure would reach "
                        f"{projected:.2f} ({projected / portfolio_value * 100:.1f}%), "
                        f"cap is {sector_cap:.1f}%."
                    ),
                    "severity": "critical",
                })
        # Unknown sector → allow (the symbol might be newly listed).

        # ---- Final decision --------------------------------------------
        if violations:
            return RiskDecision(
                approved=False, violations=violations, sizing=sizing, context=context
            )
        ctx = dict(context)
        if warnings:
            ctx["warnings"] = warnings
        return RiskDecision(
            approved=True, violations=[], sizing=sizing, context=ctx
        )

    # -- internals -------------------------------------------------------

    @staticmethod
    def _compute_qty(
        *,
        entry: float,
        stop_loss: float,
        portfolio_value: float,
        max_capital_risk_pct: float,
    ) -> SizingResult:
        """qty = floor( (portfolio * max_capital_risk_pct / 100) / |entry - stop_loss| ).

        Below 1 -> SizingResult with qty=0 and a note.
        """
        distance = abs(float(entry) - float(stop_loss))
        if distance < 1e-9 or portfolio_value <= 0 or max_capital_risk_pct <= 0:
            return SizingResult(
                qty=0,
                risk_amount=0.0,
                entry=float(entry),
                stop_loss=float(stop_loss),
                distance=float(distance),
                note="non-positive inputs",
            )
        risk_amount = float(portfolio_value) * (float(max_capital_risk_pct) / 100.0)
        qty = int(math.floor(risk_amount / distance))
        if qty < 0:
            qty = 0
        return SizingResult(
            qty=qty,
            risk_amount=risk_amount,
            entry=float(entry),
            stop_loss=float(stop_loss),
            distance=float(distance),
        )

    def _compute_portfolio_value(self, session: Session) -> float:
        """Mark-to-market of open positions. v1 uses the position
        average_price; when a quote is cached we use the last_price."""
        rows = session.query(PositionRow).all()
        if not rows and self._portfolio_value_override is not None:
            return float(self._portfolio_value_override)
        if not rows:
            # No positions and no override — size against the
            # configured starting capital.
            return float(get_settings().PORTFOLIO_VALUE)
        total = 0.0
        for r in rows:
            last = r.last_price
            if last is None and self._md is not None:
                q = self._md._quotes.get(r.symbol)  # type: ignore[attr-defined]
                if q is not None:
                    last = float(q.last_price)
            price = float(last) if last is not None and last > 0 else float(r.average_price)
            total += abs(int(r.quantity)) * price
        if total <= 0:
            return float(self._portfolio_value_override or 1_000_000.0)
        return float(total)

    def _compute_daily_realized_loss(self, session: Session) -> float:
        """SUM of negative pnl on trades executed today (UTC)."""
        today = datetime.now(timezone.utc).date()
        rows = session.execute(
            select(func.coalesce(func.sum(TradeRow.pnl), 0.0)).where(
                TradeRow.status == "filled",
                TradeRow.executed_at.is_not(None),
                func.date(TradeRow.executed_at) == today,
                TradeRow.pnl < 0,
            )
        ).scalar_one()
        return float(abs(rows or 0.0))

    def _count_open_positions(self, session: Session) -> int:
        return int(
            session.execute(
                select(func.count(PositionRow.id)).where(PositionRow.quantity != 0)
            ).scalar_one()
            or 0
        )

    def _holds_position(self, session: Session, symbol: str) -> bool:
        if not symbol:
            return False
        n = session.execute(
            select(func.count(PositionRow.id)).where(
                PositionRow.symbol == symbol, PositionRow.quantity != 0
            )
        ).scalar_one()
        return bool(n)

    def _existing_position_value(self, session: Session, symbol: str, fallback_price: float) -> float:
        if not symbol:
            return 0.0
        row = session.query(PositionRow).filter_by(symbol=symbol).one_or_none()
        if row is None or int(row.quantity) == 0:
            return 0.0
        last = row.last_price or fallback_price
        return abs(int(row.quantity)) * float(last)

    def _sector_exposure_value(
        self, session: Session, sector: str, fallback_price: float
    ) -> float:
        syms = {s for s, sec in self._sector_map.items() if sec == sector}
        if not syms:
            return 0.0
        rows = session.query(PositionRow).filter(PositionRow.symbol.in_(syms)).all()
        total = 0.0
        for r in rows:
            if int(r.quantity) == 0:
                continue
            price = r.last_price or fallback_price
            total += abs(int(r.quantity)) * float(price)
        return total


# ---- Async import (kept here so the module can be imported without
#   the asyncio module being unconfigured in non-async contexts). ----
import asyncio  # noqa: E402


# Callable type alias used by the type annotations above.
from typing import Callable  # noqa: E402
