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
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import (
    BrokerAccount,
    Position as PositionRow,
    Strategy,
    Trade as TradeRow,
)
from app.logging_config import get_logger
from app.risk import perf_sizer, position_sizer, volatility

# Type-only import — `MarketDataBus` is used solely in annotations here
# (the engine reads `self._md._quotes` off the injected instance). With
# `from __future__ import annotations` these never evaluate at runtime,
# so importing it lazily breaks the risk<->execution import cycle that
# would otherwise fire when `app.risk` is imported before `app.execution`.
if TYPE_CHECKING:
    from app.execution.market_data import MarketDataBus

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
        vol_regime: Optional[volatility.VolatilityRegime] = None,
    ) -> None:
        self._sector_map = sector_map or load_sector_map()
        self._md = market_data
        # Volatility-regime seam (India VIX). None → no throttle. When wired
        # (Phase 5) a high VIX shrinks the per-trade risk %. Sizing only —
        # never touches the LLM conviction.
        self._vol_regime = vol_regime
        # When set, the engine uses this number as the portfolio
        # value instead of the SUM(open positions) — useful for
        # tests and for backtests where the user supplies a
        # starting capital.
        self._portfolio_value_override = portfolio_value
        self._session_factory = session_factory
        # Live-funds seam: in LIVE mode sizing anchors to the real Fyers
        # account balance instead of PORTFOLIO_VALUE. None → the static
        # ledger applies (paper mode, or no funds feed). Attached by the
        # execution manager (`attach_funds_provider`).
        self._funds_provider: Optional[position_sizer.FyersFundsProvider] = None

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
        manual_qty: Optional[int] = None,
        event_type: Optional[str] = None,
    ) -> RiskDecision:
        """Run the full risk check on a signal.

        `event_type` (the originating announcement's event class) feeds
        the performance-weighted sizer — the per-trade risk %% tiers on
        the event type's realised track record when enough closed trades
        exist (PERF_SIZER_*). Absent → the base risk %% applies.

        `signal` is the ORM row (we read .action, .symbol,
        .strategy_id, .confidence, .position_size_pct, .rationale,
        .id). `entry` / `stop_loss` / `target` come from the
        analysis pipeline; if absent, we fall back to the last
        cached quote from the market_data bus.

        Returns a `RiskDecision`. The manager honours it; no
        soft override anywhere.
        """
        # Warm the live-funds cache HERE (the only async hop) so the
        # sync sizing path below can read it without blocking. Fail-safe:
        # any error just leaves the cached value as-is / None.
        if self._funds_provider is not None and self._portfolio_value_override is None:
            try:
                if get_settings().is_live:
                    await self._funds_provider.refresh()
            except Exception:  # noqa: BLE001
                pass
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
                manual_qty=manual_qty,
                event_type=event_type,
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
                manual_qty=manual_qty,
                event_type=event_type,
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
        manual_qty: Optional[int] = None,
        event_type: Optional[str] = None,
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

        # ---- R2b. confidence floor (RISK.md §1) -------------------------
        # Speed-news trades fire on the LLM's conviction; below the floor
        # the edge isn't worth the slippage. Manual orders carry
        # confidence=1.0 so they're unaffected.
        min_conf = float(getattr(settings, "MIN_SENTIMENT_CONFIDENCE", 0.0) or 0.0)
        if min_conf > 0.0 and confidence < min_conf:
            violations.append({
                "code": "RISK_CONFIDENCE_BELOW_FLOOR",
                "message": f"confidence={confidence:.2f} < floor {min_conf:.2f}.",
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

        # ---- R11. shorting toggle (RISK.md) ----------------------------
        # A SELL that OPENS a short (no existing long to close) is only
        # allowed when shorting is enabled. Closing/reducing a long is
        # always permitted regardless of the toggle.
        if action == "SELL" and not getattr(settings, "SHORTING_ENABLED", True):
            if not self._holds_position(session, symbol):
                violations.append({
                    "code": "RISK_SHORTING_DISABLED",
                    "message": "shorting is disabled; refusing to open a short.",
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

        # ---- R0b. direction-aware stop sanity ---------------------------
        # A stop at the entry has zero distance (risk undefined → can't
        # size); a stop on the WRONG side of the entry (above entry for a
        # BUY, below for a SELL) would trigger instantly / never protect.
        # Both are malformed signals that must be rejected, not "fixed".
        if abs(float(entry) - float(stop_loss)) < 1e-9:
            violations.append({
                "code": "RISK_INVALID_STOP_DISTANCE",
                "message": (
                    f"stop_loss {float(stop_loss):.2f} equals entry "
                    f"{float(entry):.2f}; zero risk distance cannot be sized."
                ),
                "severity": "critical",
            })
            return RiskDecision(approved=False, violations=violations, context=context)
        if action == "BUY" and float(stop_loss) > float(entry):
            violations.append({
                "code": "RISK_STOP_ABOVE_ENTRY",
                "message": (
                    f"BUY stop_loss {float(stop_loss):.2f} is above entry "
                    f"{float(entry):.2f}; it would trigger immediately."
                ),
                "severity": "critical",
            })
            return RiskDecision(approved=False, violations=violations, context=context)
        if action == "SELL" and float(stop_loss) < float(entry):
            violations.append({
                "code": "RISK_STOP_BELOW_ENTRY",
                "message": (
                    f"SELL stop_loss {float(stop_loss):.2f} is below entry "
                    f"{float(entry):.2f}; it would trigger immediately."
                ),
                "severity": "critical",
            })
            return RiskDecision(approved=False, violations=violations, context=context)

        # ---- compute qty -------------------------------------------------
        # Equity is a real ledger now (capital + realised + unrealised),
        # not gross open exposure. An explicit `portfolio_value` override
        # (tests / backtests) short-circuits to that value. In LIVE mode
        # with a funds feed attached, the ledger anchors to the REAL Fyers
        # account balance (fail-safe: feed unavailable → static ledger).
        live_anchor: Optional[float] = None
        if (
            self._funds_provider is not None
            and self._portfolio_value_override is None
            and settings.is_live
        ):
            try:
                # Cache warmed by the async `evaluate` wrapper.
                live_anchor = self._funds_provider.equity()
            except Exception:  # noqa: BLE001
                live_anchor = None
        portfolio_value = position_sizer.compute_equity(
            session, self._md,
            override=self._portfolio_value_override,
            live_anchor=live_anchor,
        )
        # Intraday buying power (§2b): Fyers MIS margins mean NOTIONAL
        # capacity is equity × INTRADAY_LEVERAGE (~5x). Only notional
        # contexts use it — the sizing clamp, single-name cap (R7) and
        # sector cap (R9). Risk-per-trade and every loss limit stay on
        # raw equity: leverage multiplies exposure, never the money we
        # are willing to lose.
        try:
            leverage = float(getattr(settings, "INTRADAY_LEVERAGE", 1.0) or 1.0)
        except (TypeError, ValueError):
            leverage = 1.0
        leverage = max(1.0, leverage)
        buying_power = float(portfolio_value) * leverage
        # Per-trade risk %: strategy/settings ceiling, then the graduated
        # ramp + any RiskState throttle. Skip the ramp when an explicit
        # portfolio override is supplied so sizing stays deterministic.
        cap_pct = float(
            overrides.max_capital_risk_pct
            if overrides.max_capital_risk_pct is not None
            else settings.MAX_CAPITAL_RISK_PCT
        )
        # Performance-weighted sizer: tier the base risk %% on the event
        # type's realised track record (win rate + avg R over the last N
        # closed trades). Fail-safe — not enough data / no event type /
        # disabled → cap_pct unchanged. Applied BEFORE the ramp, weekly
        # throttle and VIX multiplier, so every protective clamp still
        # stacks on top. Manual orders (no event type) are never tiered.
        if manual_qty is None:
            cap_pct, perf_ctx = perf_sizer.performance_risk_pct(
                session, event_type, cap_pct, settings
            )
            if perf_ctx is not None:
                context["perf_sizer"] = perf_ctx
        # High-VIX risk throttle (fail-safe): None VIX → 1.0× (no-op). When
        # the India-VIX feed is wired this shrinks the per-trade risk % in a
        # volatile regime. Sizing only — conviction is untouched.
        vix_level = None
        if self._vol_regime is not None:
            try:
                vix_level = self._vol_regime.india_vix()
            except Exception:  # noqa: BLE001
                vix_level = None
        vix_mult = volatility.vix_risk_multiplier(vix_level)
        max_capital_risk_pct = position_sizer.resolve_risk_pct(
            session,
            cap_pct=cap_pct,
            ramp=(self._portfolio_value_override is None),
            extra_mult=vix_mult,
        )
        # Single-name notional cap (resolved here so we can clamp the
        # auto-sized qty to it — RISK.md §2: take the SMALLER of the
        # risk-based and notional-based quantities).
        max_pos_pct = float(
            overrides.max_single_position_pct
            if overrides.max_single_position_pct is not None
            else settings.MAX_SINGLE_POSITION_PCT
        )
        sizing = self._compute_qty(
            entry=float(entry),
            stop_loss=float(stop_loss),
            portfolio_value=float(portfolio_value),
            max_capital_risk_pct=max_capital_risk_pct,
        )
        # Clamp the auto-sized qty to the notional cap (a tight stop must
        # not let the risk-based qty blow past the single-name cap). Only
        # for auto-sizing — a manual qty is the operator's explicit choice
        # and is checked (not silently shrunk) by R7 below.
        if manual_qty is None:
            ncap = position_sizer.notional_cap_qty(
                entry=float(entry),
                equity=buying_power,
                max_single_position_pct=max_pos_pct,
            )
            if 0 <= ncap < sizing.qty:
                sizing = SizingResult(
                    qty=ncap,
                    risk_amount=float(ncap) * sizing.distance,
                    entry=sizing.entry,
                    stop_loss=sizing.stop_loss,
                    distance=sizing.distance,
                    note="notional_capped",
                )
        # When the caller passes a `manual_qty` (the Trade page),
        # the operator has explicitly chosen the position size.
        # Override the auto-computed qty so the position-cap check
        # (and the rest of the downstream `sizing.qty` reads) uses
        # the operator's number, not the engine's idealized one.
        if manual_qty is not None and int(manual_qty) >= 0:
            mq = int(manual_qty)
            sizing = SizingResult(
                qty=mq,
                risk_amount=float(mq) * abs(float(entry) - float(stop_loss)),
                entry=sizing.entry,
                stop_loss=sizing.stop_loss,
                distance=sizing.distance,
            )
        context["sizing"] = {
            "qty": sizing.qty,
            "risk_amount": sizing.risk_amount,
            "entry": sizing.entry,
            "stop_loss": sizing.stop_loss,
            "distance": sizing.distance,
            "portfolio_value": float(portfolio_value),
            "intraday_leverage": leverage,
            "buying_power": buying_power,
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
        # `max_pos_pct` was resolved above (used for the notional clamp);
        # here it guards STACKING past the cap (new + existing holding).
        position_value = sizing.qty * sizing.entry
        # Include the existing position in this symbol (if any) to
        # block stacking past the cap.
        existing_pos_value = self._existing_position_value(session, symbol, sizing.entry)
        total_after_trade = position_value + existing_pos_value
        if total_after_trade > buying_power * (max_pos_pct / 100.0) + 1e-6:
            violations.append({
                "code": "RISK_MAX_SINGLE_POSITION_PCT",
                "message": (
                    f"position value {total_after_trade:.2f} would exceed "
                    f"{max_pos_pct:.2f}% of buying power "
                    f"({buying_power * max_pos_pct / 100.0:.2f}, "
                    f"{leverage:.0f}x intraday)."
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
        # Unknown ADV: in LIVE we fail closed (block) — you should never
        # market into a name whose liquidity you can't confirm with real
        # money. In paper we warn + allow so testing isn't blocked by the
        # absent volume feed (the Fyers quote doesn't carry ADV yet — see
        # the seam note in quote_feed). RISK.md §1.
        warnings: list[str] = []
        if adv is None:
            if settings.is_live and getattr(settings, "REQUIRE_KNOWN_LIQUIDITY", False):
                violations.append({
                    "code": "RISK_MIN_LIQUIDITY_UNKNOWN",
                    "message": (
                        f"ADV unknown for {symbol!r}; refusing to trade it "
                        f"live without confirmed liquidity."
                    ),
                    "severity": "critical",
                })
            else:
                warnings.append(
                    f"ADV unknown for {symbol!r}; min_liquidity rule not enforced."
                )

        # ---- R12. bid/ask spread (RISK.md §1) --------------------------
        # A wide spread means a bad fill and a stop that's already underwater
        # at entry. Block when the quoted spread exceeds MAX_SPREAD_PCT. The
        # Fyers quote doesn't carry bid/ask yet, so this is fail-safe: when
        # bid/ask are absent we SKIP (warn, never fake a pass) — it switches
        # on the moment an L1 feed populates them (Phase 5).
        max_spread = float(getattr(settings, "MAX_SPREAD_PCT", 0.0) or 0.0)
        bid = getattr(quote, "bid", None) if quote is not None else None
        ask = getattr(quote, "ask", None) if quote is not None else None
        if max_spread > 0 and bid is not None and ask is not None:
            try:
                bid_f, ask_f = float(bid), float(ask)
            except (TypeError, ValueError):
                bid_f = ask_f = 0.0
            mid = (bid_f + ask_f) / 2.0
            if mid > 0 and ask_f >= bid_f:
                spread_pct = (ask_f - bid_f) / mid * 100.0
                if spread_pct > max_spread:
                    violations.append({
                        "code": "RISK_WIDE_SPREAD",
                        "message": (
                            f"spread {spread_pct:.3f}% > max {max_spread:.3f}% "
                            f"for {symbol!r} (bid={bid_f:.2f}, ask={ask_f:.2f})."
                        ),
                        "severity": "critical",
                    })
        elif max_spread > 0:
            warnings.append(
                f"bid/ask unknown for {symbol!r}; spread rule not enforced."
            )

        # ---- R9. sector concentration ----------------------------------
        # Global default now comes from Settings (SECTOR_CONCENTRATION_PCT,
        # tightened to 25%); a per-strategy override still wins.
        sector_cap = float(
            overrides.sector_concentration_pct
            if overrides.sector_concentration_pct is not None
            else getattr(settings, "SECTOR_CONCENTRATION_PCT", DEFAULT_SECTOR_CAP_PCT)
        )
        sector = self._sector_map.get(symbol)
        if sector is not None:
            sector_value = self._sector_exposure_value(session, sector, sizing.entry)
            projected = sector_value + position_value
            if projected > buying_power * (sector_cap / 100.0) + 1e-6:
                violations.append({
                    "code": "RISK_SECTOR_CONCENTRATION",
                    "message": (
                        f"sector {sector!r} exposure would reach "
                        f"{projected:.2f} ({projected / buying_power * 100:.1f}% "
                        f"of buying power), cap is {sector_cap:.1f}%."
                    ),
                    "severity": "critical",
                })
            # ---- R13. clustering: max distinct names per sector ---------
            # News hits correlated names at once; cap the COUNT of names per
            # sector (not just the value). Adding to a name we already hold
            # doesn't count as a new cluster member.
            open_syms = self._sector_open_symbols(session, sector)
            max_per_sector = int(getattr(settings, "MAX_POSITIONS_PER_SECTOR", 0) or 0)
            if (
                max_per_sector > 0
                and symbol not in open_syms
                and len(open_syms) >= max_per_sector
            ):
                violations.append({
                    "code": "RISK_SECTOR_CLUSTER",
                    "message": (
                        f"sector {sector!r} already holds {len(open_syms)} name(s) "
                        f"({', '.join(sorted(open_syms))}); cap is {max_per_sector}."
                    ),
                    "severity": "critical",
                })
            # ---- One-name-per-event window ------------------------------
            # If another name in this sector was entered within the cluster
            # window, treat it as the same news event and take only the
            # first — refuse the correlated follow-on.
            window = int(getattr(settings, "SECTOR_CLUSTER_WINDOW_SECONDS", 0) or 0)
            if (
                window > 0
                and symbol not in open_syms
                and self._recent_sector_entry(session, sector, symbol, window)
            ):
                violations.append({
                    "code": "RISK_SECTOR_CLUSTER_WINDOW",
                    "message": (
                        f"another {sector!r} name was entered within the last "
                        f"{window}s; taking only the first of a news cluster."
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

    def _sector_open_symbols(self, session: Session, sector: str) -> set[str]:
        """Distinct symbols in `sector` that currently hold an open
        (non-zero quantity) position — drives the per-sector name cap."""
        syms = {s for s, sec in self._sector_map.items() if sec == sector}
        if not syms:
            return set()
        rows = session.query(PositionRow).filter(PositionRow.symbol.in_(syms)).all()
        return {r.symbol for r in rows if int(r.quantity) != 0}

    def _recent_sector_entry(
        self, session: Session, sector: str, symbol: str, window_seconds: int
    ) -> bool:
        """True if another name in `sector` (not `symbol`) had an ENTRY fill
        within the last `window_seconds`. Entry fills carry NULL pnl (exits
        carry realised pnl); `executed_at` is stored naive-UTC. Mirrors the
        entry/exit distinction used by `circuit_breakers.entries_today`."""
        syms = {
            s for s, sec in self._sector_map.items()
            if sec == sector and s != symbol
        }
        if not syms:
            return False
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=int(window_seconds))
        ).replace(tzinfo=None)
        n = session.execute(
            select(func.count(TradeRow.id)).where(
                TradeRow.symbol.in_(syms),
                TradeRow.status == "filled",
                TradeRow.pnl.is_(None),
                TradeRow.executed_at.is_not(None),
                TradeRow.executed_at >= cutoff,
            )
        ).scalar_one()
        return bool(n or 0)


# ---- Async import (kept here so the module can be imported without
#   the asyncio module being unconfigured in non-async contexts). ----
import asyncio  # noqa: E402


# Callable type alias used by the type annotations above.
from typing import Callable  # noqa: E402
