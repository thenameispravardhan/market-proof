"""Signal outcomes — the closed-loop feedback view (Phase 4 completion).

The OutcomeLogger has been passively recording what the stock ACTUALLY
did after every signal (price at signal, +5m, +30m) — approved and
blocked alike. These endpoints surface that table so the operator can
finally answer "which event types make money?" empirically and tune the
rules on real data.

  GET /api/outcomes              Joined rows (outcome + signal status +
                                 analysis sentiment + announcement event
                                 type/headline), newest first.
  GET /api/outcomes/summary      Per-event-type aggregates: samples,
                                 avg 5m/30m move, directional accuracy
                                 (ai_was_correct rate), taken vs blocked.
  GET /api/outcomes/export.csv   The joined rows as CSV — the offline ML
                                 training set.

`ai_was_correct` = the 5-minute move agreed with the signal's direction
(BUY → move_5m > 0, SELL → move_5m < 0); None while the sample is
incomplete. `trade_taken` = the signal reached execution (not blocked).
"""
from __future__ import annotations

import csv
import io
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Analysis, Announcement, Signal, SignalOutcome
from app.db.session import get_db
from app.logging_config import get_logger

router = APIRouter(prefix="/api/outcomes", tags=["outcomes"])
log = get_logger(__name__)

# Signal statuses that mean "the trade went to the broker".
_TAKEN_STATUSES = ("filled", "closed")

_CSV_COLUMNS = (
    "outcome_id", "signal_id", "symbol", "action", "event_type",
    "sentiment", "sentiment_score", "confidence",
    "price_at_signal", "price_5m", "price_30m",
    "move_5m_pct", "move_30m_pct",
    "ai_was_correct", "trade_taken", "signal_status", "note", "created_at",
)


def _ai_was_correct(action: str, move_5m_pct: Optional[float]) -> Optional[bool]:
    if move_5m_pct is None:
        return None
    if action == "BUY":
        return move_5m_pct > 0
    if action == "SELL":
        return move_5m_pct < 0
    return None


def _joined_rows(
    db: Session,
    *,
    limit: int,
    event_type: Optional[str] = None,
    action: Optional[str] = None,
) -> list[dict[str, Any]]:
    stmt = (
        select(SignalOutcome, Signal, Analysis, Announcement)
        .join(Signal, SignalOutcome.signal_id == Signal.id, isouter=True)
        .join(Analysis, Signal.analysis_id == Analysis.id, isouter=True)
        .join(Announcement, Analysis.announcement_id == Announcement.id, isouter=True)
        .order_by(SignalOutcome.id.desc())
        .limit(limit)
    )
    if event_type:
        stmt = stmt.where(Announcement.event_type == event_type.upper())
    if action:
        stmt = stmt.where(SignalOutcome.action == action.upper())
    out: list[dict[str, Any]] = []
    for outcome, signal, analysis, ann in db.execute(stmt).all():
        act = str(outcome.action or "").upper()
        status = getattr(signal, "status", None)
        out.append(
            {
                "outcome_id": outcome.id,
                "signal_id": outcome.signal_id,
                "symbol": outcome.symbol,
                "action": act,
                "event_type": getattr(ann, "event_type", None),
                "headline": getattr(ann, "headline", None),
                "sentiment": getattr(analysis, "sentiment", None),
                "sentiment_score": getattr(analysis, "sentiment_score", None),
                "confidence": getattr(analysis, "confidence", None),
                "price_at_signal": outcome.price_at_signal,
                "price_5m": outcome.price_5m,
                "price_30m": outcome.price_30m,
                "move_5m_pct": outcome.move_5m_pct,
                "move_30m_pct": outcome.move_30m_pct,
                "ai_was_correct": _ai_was_correct(act, outcome.move_5m_pct),
                "trade_taken": (status in _TAKEN_STATUSES) if status else False,
                "signal_status": status,
                "note": outcome.note,
                "created_at": (
                    outcome.created_at.isoformat() if outcome.created_at else None
                ),
            }
        )
    return out


@router.get("")
def list_outcomes(
    limit: int = Query(200, ge=1, le=1000),
    event_type: Optional[str] = Query(None),
    action: Optional[str] = Query(None, description="BUY | SELL"),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return _joined_rows(db, limit=limit, event_type=event_type, action=action)


@router.get("/summary")
def outcomes_summary(
    limit: int = Query(1000, ge=10, le=5000),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Per-event-type performance: the empirical answer to "what should
    my rules trade?". Directional accuracy uses the 5m move; the 30m
    columns show whether the edge persists or mean-reverts."""
    rows = _joined_rows(db, limit=limit)
    by_event: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        key = r.get("event_type") or "UNKNOWN"
        by_event.setdefault(key, []).append(r)

    def _avg(vals: list[float]) -> Optional[float]:
        return round(sum(vals) / len(vals), 3) if vals else None

    summary = []
    for event_type, items in by_event.items():
        m5 = [r["move_5m_pct"] for r in items if r["move_5m_pct"] is not None]
        m30 = [r["move_30m_pct"] for r in items if r["move_30m_pct"] is not None]
        verdicts = [r["ai_was_correct"] for r in items if r["ai_was_correct"] is not None]
        taken = sum(1 for r in items if r["trade_taken"])
        summary.append(
            {
                "event_type": event_type,
                "samples": len(items),
                "with_5m_data": len(m5),
                "avg_move_5m_pct": _avg(m5),
                "avg_move_30m_pct": _avg(m30),
                "accuracy_5m_pct": (
                    round(sum(1 for v in verdicts if v) / len(verdicts) * 100.0, 1)
                    if verdicts
                    else None
                ),
                "taken": taken,
                "blocked": len(items) - taken,
            }
        )
    summary.sort(key=lambda s: s["samples"], reverse=True)
    return {"total": len(rows), "by_event_type": summary}


@router.get("/export.csv")
def export_csv(
    limit: int = Query(1000, ge=1, le=5000),
    db: Session = Depends(get_db),
) -> PlainTextResponse:
    """The joined outcome rows as CSV — offline analysis / ML training."""
    rows = _joined_rows(db, limit=limit)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(_CSV_COLUMNS), extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return PlainTextResponse(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=signal_outcomes.csv"},
    )
