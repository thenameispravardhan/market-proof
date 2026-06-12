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
    TradingBackend,
)
from app.execution.fyers_live import FyersLiveBackend
from app.execution.market_data import MarketDataBus
from app.execution.paper import PaperBackend
from app.logging_config import get_logger
from app.risk.engine import RiskEngine
from app.services.event_bus import event_bus

log = get_logger(__name__)


# Channels the manager publishes on.
CHANNEL_TRADE_EXECUTED = "trade.executed"
CHANNEL_RISK_BLOCKED = "risk.blocked"
CHANNEL_SETTINGS_UPDATED = "settings.updated"


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
    ) -> None:
        self._risk = risk_engine or RiskEngine(market_data=market_data)
        self._md = market_data or MarketDataBus()
        # Wire market_data into the risk engine so quote lookups
        # work end-to-end.
        if not hasattr(self._risk, "_md") or self._risk._md is None:  # type: ignore[attr-defined]
            self._risk._md = self._md  # type: ignore[attr-defined]
        self._session_factory = session_factory or _default_session_factory()
        self._backends: dict[int, TradingBackend] = dict(backends or {})
        # A single paper backend instance is reused for all paper
        # accounts. Tests can pre-seed this.
        self._paper = paper_backend or PaperBackend(market_data=self._md)
        self._settings_provider = settings_provider or get_settings
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
                for q in (self._sub_queue, self._settings_sub_queue):
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
        finally:
            log.info("execution_manager.stop")
            if self._sub_queue is not None:
                event_bus.unsubscribe(CHANNEL_NEW_SIGNAL, self._sub_queue)
                self._sub_queue = None
            if self._settings_sub_queue is not None:
                event_bus.unsubscribe(CHANNEL_SETTINGS_UPDATED, self._settings_sub_queue)
                self._settings_sub_queue = None
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
        signal, strategy, account, levels = loaded
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

        # Step 3: compute entry / SL / target. Use parsed rationale
        # first, then cached quote, then 'no_entry_price' block.
        entry = levels.get("entry")
        stop_loss = levels.get("stop_loss")
        target = levels.get("target")
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
        order_type = OrderType.MARKET
        # If the signal has explicit limit/stop from T3's
        # action_params, honour them.
        # (v1: T3 only emits MARKET-style signals.)
        try:
            result = await backend.place_order(
                signal=signal,
                symbol=signal.symbol,
                side=side,
                quantity=int(decision.sizing.qty),
                order_type=order_type,
                limit_price=stop_loss if order_type == OrderType.LIMIT else None,
                stop_price=stop_loss if order_type in (OrderType.STOP_LOSS, OrderType.STOP_LOSS_MARKET) else None,
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

        # Step 6: persist the trade row (status from result).
        await asyncio.get_running_loop().run_in_executor(
            None, _persist_trade,
            self._session_factory, signal, account.id, result,
        )
        await event_bus.publish(
            CHANNEL_TRADE_EXECUTED,
            {
                "signal_id": signal.id,
                "account_id": account.id,
                "symbol": signal.symbol,
                "side": side.value,
                "quantity": int(decision.sizing.qty),
                "broker_order_id": result.broker_order_id,
                "state": result.state.value,
                "error": result.error,
                "backend": getattr(backend, "name", type(backend).__name__),
            },
        )
        return {
            "approved": True,
            "broker_order_id": result.broker_order_id,
            "state": result.state.value,
            "qty": int(decision.sizing.qty),
            "backend": getattr(backend, "name", type(backend).__name__),
        }

    # -- backend selection ---------------------------------------------

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
        # Live.
        if not getattr(account, "app_id", None) or not getattr(account, "access_token", None):
            log.warning(
                "execution_manager.account_missing_creds",
                account_id=account.id, name=account.name,
            )
            return None
        backend = FyersLiveBackend(
            app_id=account.app_id,
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
) -> Optional[tuple[Optional[Signal], Optional[Strategy], Optional[BrokerAccount], dict[str, float]]]:
    with session_factory() as session:
        signal = session.get(Signal, signal_id)
        if signal is None:
            return (None, None, None, {})
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
        levels = parse_rationale_levels(signal.rationale)
        return (signal, strategy, account, levels)


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
                executed_at=result.submitted_at if result.state == OrderState.FILLED else None,
            )
            session.add(row)
        # If the order filled, also update the position row.
        if result.state == OrderState.FILLED and result.filled_quantity > 0:
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
