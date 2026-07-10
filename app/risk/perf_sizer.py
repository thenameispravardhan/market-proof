"""Performance-weighted sizer — risk more on what actually works.

A blanket MAX_CAPITAL_RISK_PCT treats every event type the same, but the
pipeline is empirically better at some events than others. This module
computes each event type's realised track record from CLOSED trades
(exit legs carry pnl + r_multiple) and tiers the per-trade risk %:

    win rate > HIGH_WIN_RATE  AND avg R > HIGH_AVG_R  → PERF_SIZER_HIGH_RISK_PCT
    win rate < LOW_WIN_RATE   OR  avg R < LOW_AVG_R   → PERF_SIZER_LOW_RISK_PCT
    otherwise (or not enough data)                    → the configured base %

Fail-safe by construction: with fewer than PERF_SIZER_MIN_TRADES closed
trades for the event type (a fresh install, a rare event) the base risk
%% applies unchanged — the sizer only ever acts on real evidence. The
risk engine applies the tier BEFORE the ramp / weekly-throttle / VIX
multipliers, so every existing safety clamp still applies on top.

Event-type resolution walks trades.signal_id → signals.analysis_id →
analyses.announcement_id → announcements.event_type (the same join the
outcome viewer uses).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Analysis, Announcement, Signal, Trade
from app.logging_config import get_logger

log = get_logger(__name__)


@dataclass
class EventPerformance:
    """Realised track record of one event type."""

    event_type: str
    trades: int
    wins: int
    win_rate: float                 # 0..1
    avg_r: Optional[float]          # None when no r_multiple data exists

    def as_context(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "trades": self.trades,
            "wins": self.wins,
            "win_rate": round(self.win_rate, 4),
            "avg_r": round(self.avg_r, 4) if self.avg_r is not None else None,
        }


def event_performance(
    session: Session, event_type: str, *, lookback: int = 200
) -> Optional[EventPerformance]:
    """Win rate + average realised R for `event_type` over the last
    `lookback` closed trades. None when there are no closed trades."""
    if not event_type:
        return None
    rows = session.execute(
        select(Trade.pnl, Trade.r_multiple)
        .join(Signal, Trade.signal_id == Signal.id)
        .join(Analysis, Signal.analysis_id == Analysis.id)
        .join(Announcement, Analysis.announcement_id == Announcement.id)
        .where(
            Announcement.event_type == event_type,
            Trade.status == "filled",
            Trade.pnl.isnot(None),          # exit legs only
        )
        .order_by(Trade.id.desc())
        .limit(int(lookback))
    ).all()
    if not rows:
        return None
    pnls = [float(p) for (p, _r) in rows]
    rs = [float(r) for (_p, r) in rows if r is not None]
    wins = sum(1 for p in pnls if p > 0)
    return EventPerformance(
        event_type=event_type,
        trades=len(pnls),
        wins=wins,
        win_rate=wins / len(pnls),
        avg_r=(sum(rs) / len(rs)) if rs else None,
    )


def performance_risk_pct(
    session: Session,
    event_type: Optional[str],
    base_pct: float,
    settings: Any,
) -> tuple[float, Optional[dict[str, Any]]]:
    """The tiered per-trade risk %% for this event type.

    Returns (risk_pct, context) — context is None when the sizer didn't
    act (disabled / no event type / not enough data), else a dict for
    the risk decision's audit context.
    """
    if not bool(getattr(settings, "PERF_SIZER_ENABLED", True)):
        return (base_pct, None)
    if not event_type:
        return (base_pct, None)
    try:
        perf = event_performance(
            session, event_type,
            lookback=int(getattr(settings, "PERF_SIZER_LOOKBACK_TRADES", 200)),
        )
    except Exception:  # noqa: BLE001
        log.exception("perf_sizer.query_failed", event_type=event_type)
        return (base_pct, None)
    min_trades = int(getattr(settings, "PERF_SIZER_MIN_TRADES", 10))
    if perf is None or perf.trades < min_trades:
        return (base_pct, None)

    high_wr = float(getattr(settings, "PERF_SIZER_HIGH_WIN_RATE", 0.60))
    high_r = float(getattr(settings, "PERF_SIZER_HIGH_AVG_R", 1.5))
    low_wr = float(getattr(settings, "PERF_SIZER_LOW_WIN_RATE", 0.50))
    low_r = float(getattr(settings, "PERF_SIZER_LOW_AVG_R", 1.0))
    high_pct = float(getattr(settings, "PERF_SIZER_HIGH_RISK_PCT", 1.0))
    low_pct = float(getattr(settings, "PERF_SIZER_LOW_RISK_PCT", 0.5))

    # Demotion first (protective): a weak win rate OR weak R cuts risk.
    # Promotion needs BOTH a strong win rate AND strong R. Missing R data
    # (older trades without r_multiple) can never promote — only the base
    # or the demotion tier apply.
    tier = "base"
    risk_pct = float(base_pct)
    if perf.win_rate < low_wr or (perf.avg_r is not None and perf.avg_r < low_r):
        tier, risk_pct = "low", low_pct
    elif perf.avg_r is not None and perf.win_rate > high_wr and perf.avg_r > high_r:
        tier, risk_pct = "high", high_pct

    context = {**perf.as_context(), "tier": tier, "risk_pct": risk_pct,
               "base_pct": float(base_pct)}
    if tier != "base":
        log.info("perf_sizer.tiered", **context)
    return (risk_pct, context)
