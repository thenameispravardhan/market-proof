"""Outcome logger (Phase 4, plan.txt item 18): what did the stock DO?

Subscribes to `signals.new` and, for every signal (approved AND blocked
— blocked signals are the counterfactuals a win-rate review needs),
records the Fyers price at signal time and again at +5 and +30 minutes.
The rows in `signal_outcomes` are:

  - the operator's soak-test report ("did the signals work?"), and
  - the labeled dataset Phase 5/6 (pre-filter model, fine-tuning) need.

Design constraints:
  - PASSIVE: never influences trading. Every failure is swallowed and
    tagged in the row's `note` column instead of raised.
  - Fyers-only pricing (project rule): quotes come from the same
    `fetch_quote` helper the rest of the app uses; no public fallback.
    When Fyers isn't connected (or market's closed and the quote is
    stale) the row simply records `no_quote_*` — data quality is
    visible, not faked.
  - Samples are asyncio tasks in-process; a restart loses pending
    samples (rows stay half-filled with a note). Good enough for v1 —
    signals only fire during market hours while the bot runs anyway.
  - `OUTCOME_LOGGER_ENABLED` (Settings, default ON — it is pure
    telemetry and changes no trading behavior).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from app.config import get_settings
from app.logging_config import get_logger
from app.services.event_bus import event_bus

log = get_logger(__name__)

CHANNEL_SIGNALS = "signals.new"

# (label, delay_seconds, price_column, move_column)
DEFAULT_SAMPLE_POINTS: tuple[tuple[str, float], ...] = (
    ("5m", 300.0),
    ("30m", 1800.0),
)


def _default_session_factory() -> Callable[[], Session]:
    from app.db.session import SessionLocal
    return SessionLocal


async def _default_quote_fn(symbol: str) -> Optional[float]:
    """Last price for an exchange symbol via the Fyers-only quote path.
    Returns None when Fyers is not connected / symbol unresolvable."""
    try:
        from app.api.market import fetch_quote
        from app.execution.symbols import resolve_fyers_symbol

        broker_symbol = resolve_fyers_symbol(symbol)
        if not broker_symbol:
            return None
        q = await fetch_quote(broker_symbol)
        if not q:
            return None
        price = q.get("last_price")
        return float(price) if price else None
    except Exception:  # noqa: BLE001 — telemetry never raises
        return None


class OutcomeLogger:
    """Passive signals.new → signal_outcomes recorder.

    Lifecycle mirrors the analyzer Service (start/stop idempotent).
    `sample_points` and `quote_fn` are injectable so tests run in
    milliseconds with a stub feed.
    """

    def __init__(
        self,
        *,
        session_factory: Optional[Callable[[], Session]] = None,
        quote_fn: Optional[Callable[[str], Any]] = None,
        sample_points: tuple[tuple[str, float], ...] = DEFAULT_SAMPLE_POINTS,
    ) -> None:
        self._session_factory = session_factory or _default_session_factory()
        self._quote_fn = quote_fn or _default_quote_fn
        self._sample_points = sample_points
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        self._sub_queue: Optional[asyncio.Queue[Any]] = None
        self._pending: set[asyncio.Task[None]] = set()

    # -- lifecycle --------------------------------------------------------

    def start(self) -> asyncio.Task[None]:
        if self._task is not None and not self._task.done():
            return self._task
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="outcome-logger")
        return self._task

    def stop(self) -> None:
        self._stop_event.set()
        if self._sub_queue is not None:
            event_bus.unsubscribe(CHANNEL_SIGNALS, self._sub_queue)
            self._sub_queue = None
        for t in list(self._pending):
            t.cancel()

    async def wait_until_stopped(self) -> None:
        if self._task is None:
            return
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    # -- main loop --------------------------------------------------------

    async def _run(self) -> None:
        self._sub_queue = event_bus.subscribe(CHANNEL_SIGNALS)
        log.info("outcome_logger.start")
        try:
            while not self._stop_event.is_set():
                try:
                    event = await asyncio.wait_for(self._sub_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                try:
                    await self._handle_signal(event.payload)
                except Exception:  # noqa: BLE001
                    log.exception("outcome_logger.handler_crashed")
        finally:
            log.info("outcome_logger.stop")
            if self._sub_queue is not None:
                event_bus.unsubscribe(CHANNEL_SIGNALS, self._sub_queue)
                self._sub_queue = None

    async def _handle_signal(self, payload: dict[str, Any]) -> None:
        settings = get_settings()
        if not bool(getattr(settings, "OUTCOME_LOGGER_ENABLED", True)):
            return
        symbol = (payload.get("symbol") or "").strip()
        signal_id = payload.get("signal_id")
        if not symbol:
            return

        baseline = await self._quote_fn(symbol)
        outcome_id = await asyncio.get_running_loop().run_in_executor(
            None, self._insert_row, payload, baseline
        )
        log.info(
            "outcome_logger.tracking",
            signal_id=signal_id,
            symbol=symbol,
            price_at_signal=baseline,
        )
        task = asyncio.create_task(
            self._sample_later(outcome_id, symbol, baseline),
            name=f"outcome-samples-{outcome_id}",
        )
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _sample_later(
        self, outcome_id: int, symbol: str, baseline: Optional[float]
    ) -> None:
        loop = asyncio.get_running_loop()
        elapsed = 0.0
        for label, at_seconds in self._sample_points:
            delay = max(0.0, at_seconds - elapsed)
            elapsed = at_seconds
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                return  # stopping — leave the row half-filled
            except asyncio.TimeoutError:
                pass
            price = await self._quote_fn(symbol)
            await loop.run_in_executor(
                None, self._update_sample, outcome_id, label, price, baseline
            )
        await loop.run_in_executor(None, self._mark_complete, outcome_id)

    # -- sync DB helpers (run in executor) ---------------------------------

    def _insert_row(
        self, payload: dict[str, Any], baseline: Optional[float]
    ) -> int:
        from app.db.models import SignalOutcome

        with self._session_factory() as session:
            row = SignalOutcome(
                signal_id=payload.get("signal_id"),
                symbol=(payload.get("symbol") or "").strip(),
                action=str(payload.get("action") or "HOLD"),
                signal_status=payload.get("status"),
                price_at_signal=baseline,
                note=None if baseline is not None else "no_quote_at_signal",
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row.id

    def _update_sample(
        self,
        outcome_id: int,
        label: str,
        price: Optional[float],
        baseline: Optional[float],
    ) -> None:
        from app.db.models import SignalOutcome

        with self._session_factory() as session:
            row = session.get(SignalOutcome, outcome_id)
            if row is None:
                return
            move: Optional[float] = None
            if price is not None and baseline:
                move = round((price - baseline) / baseline * 100.0, 4)
            if label == "5m":
                row.price_5m = price
                row.move_5m_pct = move
            elif label == "30m":
                row.price_30m = price
                row.move_30m_pct = move
            if price is None:
                row.note = f"no_quote_{label}" if not row.note else f"{row.note},no_quote_{label}"
            session.commit()

    def _mark_complete(self, outcome_id: int) -> None:
        from app.db.models import SignalOutcome

        with self._session_factory() as session:
            row = session.get(SignalOutcome, outcome_id)
            if row is None:
                return
            row.completed_at = datetime.now(timezone.utc)
            if row.note is None:
                row.note = "ok"
            session.commit()
