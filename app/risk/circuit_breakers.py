"""Portfolio circuit breakers + kill switch (RISK.md §4, §6).

A persistent, account-wide safety layer that sits *above* the per-trade
risk engine. The engine asks "is THIS trade sound?"; the breakers ask
"should we be trading AT ALL right now?".

State lives in the singleton `RiskState` row so a halt survives a
restart. Two entry points:

  - `evaluate_breakers(session)` — run periodically (and after every
    close). Rolls the day/week/month anchors, measures equity drawdown,
    and trips breakers. Returns a summary incl. `flatten_required` so
    the caller can square off + notify. Pure DB work; does NOT itself
    place/cancel orders.

  - `entry_allowed(session, confidence=...)` — called by the execution
    manager before EVERY new entry. Fails closed while halted, inside a
    consecutive-loser cooldown (unless the signal clears the high-
    conviction resume bar), or once the daily trade count is hit.

Breakers (thresholds from Settings):

    daily loss ≥ DAILY_MAX_LOSS_PCT            -> halt + flatten (today)
    monthly drawdown ≥ MONTHLY_MAX_DRAWDOWN_PCT -> halt + flatten + paper
    weekly loss ≥ WEEKLY_MAX_LOSS_PCT          -> throttle risk %
    consecutive losers ≥ MAX_CONSECUTIVE_LOSERS -> cooldown pause
    trades today ≥ MAX_TRADES_PER_DAY          -> no new entries

Anchors reset on the IST calendar: a new day clears a daily halt, a new
week clears the risk throttle, a new month clears a monthly halt. A
manual kill (the API) only clears via the resume endpoint.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import RiskState, Trade as TradeRow
from app.logging_config import get_logger
from app.risk import position_sizer
from app.risk.market_clock import to_ist

log = get_logger(__name__)

# Disabled-reason tags — used to decide which calendar rollover clears
# the halt (a daily breach clears next day; monthly next month; a manual
# kill only clears via the resume endpoint).
REASON_DAILY = "daily_loss_limit"
REASON_MONTHLY = "monthly_drawdown_limit"
REASON_MANUAL = "manual_kill_switch"


def get_or_create_state(session: Session) -> RiskState:
    state = session.get(RiskState, 1)
    if state is None:
        state = RiskState(id=1, trading_disabled=False)
        session.add(state)
        session.flush()
    return state


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())  # Monday


def _ist_day_bounds_naive_utc(now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """[start, end) of the current IST day, expressed as NAIVE UTC
    datetimes (the schema stores naive-UTC `executed_at`)."""
    ist = to_ist(now)
    ist_midnight = ist.replace(hour=0, minute=0, second=0, microsecond=0)
    start = ist_midnight.astimezone(timezone.utc).replace(tzinfo=None)
    end = (ist_midnight + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
    return start, end


def _pct_drop(anchor: Optional[float], current: float) -> float:
    if not anchor or anchor <= 0:
        return 0.0
    return (float(anchor) - float(current)) / float(anchor) * 100.0


def entries_today(session: Session, now: Optional[datetime] = None) -> int:
    """New positions opened on the current IST day. Entry fills carry a
    NULL pnl (exits carry the realised pnl), so that's how we tell them
    apart without a side-vs-position lookup."""
    start, end = _ist_day_bounds_naive_utc(now)
    n = session.execute(
        select(func.count(TradeRow.id)).where(
            TradeRow.status == "filled",
            TradeRow.pnl.is_(None),
            TradeRow.executed_at.is_not(None),
            TradeRow.executed_at >= start,
            TradeRow.executed_at < end,
        )
    ).scalar_one()
    return int(n or 0)


def trailing_consecutive_losers(session: Session) -> int:
    """How many of the most-recent closed trades were losers, counting
    back until the first non-loss."""
    rows = session.execute(
        select(TradeRow.pnl)
        .where(TradeRow.pnl.is_not(None), TradeRow.status == "filled")
        .order_by(TradeRow.executed_at.desc().nullslast(), TradeRow.id.desc())
    ).all()
    count = 0
    for (pnl,) in rows:
        if pnl is None:
            continue
        if float(pnl) < 0:
            count += 1
        else:
            break
    return count


def evaluate_breakers(
    session: Session,
    md: Any = None,
    *,
    now: Optional[datetime] = None,
    equity: Optional[float] = None,
) -> dict[str, Any]:
    """Roll anchors, measure drawdown, trip breakers. Returns a summary.

    `equity` / `now` are injectable for tests. `flatten_required` in the
    result tells the caller to square off all positions (the breaker
    layer never places orders itself).
    """
    settings = get_settings()
    now = _as_aware_utc(now) or _utcnow()
    state = get_or_create_state(session)
    if equity is None:
        equity = position_sizer.compute_equity(session, md)
    equity = float(equity)

    ist_today = to_ist(now).date()
    week0 = _week_start(ist_today)
    month0 = ist_today.replace(day=1)
    newly: list[str] = []

    # ---- roll anchors -------------------------------------------------
    if state.day_anchor != ist_today:
        state.day_anchor = ist_today
        state.day_start_equity = equity
        if state.disabled_reason == REASON_DAILY:
            state.trading_disabled = False
            state.disabled_reason = None
        state.halted_until = None  # cooldowns don't carry overnight
    if state.week_anchor != week0:
        state.week_anchor = week0
        state.week_peak_equity = equity
        state.current_risk_pct = None  # clear the weekly throttle
    if state.month_anchor != month0:
        state.month_anchor = month0
        state.month_start_equity = equity
        if state.disabled_reason == REASON_MONTHLY:
            state.trading_disabled = False
            state.disabled_reason = None

    # Track the running weekly peak for peak→trough.
    if state.week_peak_equity is None or equity > state.week_peak_equity:
        state.week_peak_equity = equity

    flatten_required = False

    # ---- daily loss ---------------------------------------------------
    if state.day_start_equity and not state.trading_disabled:
        if _pct_drop(state.day_start_equity, equity) >= settings.DAILY_MAX_LOSS_PCT:
            state.trading_disabled = True
            state.disabled_reason = REASON_DAILY
            flatten_required = True
            newly.append("daily_loss")

    # ---- monthly drawdown --------------------------------------------
    if state.month_start_equity and state.disabled_reason != REASON_MONTHLY:
        if _pct_drop(state.month_start_equity, equity) >= settings.MONTHLY_MAX_DRAWDOWN_PCT:
            state.trading_disabled = True
            state.disabled_reason = REASON_MONTHLY
            flatten_required = True
            newly.append("monthly_drawdown")

    # ---- weekly loss -> throttle risk --------------------------------
    if state.week_peak_equity:
        if _pct_drop(state.week_peak_equity, equity) >= settings.WEEKLY_MAX_LOSS_PCT:
            throttle = float(settings.WEEKLY_BREACH_RISK_PCT)
            if state.current_risk_pct is None or state.current_risk_pct > throttle:
                state.current_risk_pct = throttle
                newly.append("weekly_loss")

    # ---- consecutive losers -> cooldown ------------------------------
    losers = trailing_consecutive_losers(session)
    if losers >= settings.MAX_CONSECUTIVE_LOSERS:
        # Only (re)arm when not already inside an active cooldown, so a
        # persistent streak doesn't push the timer forward every tick.
        cur = _as_aware_utc(state.halted_until)
        if cur is None or cur <= now:
            state.halted_until = now + timedelta(
                minutes=int(settings.CONSECUTIVE_LOSER_PAUSE_MINUTES)
            )
            newly.append("consecutive_losers")

    state.updated_at = _utcnow()
    session.commit()

    if newly:
        log.warning(
            "circuit_breaker.tripped",
            breakers=newly,
            equity=round(equity, 2),
            trading_disabled=state.trading_disabled,
            reason=state.disabled_reason,
        )

    return {
        "equity": equity,
        "day_start_equity": state.day_start_equity,
        "week_peak_equity": state.week_peak_equity,
        "month_start_equity": state.month_start_equity,
        "trading_disabled": state.trading_disabled,
        "disabled_reason": state.disabled_reason,
        "halted_until": state.halted_until.isoformat() if state.halted_until else None,
        "current_risk_pct": state.current_risk_pct,
        "trades_today": entries_today(session, now),
        "consecutive_losers": losers,
        "flatten_required": flatten_required,
        "newly_tripped": newly,
    }


def entry_allowed(
    session: Session,
    *,
    confidence: Optional[float] = None,
    now: Optional[datetime] = None,
) -> tuple[bool, Optional[str]]:
    """Gate a new entry. Returns (allowed, reason_if_blocked).

    Fails closed while trading is disabled, inside a consecutive-loser
    cooldown (unless the signal clears the high-conviction resume bar),
    or once the daily trade cap is reached.
    """
    settings = get_settings()
    now = _as_aware_utc(now) or _utcnow()
    state = get_or_create_state(session)

    if state.trading_disabled:
        return False, state.disabled_reason or "trading_disabled"

    halted_until = _as_aware_utc(state.halted_until)
    if halted_until is not None and halted_until > now:
        resume_bar = float(settings.CONSECUTIVE_LOSER_RESUME_CONFIDENCE)
        if confidence is None or float(confidence) < resume_bar:
            return False, "consecutive_loser_cooldown"
        # else: high-conviction signal is allowed to trade through the pause

    if entries_today(session, now) >= int(settings.MAX_TRADES_PER_DAY):
        return False, "max_trades_per_day"

    return True, None


def trip_manual_kill(session: Session, *, reason: str = REASON_MANUAL) -> RiskState:
    """Kill switch: disable trading until an explicit resume."""
    state = get_or_create_state(session)
    state.trading_disabled = True
    state.disabled_reason = reason
    state.updated_at = _utcnow()
    session.commit()
    log.warning("circuit_breaker.manual_kill", reason=reason)
    return state


def clear_halt(session: Session) -> RiskState:
    """Resume: clear any disable + cooldown (operator action)."""
    state = get_or_create_state(session)
    state.trading_disabled = False
    state.disabled_reason = None
    state.halted_until = None
    state.updated_at = _utcnow()
    session.commit()
    log.info("circuit_breaker.resumed")
    return state
