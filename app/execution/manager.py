"""Execution manager: subscribes to `signals.new`, runs the risk
engine, and routes approved signals to the right TradingBackend.

Architecture (one manager per process):

    signals.new --> Manager._handle_signal
        1. Load the signal + its strategy + the strategy's
           broker_accounts row (the routing key).
        2. Pick the backend for that account:
             - if account.paper_mode or TRADING_MODE=='paper'
               -> PaperBackend
             - else -> FyersLiveBackend with that account's
               app_id / access_token
        3. Look up entry / stop_loss / target. v1: parse the
           rationale string that T3's service wrote (format:
           "entry=X sl=Y target=Z rr=W"). If absent, fall back
           to the last cached quote from the MarketDataBus.
        4. Run the RiskEngine. On block, write a `risk_events`
           row, mark `signals.approved=False`, persist a blocked
           `trades` row, and publish `risk.blocked`. **DO NOT
           place an order.**
        5. On approve, call `backend.place_order` and persist
           a `trades` row with status from the OrderResult
           (PENDING / FILLED / REJECTED / etc.).
        6. Publish `trade.executed` for the UI relay.

Multi-account routing:

    strategies.broker_account_id  -> int (FK to broker_accounts)
    broker_accounts.paper_mode    -> bool (per-account override)
    Settings.TRADING_MODE         -> "paper" | "live" (global kill-switch)

The manager caches one backend per `broker_accounts.id`. When the
account's access_token is rotated (Fyers OAuth callback writes a
new token), the manager's FyersLiveBackend is re-injected with
the new token via `set_access_token`.

Hot reload:

    On `settings.updated` the manager clears its in-memory cache
    of backends that are paper-mode-coupled to TRADING_MODE, so
    the next signal picks up the new global mode.

Lifecycle:

    mgr = Manager(risk_engine=..., market_data=...)
    task = mgr.start()            # subscribes to signals.new
    ...
    mgr.stop()
    await mgr.wait_until_stopped()
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analyzer.service import CHANNEL_NEW_SIGNAL
from app.config import Settings, get_settings
from app.db.models import (
    Analysis,
    Announcement,
    AuditLog,
    BrokerAccount,
    Position as PositionRow,
    RiskEvent,
    Signal,
    Strategy,
    Trade as TradeRow,
)
from app.execution.base import (
    OrderResult,
    OrderSide,
    OrderState,
    OrderType,
    ProductType,
    TradingBackend,
)
from app.execution.fyers_live import FyersLiveBackend
from app.execution.market_data import MarketDataBus
from app.execution.paper import PaperBackend
from app.logging_config import get_logger
from app.risk import event_profiles, volatility
from app.risk.engine import RiskEngine
from app.services.event_bus import event_bus

log = get_logger(__name__)


# Channels the manager publishes on.
CHANNEL_TRADE_EXECUTED = "trade.executed"
CHANNEL_RISK_BLOCKED = "risk.blocked"
CHANNEL_SETTINGS_UPDATED = "settings.updated"
# Published by the Fyers OAuth callback after a successful
# token rotation. Payload: {"broker_account_id": int}. The
# execution manager subscribes to this so it can drop its
# cached FyersLiveBackend (which is built with the OLD
# app_id + access_token) and rebuild on the next signal —
# the FyersLiveBackend constructor reads the row from the DB
# on first use, so this guarantees the new credentials are
# the ones that hit the broker.
CHANNEL_BROKER_TOKEN_ROTATED = "broker.token_rotated"


# Regex to pull entry / sl / target / rr out of T3's rationale
# string ("... entry=X sl=Y target=Z rr=W ...").
_RATIONALE_RE = re.compile(
    r"entry=(?P<entry>-?\d+(?:\.\d+)?)\s+sl=(?P<sl>-?\d+(?:\.\d+)?)\s+"
    r"target=(?P<target>-?\d+(?:\.\d+)?)\s+rr=(?P<rr>-?\d+(?:\.\d+)?)"
)


# ---- Helpers ------------------------------------------------------------


def parse_rationale_levels(rationale: Optional[str]) -> dict[str, float]:
    """Extract entry / sl / target / rr from T3's rationale string.

    Returns an empty dict when the rationale doesn't match the
    expected pattern (older rows, custom rationales, etc.).
    """
    if not rationale or not isinstance(rationale, str):
        return {}
    m = _RATIONALE_RE.search(rationale)
    if not m:
        return {}
    try:
        return {
            "entry": float(m.group("entry")),
            "stop_loss": float(m.group("sl")),
            "target": float(m.group("target")),
            "rr": float(m.group("rr")),
        }
    except (TypeError, ValueError):
        return {}


def _default_session_factory() -> Callable[[], Session]:
    from app.db.session import SessionLocal
    return SessionLocal


def _check_entry_breakers(
    session_factory: Callable[[], Session], confidence: float
) -> tuple[bool, Optional[str]]:
    """Open a session and ask the circuit breakers whether a new entry is
    allowed (kill-switch, daily/monthly halt, cooldown, daily trade cap)."""
    from app.risk import circuit_breakers

    with session_factory() as session:
        return circuit_breakers.entry_allowed(session, confidence=confidence)


# ---- The manager --------------------------------------------------------


class Manager:
    """Owns the event-bus loop and the per-broker-backend cache.

    Construction is cheap. Call `start()` to begin subscribing to
    `signals.new`; call `stop()` to cancel the loop.

    `risk_engine` and `market_data` are constructor-injected so
    tests can pass fakes. `backends` is an optional dict that
    lets tests pre-seed a backend (e.g. a stub) per
    `broker_account_id` — when a strategy's account has a backend
    in this map, the manager uses it instead of building a
    PaperBackend or FyersLiveBackend.
    """

    def __init__(
        self,
        *,
        risk_engine: Optional[RiskEngine] = None,
        market_data: Optional[MarketDataBus] = None,
        session_factory: Optional[Callable[[], Session]] = None,
        backends: Optional[dict[int, TradingBackend]] = None,
        paper_backend: Optional[PaperBackend] = None,
        settings_provider: Optional[Callable[[], Settings]] = None,
        vol_provider: Optional[volatility.VolatilityProvider] = None,
        vol_regime: Optional[volatility.VolatilityRegime] = None,
    ) -> None:
        self._risk = risk_engine or RiskEngine(market_data=market_data)
        # Volatility seams. Defaults are the safe no-data impls, so the
        # ATR stop falls back to the % stop and the VIX gate no-ops until
        # the real feeds are wired (Phase 5). Tests / the lifespan inject
        # live providers.
        self._vol_provider = vol_provider or volatility.NullVolatilityProvider()
        self._vol_regime = vol_regime or volatility.NullVolatilityRegime()
        self._md = market_data or MarketDataBus()
        # Wire market_data into the risk engine so quote lookups
        # work end-to-end.
        if not hasattr(self._risk, "_md") or self._risk._md is None:  # type: ignore[attr-defined]
            self._risk._md = self._md  # type: ignore[attr-defined]
        # Share the volatility regime with the risk engine so its high-VIX
        # sizing throttle reads the same feed the manager's gate uses.
        if getattr(self._risk, "_vol_regime", None) is None:  # type: ignore[attr-defined]
            self._risk._vol_regime = self._vol_regime  # type: ignore[attr-defined]
        self._session_factory = session_factory or _default_session_factory()
        self._backends: dict[int, TradingBackend] = dict(backends or {})
        # A single paper backend instance is reused for all paper
        # accounts. Tests can pre-seed this.
        self._paper = paper_backend or PaperBackend(market_data=self._md)
        self._settings_provider = settings_provider or get_settings
        # Optional quote feed. When set (the real lifespan wires one
        # in), the manager seeds a live/simulated quote for the symbol
        # before placing a paper order — so the MARKET fill has a
        # price — and derives coherent entry/SL/target from it. Tests
        # leave this None and pre-seed quotes via `set_quote_sync`.
        self._quote_feed: Optional[Any] = None
        self._stop_event: asyncio.Event = asyncio.Event()
        self._ready_event: asyncio.Event = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        self._sub_queue: Optional[asyncio.Queue[Any]] = None
        self._settings_sub_queue: Optional[asyncio.Queue[Any]] = None

    # -- lifecycle -------------------------------------------------------

    def start(self) -> asyncio.Task[None]:
        if self._task is not None and not self._task.done():
            return self._task
        self._stop_event.clear()
        self._ready_event.clear()
        self._task = asyncio.create_task(self._run(), name="execution-manager")
        return self._task

    async def wait_until_ready(self, timeout: float = 2.0) -> None:
        await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)

    def stop(self) -> None:
        self._stop_event.set()
        if self._sub_queue is not None:
            try:
                event_bus.unsubscribe(CHANNEL_NEW_SIGNAL, self._sub_queue)
            except Exception:  # noqa: BLE001
                pass
            self._sub_queue = None
        if self._settings_sub_queue is not None:
            try:
                event_bus.unsubscribe(CHANNEL_SETTINGS_UPDATED, self._settings_sub_queue)
            except Exception:  # noqa: BLE001
                pass
            self._settings_sub_queue = None
        if self._token_sub_queue is not None:
            try:
                event_bus.unsubscribe(
                    CHANNEL_BROKER_TOKEN_ROTATED, self._token_sub_queue
                )
            except Exception:  # noqa: BLE001
                pass
            self._token_sub_queue = None

    async def wait_until_stopped(self) -> None:
        if self._task is None:
            return
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
        # Best-effort stop the paper backend.
        try:
            await self._paper.stop()
        except Exception:  # noqa: BLE001
            pass

    # -- main loop -------------------------------------------------------

    async def _run(self) -> None:
        self._sub_queue = event_bus.subscribe(CHANNEL_NEW_SIGNAL)
        self._settings_sub_queue = event_bus.subscribe(CHANNEL_SETTINGS_UPDATED)
        self._token_sub_queue = event_bus.subscribe(CHANNEL_BROKER_TOKEN_ROTATED)
        log.info("execution_manager.start", channel=CHANNEL_NEW_SIGNAL)
        # Start the paper backend's pending-order loop.
        self._paper.start()
        self._ready_event.set()
        try:
            while not self._stop_event.is_set():
                # Use a short wait so we can also notice stop_event.
                # We pull events from each queue without spawning
                # per-iteration tasks (which were leaking on
                # cancellation).
                events: list[Any] = []
                deadline = asyncio.get_running_loop().time() + 0.5
                for q in (
                    self._sub_queue,
                    self._settings_sub_queue,
                    self._token_sub_queue,
                ):
                    if q is None:
                        continue
                    try:
                        # `get_nowait` returns immediately. The
                        # event loop is free for the sleep below.
                        evt = q.get_nowait()
                        events.append(evt)
                    except asyncio.QueueEmpty:
                        pass
                if not events:
                    # No events — wait a bit for the stop event.
                    remaining = max(0.0, deadline - asyncio.get_running_loop().time())
                    if remaining > 0:
                        try:
                            await asyncio.wait_for(
                                self._stop_event.wait(), timeout=remaining
                            )
                        except asyncio.TimeoutError:
                            pass
                    continue
                for event in events:
                    if event.channel == CHANNEL_NEW_SIGNAL:
                        try:
                            await self._handle_signal(event.payload)
                        except Exception:  # noqa: BLE001
                            log.exception("execution_manager.handler_crashed")
                    elif event.channel == CHANNEL_SETTINGS_UPDATED:
                        try:
                            await self._on_settings_updated(event.payload)
                        except Exception:  # noqa: BLE001
                            log.exception("execution_manager.settings_reload_failed")
                    elif event.channel == CHANNEL_BROKER_TOKEN_ROTATED:
                        try:
                            self._on_broker_token_rotated(event.payload)
                        except Exception:  # noqa: BLE001
                            log.exception("execution_manager.token_rotation_handler_crashed")
        finally:
            log.info("execution_manager.stop")
            if self._sub_queue is not None:
                event_bus.unsubscribe(CHANNEL_NEW_SIGNAL, self._sub_queue)
                self._sub_queue = None
            if self._settings_sub_queue is not None:
                event_bus.unsubscribe(CHANNEL_SETTINGS_UPDATED, self._settings_sub_queue)
                self._settings_sub_queue = None
            if self._token_sub_queue is not None:
                event_bus.unsubscribe(CHANNEL_BROKER_TOKEN_ROTATED, self._token_sub_queue)
                self._token_sub_queue = None
            try:
                await self._paper.stop()
            except Exception:  # noqa: BLE001
                pass

    # -- per-signal handler ---------------------------------------------

    async def _handle_signal(self, payload: dict[str, Any]) -> None:
        signal_id = payload.get("signal_id")
        if not isinstance(signal_id, int):
            log.warning("execution_manager.bad_signal_payload", payload=payload)
            return
        await self.process_signal(signal_id)

    async def process_signal(self, signal_id: int) -> Optional[dict[str, Any]]:
        """Process one signal end-to-end.

        Returns a small dict describing the outcome (for tests
        that want to assert what happened). The manager itself
        emits the public events on the bus.
        """
        # Step 1: load signal + strategy + account.
        loaded = await asyncio.get_running_loop().run_in_executor(
            None, _load_signal_context, self._session_factory, signal_id
        )
        if loaded is None:
            log.warning("execution_manager.signal_not_found", signal_id=signal_id)
            return None
        signal, strategy, account, levels, filed_at, event_type = loaded
        if signal is None:
            return None
        if strategy is None or account is None:
            # No strategy / no account → can't route. Block.
            await asyncio.get_running_loop().run_in_executor(
                None, _persist_risk_block,
                self._session_factory, signal, None,
                "NO_ROUTING", "no strategy or broker_account on the signal",
            )
            await event_bus.publish(
                CHANNEL_RISK_BLOCKED,
                {"signal_id": signal.id, "code": "NO_ROUTING"},
            )
            return {"approved": False, "code": "NO_ROUTING"}

        # The dashboard Paper/Fyers on-off switches flip the account's
        # `enabled` flag. A disabled account means "don't trade this
        # account" — block before any order is placed.
        if not getattr(account, "enabled", True):
            await asyncio.get_running_loop().run_in_executor(
                None, _persist_risk_block,
                self._session_factory, signal, account.id,
                "ACCOUNT_DISABLED",
                f"account {account.name!r} is switched off",
            )
            await event_bus.publish(
                CHANNEL_RISK_BLOCKED,
                {"signal_id": signal.id, "code": "ACCOUNT_DISABLED", "account_id": account.id},
            )
            return {"approved": False, "code": "ACCOUNT_DISABLED"}

        # Step 1.5: pre-entry gate (RISK.md) — portfolio circuit breakers /
        # kill-switch, the IST entry window, and an order-time staleness
        # re-check (the LLM call may have aged the signal past the
        # freshness budget). Any of these blocks before we touch a broker.
        gate = await self._pre_entry_gate(signal, filed_at, event_type)
        if gate is not None:
            code, message = gate
            await asyncio.get_running_loop().run_in_executor(
                None, _persist_risk_block,
                self._session_factory, signal, account.id, code, message,
            )
            await event_bus.publish(
                CHANNEL_RISK_BLOCKED,
                {"signal_id": signal.id, "code": code, "message": message,
                 "symbol": signal.symbol, "account_id": account.id},
            )
            return {"approved": False, "code": code}

        # Step 2: pick the backend.
        backend = self._backend_for(account)
        if backend is None:
            await asyncio.get_running_loop().run_in_executor(
                None, _persist_risk_block,
                self._session_factory, signal, account.id,
                "NO_BACKEND",
                f"no backend available for account {account.name!r}",
            )
            await event_bus.publish(
                CHANNEL_RISK_BLOCKED,
                {"signal_id": signal.id, "code": "NO_BACKEND"},
            )
            return {"approved": False, "code": "NO_BACKEND"}

        # Step 3: compute entry / SL / target.
        #
        # When a quote feed is wired in (the real runtime), it is the
        # source of truth: seed a live/simulated quote for the symbol
        # so the MARKET order fills, use that as the entry, and derive
        # SL/target from the configured DEFAULT_SL_PCT / DEFAULT_TARGET_RR.
        # We persist these coherent levels back onto the signal so the
        # TradeManager (which reads the rationale) and the UI agree.
        #
        # Without a quote feed (tests), fall back to the parsed
        # rationale levels, then any cached quote.
        entry = levels.get("entry")
        stop_loss = levels.get("stop_loss")
        target = levels.get("target")
        # Per-event hold window (set when we derive coherent levels below).
        # Falls through to the global MAX_HOLD_SECONDS in TradeManager when
        # left None (older publishers / no quote feed).
        event_max_hold: Optional[int] = None
        if self._quote_feed is not None:
            try:
                seeded = await self._quote_feed.seed_symbol(signal.symbol)
            except Exception:  # noqa: BLE001
                log.exception("execution_manager.quote_seed_failed", symbol=signal.symbol)
                seeded = None
            if seeded is not None and seeded > 0:
                entry = float(seeded)
                # Warm the ATR cache (live Fyers candle provider) before we
                # size the stop; no-op for the Null / paper providers.
                await self._refresh_atr(signal.symbol)
                stop_loss, target, rr, event_max_hold = self._derive_levels(
                    symbol=signal.symbol,
                    entry=entry,
                    action=signal.action,
                    event_type=event_type,
                )
                await asyncio.get_running_loop().run_in_executor(
                    None, _update_signal_levels,
                    self._session_factory, signal.id, entry, stop_loss, target, rr,
                )
            else:
                # A live quote feed is wired but couldn't price the symbol
                # right now (WS silent + REST miss). Do NOT fall back to the
                # rule's placeholder entry or a synthetic anchor: the fill
                # becomes the cost basis, and a fabricated entry that then
                # marks/exits against the real feed manufactures a phantom
                # P&L (the CEIGALL ₹833→₹365 ≈ -₹1L bug). Decline instead.
                await asyncio.get_running_loop().run_in_executor(
                    None, _persist_risk_block,
                    self._session_factory, signal, account.id,
                    "NO_LIVE_PRICE",
                    f"no live price available for {signal.symbol!r}; entry "
                    "skipped to avoid a synthetic fill",
                )
                await event_bus.publish(
                    CHANNEL_RISK_BLOCKED,
                    {"signal_id": signal.id, "code": "NO_LIVE_PRICE",
                     "message": f"no live price for {signal.symbol}",
                     "symbol": signal.symbol, "account_id": account.id},
                )
                log.warning(
                    "execution_manager.no_live_price_block",
                    symbol=signal.symbol, signal_id=signal.id,
                )
                return {"approved": False, "code": "NO_LIVE_PRICE"}
        if entry is None:
            quote = await self._md.get_quote(signal.symbol)
            if quote is not None:
                entry = float(quote.last_price)
        if stop_loss is None and entry is not None:
            if signal.action == "BUY":
                stop_loss = float(entry) * 0.95
            else:
                stop_loss = float(entry) * 1.05

        # Step 4: risk engine.
        decision = await self._risk.evaluate(
            signal=signal,
            strategy=strategy,
            account=account,
            entry=entry,
            stop_loss=stop_loss,
            target=target,
        )
        if not decision.approved:
            code = decision.codes[0] if decision.codes else "RISK_BLOCKED"
            message = decision.violations[0]["message"] if decision.violations else "risk blocked"
            await asyncio.get_running_loop().run_in_executor(
                None, _persist_risk_block,
                self._session_factory, signal, account.id, code, message,
                decision.context,
            )
            await event_bus.publish(
                CHANNEL_RISK_BLOCKED,
                {
                    "signal_id": signal.id,
                    "code": code,
                    "message": message,
                    "violations": decision.violations,
                    "symbol": signal.symbol,
                    "account_id": account.id,
                },
            )
            return {"approved": False, "code": code, "violations": decision.violations}

        # Step 5: place the order.
        side = OrderSide.BUY if signal.action == "BUY" else OrderSide.SELL
        # IOC marketable-limit entry (RISK.md §5): cap the fill within a
        # small buffer of the intended entry so a news spike can't fill us
        # far away, and use immediate-or-cancel so we never rest in a fast
        # market. Always INTRADAY for the auto pipeline. Falls back to a
        # plain MARKET order only when we have no entry price to anchor the
        # limit on.
        settings_obj = self._settings_provider()
        intended_entry = float(entry) if entry is not None else None
        buffer = float(getattr(settings_obj, "ENTRY_BUFFER_PCT", 0.2)) / 100.0
        if intended_entry is not None and intended_entry > 0:
            limit_price = (
                intended_entry * (1.0 + buffer) if side == OrderSide.BUY
                else intended_entry * (1.0 - buffer)
            )
            order_type = OrderType.LIMIT
            validity = "IOC"
        else:
            limit_price = None
            order_type = OrderType.MARKET
            validity = "DAY"
        try:
            result = await backend.place_order(
                signal=signal,
                symbol=signal.symbol,
                side=side,
                quantity=int(decision.sizing.qty),
                order_type=order_type,
                limit_price=limit_price,
                stop_price=None,
                product_type=ProductType.INTRADAY,
                validity=validity,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("execution_manager.place_order_crashed", signal_id=signal.id)
            await asyncio.get_running_loop().run_in_executor(
                None, _persist_trade_placed_only,
                self._session_factory, signal, account.id,
                side.value, decision.sizing.qty,
                0.0, "market", "REJECTED", str(e),
            )
            return {"approved": True, "code": "PLACE_ORDER_FAILED", "error": str(e)}

        # Slippage of the fill vs the intended entry (RISK.md §5/§7). The
        # IOC marketable-limit bounds this to ~the buffer; we record it on
        # the trade row and warn if it ever exceeds tolerance.
        slippage_pct: Optional[float] = None
        if result.average_price is not None and intended_entry and intended_entry > 0:
            slippage_pct = (
                abs(float(result.average_price) - intended_entry) / intended_entry * 100.0
            )
            if slippage_pct > float(getattr(settings_obj, "MAX_SLIPPAGE_PCT", 0.25)):
                log.warning(
                    "execution_manager.slippage_exceeded",
                    symbol=signal.symbol, intended=round(intended_entry, 2),
                    fill=round(float(result.average_price), 2),
                    slippage_pct=round(slippage_pct, 3),
                )

        # Step 6: persist the trade row (status from result).
        await asyncio.get_running_loop().run_in_executor(
            None, _persist_trade,
            self._session_factory, signal, account.id, result, slippage_pct,
        )
        # The actual fill price is the authoritative entry — prefer it
        # over the analysis levels so the TradeManager's P&L matches
        # what we really paid. Fall back to the computed entry.
        fill_entry = (
            float(result.average_price)
            if result.average_price is not None
            else (float(entry) if entry is not None else None)
        )
        await event_bus.publish(
            CHANNEL_TRADE_EXECUTED,
            {
                "signal_id": signal.id,
                "account_id": account.id,
                "strategy_id": getattr(strategy, "id", None),
                "symbol": signal.symbol,
                "side": side.value,
                "quantity": int(decision.sizing.qty),
                "broker_order_id": result.broker_order_id,
                "state": result.state.value,
                "error": result.error,
                "backend": getattr(backend, "name", type(backend).__name__),
                # Coherent trade-management levels (entry = real fill).
                "entry": fill_entry,
                "stop_loss": float(stop_loss) if stop_loss is not None else None,
                "target": float(target) if target is not None else None,
                # Per-event hold window (None → TradeManager uses the global
                # MAX_HOLD_SECONDS). Carries the event-matrix hold rule.
                "max_hold_seconds": event_max_hold,
            },
        )
        return {
            "approved": True,
            "broker_order_id": result.broker_order_id,
            "state": result.state.value,
            "qty": int(decision.sizing.qty),
            "backend": getattr(backend, "name", type(backend).__name__),
        }

    def _derive_levels(
        self,
        *,
        symbol: str,
        entry: float,
        action: str,
        event_type: Optional[str],
    ) -> tuple[float, float, float, int]:
        """Compute (stop_loss, target, rr, max_hold_seconds) for an entry.

        Volatility-aware: the stop distance is ATR×mult (clamped to a sane
        band of entry) when an ATR is available, else the event profile's
        percentage stop. The target sits at `rr`× the stop distance. Both
        the ATR multiplier, the RR, and the hold window come from the
        per-event-type profile (`event_profiles`), which falls back to the
        global Settings for anything it doesn't override.
        """
        settings = self._settings_provider()
        profile = event_profiles.profile_for(event_type).resolved(settings)
        atr = (
            volatility.resolve_atr(self._vol_provider, symbol)
            if getattr(settings, "ATR_ENABLED", True)
            else None
        )
        dist = volatility.stop_distance(
            entry=float(entry),
            atr=atr,
            mult=profile.sl_atr_mult,
            default_pct=profile.sl_default_pct,
            min_pct=float(settings.DEFAULT_SL_MIN_PCT),
            max_pct=float(settings.ATR_MAX_STOP_PCT),
            smallcap_price=float(settings.SMALLCAP_PRICE),
            smallcap_pct=float(settings.DEFAULT_SL_SMALLCAP_PCT),
        )
        rr = float(profile.target_rr)
        entry = float(entry)
        if str(action).upper() == "BUY":
            stop_loss = entry - dist
            target = entry + dist * rr
        else:
            stop_loss = entry + dist
            target = entry - dist * rr
        # Guard a degenerate (sub-floor) entry from producing a <=0 stop.
        stop_loss = max(0.01, stop_loss)
        return (stop_loss, target, rr, int(profile.max_hold_seconds))

    async def _refresh_atr(self, symbol: str) -> None:
        """Warm the ATR cache for `symbol` before sizing (async path).
        No-op for providers without an async `refresh` (Null / stub /
        paper tick estimator). Never raises."""
        p = self._vol_provider
        refresh = getattr(p, "refresh", None)
        if refresh is None:
            return
        try:
            await refresh(symbol)
        except Exception:  # noqa: BLE001
            log.debug("execution_manager.atr_refresh_failed", symbol=symbol)

    async def _refresh_vix(self) -> None:
        """Warm the India-VIX cache before the gate / sizing read it.
        No-op for regimes without an async `refresh`. Never raises."""
        r = self._vol_regime
        refresh = getattr(r, "refresh", None)
        if refresh is None:
            return
        try:
            await refresh()
        except Exception:  # noqa: BLE001
            log.debug("execution_manager.vix_refresh_failed")

    async def _pre_entry_gate(
        self, signal: Signal, filed_at: Optional[datetime],
        event_type: Optional[str] = None,
    ) -> Optional[tuple[str, str]]:
        """Gate a new auto entry on the portfolio circuit breakers /
        kill-switch, the volatility regime, the per-event confidence floor,
        the IST entry window, and order-time freshness.

        Returns (code, message) when blocked, else None. Manual Trade-page
        orders deliberately do NOT pass through here.
        """
        from app.risk import market_clock

        settings = self._settings_provider()
        # 1. Circuit breakers / kill switch (DB-backed; respects the
        #    consecutive-loser high-conviction resume bar).
        allowed, reason = await asyncio.get_running_loop().run_in_executor(
            None, _check_entry_breakers, self._session_factory,
            float(getattr(signal, "confidence", 0.0) or 0.0),
        )
        if not allowed:
            return (f"HALT_{(reason or 'blocked').upper()}", f"circuit breaker: {reason}")
        # 1b. Volatility regime (India VIX). Fail-safe: None VIX → skip. When
        #     the feed is wired, suspend new entries above INDIA_VIX_MAX.
        await self._refresh_vix()
        vix_max = float(getattr(settings, "INDIA_VIX_MAX", 0.0) or 0.0)
        if vix_max > 0 and self._vol_regime is not None:
            try:
                vix = self._vol_regime.india_vix()
            except Exception:  # noqa: BLE001
                vix = None
            if vix is not None and float(vix) > vix_max:
                return ("HIGH_VIX",
                        f"India VIX {float(vix):.2f} > max {vix_max:.2f}; entries suspended")
        # 1c. Per-event confidence floor (event matrix). Priced-in events
        #     (dividends, splits) demand stronger conviction than the global
        #     floor; the global floor itself is still enforced in the risk
        #     engine (R2b). This only RAISES the bar for certain events.
        profile = event_profiles.profile_for(event_type).resolved(settings)
        conf = float(getattr(signal, "confidence", 0.0) or 0.0)
        if profile.min_confidence > 0 and conf < profile.min_confidence:
            return ("EVENT_CONFIDENCE_FLOOR",
                    f"confidence {conf:.2f} < {profile.min_confidence:.2f} required "
                    f"for event {event_type or 'DEFAULT'}")
        # 2. IST entry window — enforced in EVERY mode (intraday-only rule:
        #    no carry-forward, ever). An entry outside market hours can't be
        #    squared off the same session — the price feed is frozen, so the
        #    position just sits (SL/target can never trigger) until the time
        #    exit closes it flat, or worse survives overnight if the bot
        #    stops. Paper entries after hours are junk data, not simulation.
        #    Set ENFORCE_MARKET_HOURS=false to deliberately test off-hours.
        if not market_clock.is_entry_window():
            return ("MARKET_CLOSED",
                    market_clock.entry_block_reason() or "outside entry window")
        # 3. Order-time staleness re-check.
        if filed_at is not None:
            fa = filed_at if filed_at.tzinfo is not None else filed_at.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - fa).total_seconds()
            max_age = float(getattr(settings, "MAX_NEWS_AGE_SECONDS", 90))
            if age > max_age:
                return ("STALE_AT_ORDER",
                        f"news aged {age:.0f}s > {max_age:.0f}s at order time")
        return None

    # -- backend selection ---------------------------------------------

    def _effective_app_id(self, account: BrokerAccount) -> Optional[str]:
        """The Fyers app_id to authenticate order placement with.

        `.env` (FYERS_APP_ID) is the SINGLE SOURCE OF TRUTH. The
        `broker_accounts.app_id` column only mirrors it (written during
        OAuth) and goes stale the moment the operator edits `.env` /
        re-creates their Fyers app without re-running OAuth — which is
        exactly how an order ends up rejected as the *old* app. Reading
        from settings here guarantees the bot always trades as the app
        configured in `.env`, falling back to the row's copy only when
        `.env` is blank.
        """
        settings = self._settings_provider()
        return (getattr(settings, "FYERS_APP_ID", "") or "").strip() or getattr(
            account, "app_id", None
        )

    def _backend_for(self, account: BrokerAccount) -> Optional[TradingBackend]:
        """Pick the backend for `account`.

        1. If the manager already has a backend for this account id
           (caller pre-seeded it), use it.
        2. If `account.paper_mode` is True, return the shared
           PaperBackend.
        3. If `Settings.TRADING_MODE == 'paper'`, force paper
           (global kill-switch). The account's app_id / secret /
           access_token are still loaded; the next time the user
           flips TRADING_MODE to 'live' we'll spin up a
           FyersLiveBackend on demand.
        4. Otherwise (live), spin up a FyersLiveBackend with
           the account's app_id + access_token and cache it.
        """
        cached = self._backends.get(int(account.id))
        if cached is not None:
            return cached
        settings = self._settings_provider()
        if bool(getattr(account, "paper_mode", True)) or settings.is_paper:
            return self._paper
        # Live. app_id comes from .env (see `_effective_app_id`); the
        # access_token is the OAuth artifact stored on the row.
        app_id = self._effective_app_id(account)
        if not app_id or not getattr(account, "access_token", None):
            log.warning(
                "execution_manager.account_missing_creds",
                account_id=account.id, name=account.name,
            )
            return None
        backend = FyersLiveBackend(
            app_id=app_id,
            access_token=account.access_token,
            broker_account_id=int(account.id),
            account_name=account.name,
        )
        self._backends[int(account.id)] = backend
        return backend

    def _manual_backend_for(self, account: BrokerAccount) -> Optional[TradingBackend]:
        """Backend for a *manual* Trade-page order.

        Unlike `_backend_for`, this ignores the global paper
        kill-switch: the Trade page is real-money by design and the
        account has already been verified as a live, tokened account
        by the API. So a manual order on a Fyers account always routes
        to the live Fyers backend (otherwise orders silently went to
        paper whenever TRADING_MODE=paper). Pre-seeded backends (test
        stubs) still win.
        """
        cached = self._backends.get(int(account.id))
        if cached is not None:
            return cached
        if bool(getattr(account, "paper_mode", False)):
            return self._paper
        # app_id from .env (single source of truth); token from the row.
        app_id = self._effective_app_id(account)
        if not app_id or not getattr(account, "access_token", None):
            return None
        backend = FyersLiveBackend(
            app_id=app_id,
            access_token=account.access_token,
            broker_account_id=int(account.id),
            account_name=account.name,
        )
        self._backends[int(account.id)] = backend
        return backend

    def invalidate_backend(self, broker_account_id: int) -> None:
        """Drop the cached backend for an account (e.g. after the
        Fyers OAuth callback rotates the access_token)."""
        self._backends.pop(int(broker_account_id), None)

    def register_backend(self, broker_account_id: int, backend: TradingBackend) -> None:
        """Inject a backend (e.g. a stub for tests)."""
        self._backends[int(broker_account_id)] = backend

    # -- manual order placement (Trade page) ---------------------------

    async def place_manual_order(
        self,
        *,
        account: BrokerAccount,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str = "MARKET",
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        product_type: str = "INTRADAY",
        bypass_risk: bool = False,
        operator: str = "ui_trade_page",
    ) -> dict[str, Any]:
        """Place a single order from the Trade page (NOT from a signal).

        Flow:
          1. Build a lightweight `ManualSignal` duck-typed object
             carrying the order's fields (action, symbol, confidence,
             position_size_pct, rationale, id=None).
          2. Run the risk engine against that signal so manual trades
             respect the same global caps as auto-generated ones.
             With `bypass_risk=True` (the operator typed the confirm
             phrase) we still LOG the risk verdict but don't block.
          3. Pick the backend for the account and submit.
          4. Persist a `trades` row in the same shape the auto-pipeline
             uses, plus an `audit_log` row tagged with `actor=ui` and
             the operator's reason.
          5. Publish `trade.executed` so the WS / dashboard updates.
        """
        if int(quantity) <= 0:
            raise ValueError("quantity must be > 0")
        try:
            side_enum = OrderSide(side.upper())
        except ValueError as e:
            raise ValueError(f"side must be BUY or SELL, got {side!r}") from e
        try:
            type_enum = OrderType(order_type.upper())
        except ValueError as e:
            raise ValueError(f"unknown order_type {order_type!r}") from e
        try:
            product_enum = ProductType(product_type.upper())
        except ValueError as e:
            raise ValueError(f"unknown product_type {product_type!r}") from e

        # The risk engine expects an object with these attributes.
        from types import SimpleNamespace
        manual_signal = SimpleNamespace(
            id=None,
            symbol=symbol,
            action=side_enum.value,
            confidence=1.0,
            position_size_pct=0.0,
            strategy_id=None,
            rationale=f"manual trade from {operator}",
        )

        # Derive entry / stop_loss / target for the risk engine. For
        # manual trades, the operator didn't pick SL/target — but the
        # risk engine hard-blocks on None. We use:
        #   entry     = the limit_price the operator entered, else the
        #               last cached quote. If we have neither, we use
        #               a conservative placeholder so the engine can
        #               at least produce a decision — but flag it in
        #               the response so the operator knows the SL/target
        #               numbers are derived, not real.
        settings = self._settings_provider()
        try:
            sl_pct = float(settings.DEFAULT_SL_PCT) / 100.0
            rr = float(settings.DEFAULT_TARGET_RR)
        except (AttributeError, TypeError, ValueError):
            sl_pct, rr = 0.06, 3.0

        entry = limit_price
        entry_is_synthetic = False
        if entry is None:
            q = await self._md.get_quote(symbol)
            if q is not None:
                entry = float(q.last_price)
        if entry is None or float(entry) <= 0.0:
            # Synthetic placeholder: ₹100 / share is a reasonable
            # NSE cash default. The risk engine's position-cap check
            # uses this as the size basis; it's better than 1.0 INR
            # which would let huge qty's slip through the cap.
            entry = 100.0
            entry_is_synthetic = True
        entry = float(entry)
        if side_enum == OrderSide.BUY:
            stop_loss = entry * (1.0 - sl_pct)
            target = entry * (1.0 + sl_pct * rr)
        else:
            stop_loss = entry * (1.0 + sl_pct)
            target = entry * (1.0 - sl_pct * rr)

        # Manual Trade-page orders respect the SAME global risk caps as
        # the auto pipeline (so changes you make in Settings actually take
        # effect here too). A rejected verdict BLOCKS the order unless the
        # operator explicitly overrides it (`bypass_risk=True`). A risk-
        # engine *fault* never blocks — it's surfaced as advisory only.
        risk_codes: list[str] = []
        risk_message = ""
        risk_approved = True
        try:
            decision = await self._risk.evaluate(
                signal=manual_signal,
                account=account,
                entry=entry,
                stop_loss=stop_loss,
                target=target,
                manual_qty=int(quantity),
            )
            risk_approved = bool(decision.approved)
            if not decision.approved:
                risk_codes = list(decision.codes)
                risk_message = (
                    "; ".join(v.get("code", "?") for v in decision.violations)
                    if decision.violations
                    else "risk limit exceeded"
                )
                log.warning(
                    "manual_order.risk_bypassed" if bypass_risk
                    else "manual_order.risk_blocked",
                    account_id=account.id, symbol=symbol, side=side,
                    quantity=quantity, codes=risk_codes,
                )
        except Exception as e:  # noqa: BLE001
            # Engine fault → advisory only; never block on an engine error.
            log.warning("manual_order.risk_engine_failed", error=str(e))
            risk_message = f"risk check unavailable: {e}"

        # Enforce: a rejected verdict blocks the order unless bypassed. We
        # return before touching the backend so no order reaches the broker.
        if not risk_approved and not bypass_risk:
            return {
                "ok": False,
                "blocked": True,
                "risk_codes": risk_codes,
                "risk_message": risk_message,
                "risk_warning": risk_message or None,
                "bypassed_risk": False,
                "broker_order_id": None,
                "status": "REJECTED_RISK",
                "error": (
                    f"Blocked by risk limits: {risk_message}. Reduce the "
                    "order size, relax the limit in Settings, or re-submit "
                    "with risk override to place it anyway."
                ),
                "reason": "risk_block",
                "entry_used": entry,
                "stop_loss_used": stop_loss,
                "target_used": target,
                "entry_is_synthetic": entry_is_synthetic,
            }

        backend = self._manual_backend_for(account)
        if backend is None:
            raise RuntimeError(
                f"no live backend for account {account.id} "
                f"(name={account.name!r}). Complete Fyers OAuth in the Accounts page."
            )

        result = await backend.place_order(
            signal=manual_signal,
            symbol=symbol,
            side=side_enum,
            quantity=int(quantity),
            order_type=type_enum,
            limit_price=limit_price,
            stop_price=stop_price,
            product_type=product_enum,
        )
        # Fill reconciliation is webhook-only by design: the order is
        # persisted in whatever state the broker returned (PENDING for a
        # Fyers /orders/sync), and the Fyers postback (/api/fyers/postback)
        # flips it to filled/rejected and updates the position. We do NOT
        # poll get_order_status here — the operator wants order data to come
        # exclusively from the Fyers webhook.
        # Persist + audit
        await asyncio.get_running_loop().run_in_executor(
            None,
            self._persist_manual_trade_executed,
            account.id,
            manual_signal,
            side_enum,
            int(quantity),
            float(limit_price or stop_price or 0.0),
            type_enum,
            product_enum,
            result,
            operator,
            bypass_risk,
            risk_codes,
            risk_message,
        )
        # `result.state` is one of PENDING / FILLED / REJECTED / etc.
        # The local `trades.status` column uses the same lowercase
        # vocab as the auto-pipeline ("placed" for new rows, "filled"
        # for executed, "rejected" for refused, "cancelled" for
        # cancelled).
        # Publish so /ws and the dashboard light up.
        try:
            from app.services.event_bus import event_bus
            await event_bus.publish(
                CHANNEL_TRADE_EXECUTED,
                {
                    "trade_id": getattr(result, "trade_id", None),
                    "broker_order_id": result.broker_order_id,
                    "symbol": result.symbol,
                    "side": result.side.value,
                    "quantity": result.quantity,
                    "order_type": result.order_type.value,
                    "state": result.state.value,
                    "source": "manual",
                    "operator": operator,
                },
            )
        except Exception:  # noqa: BLE001
            log.exception("manual_order.publish_failed")
        return {
            "ok": result.state in (OrderState.PENDING, OrderState.FILLED),
            "blocked": False,
            # Risk codes are surfaced as a warning; the order proceeded
            # either because it passed risk or the operator overrode it.
            "risk_codes": risk_codes,
            "risk_message": risk_message,
            "risk_warning": risk_message or None,
            "bypassed_risk": bool(bypass_risk and risk_codes),
            "broker_order_id": result.broker_order_id,
            "status": result.state.value,
            "error": result.error,
            # Surface the OrderResult's `raw` field as `reason` so
            # the UI can branch on diagnostic markers from the
            # Fyers backend (e.g. "ip_whitelist" → render a
            # copyable public IP hint).
            "reason": (result.raw or {}).get("reason") if result.raw else None,
            "entry_used": entry,
            "stop_loss_used": stop_loss,
            "target_used": target,
            "entry_is_synthetic": entry_is_synthetic,
        }

    def _persist_manual_trade_executed(
        self,
        account_id: int,
        signal: Any,
        side: OrderSide,
        quantity: int,
        price: float,
        order_type: OrderType,
        product_type: ProductType,
        result: OrderResult,
        operator: str,
        bypass_risk: bool,
        risk_codes: list[str],
        risk_message: str,
    ) -> None:
        # Map OrderState -> the local trades.status vocabulary the
        # rest of the app uses (lowercase, "placed" for new rows).
        state_to_status = {
            OrderState.PENDING: "placed",
            OrderState.FILLED: "filled",
            OrderState.CANCELLED: "cancelled",
            OrderState.REJECTED: "rejected",
            OrderState.EXPIRED: "expired",
        }
        local_status = state_to_status.get(result.state, "placed")
        # On a confirmed fill, the authoritative price is the broker's
        # average fill price (a MARKET order's `price` arg is 0).
        fill_price = float(
            result.average_price
            if result.average_price is not None
            else (price or 0.0)
        )
        from app.db.session import SessionLocal
        with SessionLocal() as session:
            session.add(
                TradeRow(
                    signal_id=None,
                    broker_account_id=account_id,
                    symbol=result.symbol,
                    side=side.value,
                    quantity=int(quantity),
                    price=fill_price,
                    order_type=order_type.value,
                    status=local_status,
                    broker_order_id=result.broker_order_id or None,
                    executed_at=datetime.now(timezone.utc) if result.state == OrderState.FILLED else None,
                )
            )
            # Mirror a confirmed fill into the positions table so the
            # dashboard's Active Positions shows the manual trade (and it
            # can be squared off). Pending orders update later via the
            # postback / next status poll.
            if result.state == OrderState.FILLED and fill_price > 0:
                qty = int(quantity)
                signed = qty if side == OrderSide.BUY else -qty
                pos = (
                    session.query(PositionRow)
                    .filter_by(symbol=result.symbol)
                    .one_or_none()
                )
                if pos is None:
                    if signed != 0:
                        session.add(
                            PositionRow(
                                symbol=result.symbol,
                                quantity=signed,
                                average_price=fill_price,
                                last_price=fill_price,
                                unrealized_pnl=0.0,
                            )
                        )
                else:
                    new_qty = pos.quantity + signed
                    if signed > 0 and new_qty != 0:
                        pos.average_price = (
                            pos.average_price * pos.quantity + fill_price * signed
                        ) / new_qty
                    pos.quantity = new_qty
                    pos.last_price = fill_price
            session.add(
                AuditLog(
                    actor=operator,
                    action="order.manual_placed",
                    target=f"account:{account_id}",
                    before=None,
                    after={
                        "symbol": result.symbol,
                        "side": side.value,
                        "quantity": int(quantity),
                        "order_type": order_type.value,
                        "product_type": product_type.value,
                        "broker_order_id": result.broker_order_id,
                        "state": result.state.value,
                        "limit_price": float(price or 0.0),
                        "bypass_risk": bypass_risk,
                        "risk_codes": risk_codes,
                        "risk_message": risk_message,
                    },
                )
            )
            session.commit()

    def _persist_manual_trade_blocked(
        self,
        *,
        account: BrokerAccount,
        signal: Any,
        side: OrderSide,
        quantity: int,
        price: float,
        order_type: OrderType,
        product_type: ProductType,
        reason: str,
        codes: list[str],
        operator: str,
    ) -> None:
        from app.db.session import SessionLocal
        with SessionLocal() as session:
            session.add(
                TradeRow(
                    signal_id=None,
                    broker_account_id=account.id,
                    symbol=signal.symbol,
                    side=side.value,
                    quantity=int(quantity),
                    price=float(price or 0.0),
                    order_type=order_type.value,
                    status="rejected_risk",
                    broker_order_id=None,
                )
            )
            session.add(
                RiskEvent(
                    event_type="manual_risk_block",
                    severity="warning",
                    message=reason,
                    halted=False,
                    context={
                        "source": "manual",
                        "operator": operator,
                        "side": side.value,
                        "quantity": int(quantity),
                        "order_type": order_type.value,
                        "product_type": product_type.value,
                        "codes": codes,
                    },
                )
            )
            session.add(
                AuditLog(
                    actor=operator,
                    action="order.manual_blocked",
                    target=f"account:{account.id}",
                    before=None,
                    after={
                        "symbol": signal.symbol,
                        "side": side.value,
                        "quantity": int(quantity),
                        "reason": reason,
                        "codes": codes,
                    },
                )
            )
            session.commit()

    def attach_quote_feed(self, quote_feed: Any) -> None:
        """Wire in a QuoteFeed so paper orders get a fill price and
        coherent entry/SL/target. Called by the lifespan."""
        self._quote_feed = quote_feed

    def attach_volatility_providers(
        self,
        *,
        vol_provider: Optional[volatility.VolatilityProvider] = None,
        vol_regime: Optional[volatility.VolatilityRegime] = None,
    ) -> None:
        """Swap in live volatility providers (Fyers ATR + India VIX) after
        construction. Called by the lifespan once the Fyers fetch fns are
        ready. The regime is shared with the risk engine so its high-VIX
        sizing throttle reads the same feed."""
        if vol_provider is not None:
            self._vol_provider = vol_provider
        if vol_regime is not None:
            self._vol_regime = vol_regime
            self._risk._vol_regime = vol_regime  # type: ignore[attr-defined]

    def attach_funds_provider(self, funds_provider: Any) -> None:
        """Wire the live Fyers funds feed into the risk engine so LIVE
        sizing anchors to the real account balance (RISK.md §2). Called
        by the lifespan once the Fyers fetch fn is ready. Fail-safe —
        with no provider (or a failing fetch) sizing falls back to the
        PORTFOLIO_VALUE ledger."""
        self._risk._funds_provider = funds_provider  # type: ignore[attr-defined]

    # -- settings hot-reload -------------------------------------------

    async def _on_settings_updated(self, _payload: dict[str, Any]) -> None:
        """Hot-reload: invalidate any FyersLiveBackend so a fresh
        one is built with the new TRADING_MODE on the next signal."""
        # Touch the risk engine so the operator can change global
        # risk defaults without restarting. (The engine re-reads
        # `get_settings()` on every call, so nothing to do here.)
        log.info("execution_manager.settings_reloaded")
        # If TRADING_MODE flipped, drop live backends so they're
        # rebuilt on the next signal.
        # We don't have the diff in the payload (just changed_keys),
        # so we conservatively drop all Fyers backends.
        to_drop = [
            acc_id
            for acc_id, b in self._backends.items()
            if isinstance(b, FyersLiveBackend)
        ]
        for acc_id in to_drop:
            self._backends.pop(acc_id, None)
        # Rebuild the risk engine's market data reference (it may
        # have been swapped by tests).
        if not getattr(self._risk, "_md", None):  # type: ignore[attr-defined]
            self._risk._md = self._md  # type: ignore[attr-defined]

    # -- token rotation hot-reload --------------------------------------

    def _on_broker_token_rotated(self, payload: dict[str, Any]) -> None:
        """Drop any cached FyersLiveBackend for the rotated account.

        The Fyers OAuth callback writes the new access_token (and,
        if the operator re-created their Fyers app, the new
        app_id) to the `broker_accounts` row and then publishes
        `broker.token_rotated`. We drop the cached backend so the
        next signal / place_order rebuilds it from the DB — the
        rebuild path reads the row fresh, so the operator's next
        trade uses the new credentials without a backend restart.
        """
        acc_id = payload.get("broker_account_id")
        if not isinstance(acc_id, int):
            log.warning(
                "execution_manager.token_rotation_bad_payload",
                payload=payload,
            )
            return
        # Only drop if we actually have a cached backend for this
        # account — no need to invalidate something that doesn't
        # exist, and the log line is more useful with a single
        # account_id than with a list of all backends.
        if acc_id in self._backends:
            log.info(
                "execution_manager.invalidate_after_token_rotation",
                account_id=acc_id,
            )
            self._backends.pop(acc_id, None)
        # Note: the broker_accounts row may also have changed
        # `app_id` (operator re-created their Fyers app). The
        # cached backend is keyed by `broker_account_id`; the
        # FyersLiveBackend constructor reads the row from the DB
        # when the account is first looked up, so a fresh
        # `app_id` + `access_token` will be picked up on the
        # next `_manual_backend_for(account)` call.

    # -- public test helpers --------------------------------------------

    @property
    def risk_engine(self) -> RiskEngine:
        return self._risk

    @property
    def market_data(self) -> MarketDataBus:
        return self._md

    @property
    def paper_backend(self) -> PaperBackend:
        return self._paper


# ---- DB helpers (sync, run in executor) --------------------------------


def _load_signal_context(
    session_factory: Callable[[], Session],
    signal_id: int,
) -> Optional[tuple[Optional[Signal], Optional[Strategy], Optional[BrokerAccount], dict[str, float], Optional[datetime], Optional[str]]]:
    with session_factory() as session:
        signal = session.get(Signal, signal_id)
        if signal is None:
            return (None, None, None, {}, None, None)
        strategy = (
            session.get(Strategy, signal.strategy_id) if signal.strategy_id else None
        )
        # The strategy carries the broker_account_id reference —
        # we look up the account from there. (T1's `strategies`
        # table doesn't have a `broker_account_id` column; the
        # mapping is by convention via `strategy.config`.)
        account: Optional[BrokerAccount] = None
        if strategy is not None:
            cfg = strategy.config or {}
            acc_id = cfg.get("broker_account_id")
            if isinstance(acc_id, int):
                account = session.get(BrokerAccount, acc_id)
        if account is None and strategy is not None:
            # Fall back: look at the first enabled broker_account
            # the strategy knows about (T1 only carries the
            # `name` + `paper_mode` flags; in v1 we route by the
            # first enabled row matching the strategy's name).
            account = (
                session.query(BrokerAccount)
                .filter(BrokerAccount.enabled.is_(True))
                .order_by(BrokerAccount.id.asc())
                .first()
            )
        # Pull the originating announcement's filed_at for the order-time
        # staleness re-check (the LLM call may have aged the signal) and the
        # analysis event_type, which drives the per-event trade-management
        # profile (stop/target/hold).
        filed_at = None
        event_type = None
        if signal.analysis_id is not None:
            a = session.get(Analysis, signal.analysis_id)
            if a is not None and a.announcement_id is not None:
                ann = session.get(Announcement, a.announcement_id)
                if ann is not None:
                    filed_at = ann.filed_at
                    # event_type lives on the Announcement (the Analysis row
                    # has no such column). It drives the per-event trade
                    # profile (stop/target/hold) and the per-event confidence
                    # floor — reading it off `a` always yielded None, which
                    # silently flattened every trade to the DEFAULT profile.
                    event_type = ann.event_type
        levels = parse_rationale_levels(signal.rationale)
        return (signal, strategy, account, levels, filed_at, event_type)


def _update_signal_levels(
    session_factory: Callable[[], Session],
    signal_id: int,
    entry: float,
    stop_loss: float,
    target: float,
    rr: float,
) -> None:
    """Rewrite the signal's rationale with coherent entry/SL/target so
    the TradeManager and the UI use the real (quote-seeded) levels.

    Preserves any human-readable prefix; replaces only the
    `entry=.. sl=.. target=.. rr=..` tail (re-appending if absent).
    """
    with session_factory() as session:
        sig = session.get(Signal, signal_id)
        if sig is None:
            return
        base = _RATIONALE_RE.sub("", sig.rationale or "").strip()
        levels_str = (
            f"entry={entry:.2f} sl={stop_loss:.2f} "
            f"target={target:.2f} rr={rr:.2f}"
        )
        sig.rationale = (f"{base} {levels_str}").strip() if base else levels_str
        session.commit()


def _persist_risk_block(
    session_factory: Callable[[], Session],
    signal: Signal,
    account_id: Optional[int],
    code: str,
    message: str,
    context: Optional[dict[str, Any]] = None,
) -> None:
    with session_factory() as session:
        # Re-attach the signal to this session.
        s = session.merge(signal)
        s.status = "blocked"
        if code and code not in (s.rationale or ""):
            s.rationale = (s.rationale or "") + f" | blocked: {code} ({message})"
        # Persist a risk_events row.
        ctx = dict(context or {})
        ctx["signal_id"] = signal.id
        ctx["account_id"] = account_id
        session.add(
            RiskEvent(
                event_type=code or "RISK_BLOCKED",
                severity="warning",
                message=message,
                context=ctx,
                halted=False,
            )
        )
        # And a "blocked" trade row so the audit trail is complete.
        session.add(
            TradeRow(
                signal_id=signal.id,
                broker_account_id=account_id,
                symbol=signal.symbol,
                side=(signal.action or "BUY"),
                quantity=0,
                price=0.0,
                order_type="blocked",
                status="rejected",
                broker_order_id=None,
                pnl=None,
            )
        )
        session.add(
            AuditLog(
                actor="system",
                action="risk.block",
                target=f"signal:{signal.id}",
                before={"status": "pending"},
                after={"status": "blocked", "code": code, "message": message},
            )
        )
        session.commit()


def _persist_trade(
    session_factory: Callable[[], Session],
    signal: Signal,
    account_id: int,
    result: OrderResult,
    slippage_pct: Optional[float] = None,
) -> None:
    """Persist the trade row for a placed/filled order.

    If a row already exists for the same `broker_order_id` (the
    paper backend creates one up front, then updates it on
    fill), UPDATE that row instead of inserting a duplicate.
    """
    with session_factory() as session:
        s = session.merge(signal)
        # Map OrderState to the trades.status column vocabulary
        # (placed / filled / cancelled / rejected / expired).
        status_map = {
            OrderState.PENDING: "placed",
            OrderState.FILLED: "filled",
            OrderState.CANCELLED: "cancelled",
            OrderState.REJECTED: "rejected",
            OrderState.EXPIRED: "expired",
        }
        new_status = status_map.get(result.state, "placed")
        existing: Optional[TradeRow] = None
        if result.broker_order_id:
            existing = (
                session.query(TradeRow)
                .filter_by(broker_order_id=result.broker_order_id)
                .one_or_none()
            )
        if existing is not None:
            existing.status = new_status
            if result.state == OrderState.FILLED and result.average_price is not None:
                existing.price = float(result.average_price)
                existing.executed_at = result.submitted_at
            existing.broker_account_id = account_id
            if slippage_pct is not None:
                existing.slippage_pct = float(slippage_pct)
        else:
            row = TradeRow(
                signal_id=s.id,
                broker_account_id=account_id,
                symbol=result.symbol,
                side=result.side.value,
                quantity=int(result.quantity),
                price=float(result.average_price or result.limit_price or result.stop_price or 0.0),
                order_type=result.order_type.value,
                status=new_status,
                broker_order_id=result.broker_order_id or None,
                slippage_pct=float(slippage_pct) if slippage_pct is not None else None,
                executed_at=result.submitted_at if result.state == OrderState.FILLED else None,
            )
            session.add(row)
        # If the order filled, also update the position row — but ONLY for
        # non-paper backends. The paper backend already mirrors its own
        # positions to the DB (paper._mirror_position); updating here too
        # DOUBLED the quantity (e.g. a BUY 17 showed as a 34-share position
        # with a 34-share exit). Fyers/live fills still update here.
        is_paper_fill = bool(
            result.broker_order_id and result.broker_order_id.startswith("PAPER-")
        )
        if result.state == OrderState.FILLED and result.filled_quantity > 0 and not is_paper_fill:
            pos = session.query(PositionRow).filter_by(symbol=result.symbol).one_or_none()
            if pos is None:
                pos = PositionRow(
                    symbol=result.symbol,
                    quantity=int(result.filled_quantity) * (1 if result.side == OrderSide.BUY else -1),
                    average_price=float(result.average_price or 0.0),
                    last_price=result.average_price,
                    unrealized_pnl=0.0,
                    strategy_id=s.strategy_id,
                )
                session.add(pos)
            else:
                # Simple weighted average update; the paper backend
                # already maintains the in-memory mirror, but we
                # keep the DB row consistent.
                if result.side == OrderSide.BUY:
                    new_qty = pos.quantity + int(result.filled_quantity)
                    pos.average_price = (
                        (pos.average_price * pos.quantity + result.average_price * result.filled_quantity)
                        / new_qty
                        if new_qty
                        else pos.average_price
                    )
                    pos.quantity = new_qty
                else:
                    pos.quantity = pos.quantity - int(result.filled_quantity)
                pos.last_price = result.average_price
        if result.state in (OrderState.FILLED, OrderState.REJECTED, OrderState.CANCELLED, OrderState.EXPIRED):
            s.status = "filled" if result.state == OrderState.FILLED else "blocked"
        session.add(
            AuditLog(
                actor="system",
                action="trade.placed" if result.state == OrderState.PENDING else "trade." + result.state.value.lower(),
                target=f"signal:{s.id}",
                after={
                    "broker_order_id": result.broker_order_id,
                    "state": result.state.value,
                    "quantity": int(result.quantity),
                    "filled_quantity": int(result.filled_quantity),
                    "average_price": result.average_price,
                },
            )
        )
        session.commit()


def _persist_trade_placed_only(
    session_factory: Callable[[], Session],
    signal: Signal,
    account_id: Optional[int],
    side: str,
    quantity: int,
    price: float,
    order_type: str,
    status: str,
    error: str,
) -> None:
    """Persist a `placed` row when the backend raised before we
    could build a full OrderResult."""
    with session_factory() as session:
        session.add(
            TradeRow(
                signal_id=signal.id,
                broker_account_id=account_id,
                symbol=signal.symbol,
                side=side,
                quantity=int(quantity),
                price=float(price),
                order_type=order_type,
                status=status,
                broker_order_id=None,
                pnl=None,
            )
        )
        session.add(
            AuditLog(
                actor="system",
                action="trade.placed_error",
                target=f"signal:{signal.id}",
                after={"error": error, "status": status},
            )
        )
        session.commit()
