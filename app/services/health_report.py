"""Daily health report — the operator's end-of-day summary.

Every trading day at HEALTH_REPORT_TIME_IST (default 15:45, just after
the EOD square-off) the scheduler compiles a one-screen report and
publishes it on the `system.report` bus channel; the NotificationManager
relays it to every channel whose events filter includes "report"
(Telegram / email / discord / webhook). No dashboard login needed to
know whether the bot traded, what it blocked, and whether anything broke.

Report contents (IST trading day):
  - signals processed / approved / blocked (risk) / drift-deferred
  - trades executed (entry fills) and positions closed (+ realised P&L)
  - open positions right now
  - circuit-breaker status (OK / HALTED + reason / cooldown)
  - Fyers WS last tick age (feed health)
  - database size on disk

`compile_health_report(db)` is pure-read and also serves
GET /api/metrics/health-report; POST .../health-report/send triggers the
same publish path manually.

The same service also runs a PRE-OPEN preflight at
HEALTH_REPORT_PREFLIGHT_TIME_IST (default 09:05) and publishes on
`system.error` only when it finds a problem — see `compile_preflight`.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import (
    Position as PositionRow,
    RiskEvent,
    Signal,
    Trade as TradeRow,
)
from app.db.session import engine
from app.logging_config import get_logger
from app.risk.market_clock import IST, _is_trading_day, to_ist
from app.services.event_bus import event_bus

log = get_logger(__name__)

CHANNEL_SYSTEM_REPORT = "system.report"
CHANNEL_SYSTEM_ERROR = "system.error"

# The preflight probe: any live Fyers quote proves the daily OAuth token
# is still good. NIFTY 50 always trades, so a None here is the token, not
# the symbol.
PREFLIGHT_PROBE_SYMBOL = "NSE:NIFTY50-INDEX"

# Risk-event types that mean "deferred by the entry state machine's
# anti-chase gate", as opposed to a hard risk block.
_DRIFT_EVENT_TYPES = ("EXCESSIVE_DRIFT", "RETRACEMENT_EXPIRED", "SYMBOL_LOCKED")


def _ist_day_bounds_utc_naive(now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """The current IST calendar day as naive-UTC bounds (the DB stores
    naive UTC timestamps)."""
    ist_now = to_ist(now)
    day_start_ist = datetime.combine(ist_now.date(), dt_time(0, 0), tzinfo=IST)
    start_utc = day_start_ist.astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc, start_utc + timedelta(days=1)


def _db_size_mb() -> Optional[float]:
    """Size of the SQLite file behind DATABASE_URL, in MB. None for
    non-file databases (in-memory tests)."""
    url = str(getattr(get_settings(), "DATABASE_URL", "") or "")
    if not url.startswith("sqlite") or ":memory:" in url:
        return None
    # sqlite:///./data/trading.db → ./data/trading.db
    path = url.split("///", 1)[-1]
    try:
        return round(os.path.getsize(path) / (1024 * 1024), 2)
    except OSError:
        return None


def _ws_last_tick(market_data: Any) -> Optional[str]:
    """Timestamp of the newest Fyers-WS tick on the bus, or None when the
    socket has never delivered one (paper mode / feed down)."""
    if market_data is None:
        return None
    try:
        quotes = dict(market_data._quotes)  # documented private read
    except Exception:  # noqa: BLE001
        return None
    newest: Optional[datetime] = None
    for q in quotes.values():
        if (q.extra or {}).get("source") != "fyers_ws":
            continue
        ts = q.timestamp
        if newest is None or ts > newest:
            newest = ts
    return newest.isoformat() if newest else None


def compile_health_report(
    db: Session, *, market_data: Any = None, now: Optional[datetime] = None
) -> dict[str, Any]:
    """One IST trading day, summarised. Read-only."""
    from app.risk import circuit_breakers as cb

    start, end = _ist_day_bounds_utc_naive(now)

    def _count(stmt) -> int:
        return int(db.execute(stmt).scalar_one_or_none() or 0)

    signals_total = _count(
        select(func.count()).select_from(Signal).where(
            Signal.created_at >= start, Signal.created_at < end
        )
    )
    signals_blocked = _count(
        select(func.count()).select_from(Signal).where(
            Signal.created_at >= start, Signal.created_at < end,
            Signal.status == "blocked",
        )
    )
    drift_deferred = _count(
        select(func.count()).select_from(RiskEvent).where(
            RiskEvent.created_at >= start, RiskEvent.created_at < end,
            RiskEvent.event_type.in_(_DRIFT_EVENT_TYPES),
        )
    )
    # Entry fills carry no pnl; exit legs carry realised pnl.
    trades_executed = _count(
        select(func.count()).select_from(TradeRow).where(
            TradeRow.created_at >= start, TradeRow.created_at < end,
            TradeRow.status == "filled", TradeRow.pnl.is_(None),
        )
    )
    positions_closed = _count(
        select(func.count()).select_from(TradeRow).where(
            TradeRow.created_at >= start, TradeRow.created_at < end,
            TradeRow.status == "filled", TradeRow.pnl.isnot(None),
        )
    )
    realized_pnl = float(
        db.execute(
            select(func.coalesce(func.sum(TradeRow.pnl), 0.0)).where(
                TradeRow.created_at >= start, TradeRow.created_at < end,
                TradeRow.pnl.isnot(None),
            )
        ).scalar_one_or_none()
        or 0.0
    )
    open_positions = _count(
        select(func.count()).select_from(PositionRow).where(PositionRow.quantity != 0)
    )
    critical_events = _count(
        select(func.count()).select_from(RiskEvent).where(
            RiskEvent.created_at >= start, RiskEvent.created_at < end,
            RiskEvent.severity == "critical",
        )
    )

    state = cb.get_or_create_state(db)
    halted_until = state.halted_until.isoformat() if state.halted_until else None
    if state.trading_disabled:
        breaker_status = f"HALTED ({state.disabled_reason or 'manual'})"
    elif halted_until:
        breaker_status = f"COOLDOWN (until {halted_until})"
    else:
        breaker_status = "OK"

    return {
        "date_ist": to_ist(now).date().isoformat(),
        "signals_total": signals_total,
        "signals_blocked_risk": signals_blocked,
        "signals_drift_deferred": drift_deferred,
        "trades_executed": trades_executed,
        "positions_closed": positions_closed,
        "realized_pnl": round(realized_pnl, 2),
        "open_positions": open_positions,
        "critical_events": critical_events,
        "circuit_breaker": breaker_status,
        "trading_disabled": bool(state.trading_disabled),
        "halted_until": halted_until,
        "fyers_ws_last_tick": _ws_last_tick(market_data),
        "db_size_mb": _db_size_mb(),
    }


def format_report(report: dict[str, Any]) -> tuple[str, str]:
    """(subject, body) for the notification channels — plain text that
    reads well in Telegram and email alike."""
    pnl = report.get("realized_pnl") or 0.0
    pnl_str = f"{'+' if pnl >= 0 else ''}₹{pnl:,.2f}"
    subject = (
        f"Daily health report {report['date_ist']} — "
        f"{report['trades_executed']} trades, {pnl_str}, "
        f"breaker {report['circuit_breaker']}"
    )
    lines = [
        f"Date (IST):                 {report['date_ist']}",
        f"Signals processed:          {report['signals_total']}",
        f"Blocked by risk engine:     {report['signals_blocked_risk']}",
        f"Drift/retracement deferred: {report['signals_drift_deferred']}",
        f"Trades executed:            {report['trades_executed']}",
        f"Positions closed:           {report['positions_closed']}",
        f"Realised P&L:               {pnl_str}",
        f"Open positions now:         {report['open_positions']}",
        f"Critical events today:      {report['critical_events']}",
        f"Circuit breaker:            {report['circuit_breaker']}",
        f"Fyers WS last tick:         {report.get('fyers_ws_last_tick') or 'never (paper / feed down)'}",
    ]
    if report.get("db_size_mb") is not None:
        lines.append(f"Database size:              {report['db_size_mb']} MB")
    return subject, "\n".join(lines)


def _db_integrity_problem() -> Optional[str]:
    """`PRAGMA quick_check` on the live SQLite file; None when it is clean.

    A malformed page is a SILENT outage. Reads keep succeeding from the
    healthy btrees, so the dashboard, the monitors and the LLM all look
    fine, while every INSERT fails with "disk I/O error" and the nightly
    backup refuses to rotate. On 2026-08-25 a corrupt `risk_state` page
    ran five days that way — 1,715 failed inserts and three skipped
    backups — because nothing ever checked. quick_check skips the index
    cross-checks, so it is cheap enough to run daily on a 160 MB file.
    """
    try:
        with engine.connect() as conn:
            result = conn.exec_driver_sql("PRAGMA quick_check(1)").scalar()
    except Exception as e:  # noqa: BLE001
        log.warning("preflight.integrity_probe_failed", error=str(e))
        return f"Database integrity check could not run ({e})."
    if str(result).strip().lower() != "ok":
        return (
            f"Database is CORRUPT (quick_check: {result}) — inserts are failing "
            "and backups have stopped rotating. Restore per the runbook before "
            "the open."
        )
    return None


def _resource_problems() -> list[str]:
    """RAM/disk warnings from the same source the Dashboard reads.

    Reuses `app.api.system` rather than re-deriving the numbers, so the
    alarm and the on-screen bars can never disagree. Never raises — a
    broken probe is silent here because the DB and Fyers checks above are
    the ones worth waking someone for.
    """
    try:
        from app.api.system import system_resources

        return list(system_resources().get("warnings", []))
    except Exception as e:  # noqa: BLE001
        log.warning("preflight.resource_probe_failed", error=str(e))
        return []


async def compile_preflight() -> list[str]:
    """Pre-open readiness problems, or [] when the bot is good to trade.

    Both checks cover failures that are invisible until the market is
    already running, and both have cost a full trading day:

      - the Fyers access token expires DAILY and is re-authed by hand;
        without it every entry blocks NO_LIVE_PRICE while REST, the LLM
        and the rules engine all keep looking healthy.
      - AI analysis switched off skips filings permanently — they are
        placeholder-marked and never re-analysed.
      - a corrupt SQLite page fails every INSERT while every READ still
        works, so the whole dashboard reads healthy while nothing is
        recorded and the nightly backup silently stops rotating.

    Read-only and never raises: a broken probe reads as a problem, which
    is the safe direction for an alarm."""
    problems: list[str] = []

    if not bool(getattr(get_settings(), "AI_ANALYSIS_ENABLED", True)):
        problems.append(
            "AI analysis is OFF — filings will be skipped and are never re-analysed."
        )

    corrupt = _db_integrity_problem()
    if corrupt:
        problems.append(corrupt)

    # RAM/disk headroom. Both have already cost a full trading day (an OOM
    # kill on 2026-08-15, and three SQLite corruptions on a box that was
    # paging), and both are gradual — 09:05 is early enough to act.
    problems.extend(_resource_problems())

    # Lazy import: app.api.market reaches back into app.main for the
    # shared ExecutionManager.
    from app.api.market import fetch_quote

    try:
        quote = await fetch_quote(PREFLIGHT_PROBE_SYMBOL)
    except Exception as e:  # noqa: BLE001
        log.warning("preflight.probe_failed", error=str(e))
        quote = None
    if quote is None:
        problems.append(
            f"Fyers is not serving quotes ({PREFLIGHT_PROBE_SYMBOL}) — re-auth in the "
            "UI or every entry today blocks with NO_LIVE_PRICE."
        )

    return problems


def _default_session_factory() -> Callable[[], Session]:
    from app.db.session import SessionLocal
    return SessionLocal


class HealthReportService:
    """Fires the daily report once per IST day at HEALTH_REPORT_TIME_IST.

    The loop wakes every ~30s; the report fires when the IST clock passes
    the configured time and hasn't fired for that date yet. Gated off
    under TESTING (tests call `send_now()` / the API instead)."""

    def __init__(
        self,
        *,
        market_data: Any = None,
        session_factory: Optional[Callable[[], Session]] = None,
    ) -> None:
        self._md = market_data
        self._session_factory = session_factory or _default_session_factory()
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        self._last_sent_date: Optional[str] = None
        self._last_preflight_date: Optional[str] = None

    def start(self) -> Optional[asyncio.Task[None]]:
        if getattr(get_settings(), "TESTING", 0):
            return None
        if not bool(getattr(get_settings(), "HEALTH_REPORT_ENABLED", True)):
            return None
        if self._task is not None and not self._task.done():
            return self._task
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="health-report")
        return self._task

    def stop(self) -> None:
        self._stop_event.set()

    async def wait_until_stopped(self) -> None:
        if self._task is None:
            return
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    def _due(self, now: Optional[datetime] = None) -> bool:
        settings = get_settings()
        raw = str(getattr(settings, "HEALTH_REPORT_TIME_IST", "15:45") or "15:45")
        try:
            hh, mm = raw.split(":")
            fire_at = dt_time(hour=int(hh), minute=int(mm))
        except (TypeError, ValueError):
            fire_at = dt_time(hour=15, minute=45)
        ist_now = to_ist(now)
        if ist_now.date().isoformat() == self._last_sent_date:
            return False
        return ist_now.time() >= fire_at

    def _preflight_due(self, now: Optional[datetime] = None) -> bool:
        raw = str(
            getattr(get_settings(), "HEALTH_REPORT_PREFLIGHT_TIME_IST", "") or ""
        ).strip()
        if not raw:
            return False
        try:
            hh, mm = raw.split(":")
            fire_at = dt_time(hour=int(hh), minute=int(mm))
        except (TypeError, ValueError):
            return False
        ist_now = to_ist(now)
        if not _is_trading_day(ist_now):
            return False
        if ist_now.date().isoformat() == self._last_preflight_date:
            return False
        return ist_now.time() >= fire_at

    async def run_preflight(self, now: Optional[datetime] = None) -> list[str]:
        """Run the pre-open checks and alert ONLY on problems — a daily
        all-clear ping is an alarm that gets muted. `now` is the same
        clock `_preflight_due` was asked about, so the once-per-day stamp
        can't land on a different date than the check."""
        problems = await compile_preflight()
        self._last_preflight_date = to_ist(now).date().isoformat()
        if not problems:
            log.info("preflight.ok", date_ist=self._last_preflight_date)
            return problems
        body = "\n".join(f"- {p}" for p in problems)
        await event_bus.publish(
            CHANNEL_SYSTEM_ERROR,
            {
                "subject": "Pre-market preflight FAILED",
                "body": f"The bot is not ready to trade today:\n{body}",
                "error": "preflight_failed",
                "problems": problems,
            },
        )
        log.warning("preflight.failed", problems=problems)
        return problems

    async def send_now(self) -> dict[str, Any]:
        """Compile + publish immediately (the scheduler's firing path)."""
        loop = asyncio.get_running_loop()

        def _compile() -> dict[str, Any]:
            with self._session_factory() as session:
                return compile_health_report(session, market_data=self._md)

        report = await loop.run_in_executor(None, _compile)
        subject, body = format_report(report)
        delivered = await event_bus.publish(
            CHANNEL_SYSTEM_REPORT,
            {"subject": subject, "body": body, "report": report},
        )
        self._last_sent_date = report["date_ist"]
        log.info(
            "health_report.sent",
            date_ist=report["date_ist"], subscribers=delivered,
            trades=report["trades_executed"], pnl=report["realized_pnl"],
        )
        return report

    async def _run(self) -> None:
        log.info("health_report.start")
        try:
            while not self._stop_event.is_set():
                try:
                    if self._due():
                        await self.send_now()
                    if self._preflight_due():
                        await self.run_preflight()
                except Exception:  # noqa: BLE001
                    log.exception("health_report.tick_failed")
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=30.0)
                    break
                except asyncio.TimeoutError:
                    pass
        finally:
            log.info("health_report.stop")
