"""Observability endpoints — the "latency budget" view + health report.

  GET  /api/metrics/pipeline-latency   p50/p95/p99 of the per-analysis
       timing instrumentation (analyses.raw_response.timings), the
       news-age distribution, the pipeline-deadline hit rate, the
       fast-track vs LLM split, and a per-analysis series for the
       dashboard sparkline.
  GET  /api/metrics/health-report      Compile the daily health report
       on demand (same payload the 15:45 IST scheduler sends).
  POST /api/metrics/health-report/send Compile AND publish it through
       the notification channels (event type "report").

The timing data was always collected (Phase 0) but buried in JSON —
these endpoints surface it so pipeline degradation is visible BEFORE it
costs P&L (a p95 creeping from 2.5s to 4s = DeepSeek congested → the
operator can flip to fast-track-only).
"""
from __future__ import annotations

import math
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Analysis, Signal
from app.db.session import get_db
from app.logging_config import get_logger
from app.services import health_report as health_report_svc
from app.services.event_bus import event_bus

router = APIRouter(prefix="/api/metrics", tags=["metrics"])
log = get_logger(__name__)


# News-age histogram bucket upper bounds (seconds); the last bucket is
# open-ended. Chosen around the freshness gates (MAX_NEWS_AGE / deadline).
_AGE_BUCKETS = (2.0, 5.0, 10.0, 30.0, 60.0, 120.0)


def _percentile(values: list[float], pct: float) -> Optional[float]:
    """Nearest-rank-with-interpolation percentile; None on empty input."""
    if not values:
        return None
    vs = sorted(values)
    if len(vs) == 1:
        return round(vs[0], 1)
    k = (len(vs) - 1) * (pct / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return round(vs[int(k)], 1)
    return round(vs[lo] + (vs[hi] - vs[lo]) * (k - lo), 1)


def _stat_block(values: list[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "p50": _percentile(values, 50),
        "p95": _percentile(values, 95),
        "p99": _percentile(values, 99),
        "min": round(min(values), 1) if values else None,
        "max": round(max(values), 1) if values else None,
    }


@router.get("/pipeline-latency")
def pipeline_latency(
    window: int = Query(100, ge=10, le=500),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Summarise the last `window` analyses that carry timing data."""
    # Over-fetch: placeholder rows (stale_news, ai_analysis_disabled, …)
    # carry no timings and are skipped until `window` real analyses are
    # collected.
    rows = (
        db.execute(
            select(Analysis).order_by(Analysis.id.desc()).limit(window * 3)
        )
        .scalars()
        .all()
    )
    collected: list[tuple[Analysis, dict[str, Any]]] = []
    for a in rows:
        raw = a.raw_response or {}
        timings = raw.get("timings") if isinstance(raw, dict) else None
        if not isinstance(timings, dict) or timings.get("total_ms") is None:
            continue
        collected.append((a, timings))
        if len(collected) >= window:
            break

    def _vals(key: str) -> list[float]:
        return [
            float(t[key])
            for (_a, t) in collected
            if t.get(key) is not None
        ]

    fast = sum(1 for (a, _t) in collected if a.model == "fast-track")
    total = len(collected)

    ages = _vals("news_age_s_at_start")
    histogram: list[dict[str, Any]] = []
    prev = 0.0
    for le in _AGE_BUCKETS:
        histogram.append(
            {
                "le": le,
                "count": sum(1 for v in ages if prev <= v < le),
            }
        )
        prev = le
    histogram.append(
        {"le": None, "count": sum(1 for v in ages if v >= _AGE_BUCKETS[-1])}
    )

    # Deadline hit rate: signals blocked as "late" among the signals born
    # from this window of analyses.
    deadline_blocked = 0
    signals_total = 0
    ids = [a.id for (a, _t) in collected]
    if ids:
        sigs = (
            db.execute(select(Signal).where(Signal.analysis_id.in_(ids)))
            .scalars()
            .all()
        )
        signals_total = len(sigs)
        deadline_blocked = sum(
            1 for s in sigs if "pipeline_deadline" in (s.rationale or "")
        )

    # Oldest→newest series for the sparkline (cap at 60 points).
    series = [
        {
            "analysis_id": a.id,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "total_ms": t.get("total_ms"),
            "llm_ms": t.get("llm_ms"),
            "track": "fast" if a.model == "fast-track" else "llm",
        }
        for (a, t) in reversed(collected[:60])
    ]

    return {
        "window": total,
        "counts": {"total": total, "fast_track": fast, "llm": total - fast},
        "fast_track_pct": round(fast / total * 100.0, 1) if total else None,
        "latency": {
            "pdf_fetch_ms": _stat_block(_vals("pdf_fetch_ms")),
            "pdf_extract_ms": _stat_block(_vals("pdf_extract_ms")),
            "llm_ms": _stat_block(_vals("llm_ms")),
            "total_ms": _stat_block(_vals("total_ms")),
        },
        "news_age_s_at_start": {
            **_stat_block(ages),
            "histogram": histogram,
        },
        "deadline": {
            "blocked": deadline_blocked,
            "total_signals": signals_total,
            "hit_rate_pct": (
                round(deadline_blocked / signals_total * 100.0, 1)
                if signals_total
                else None
            ),
        },
        "series": series,
    }


@router.get("/health-report")
def health_report(db: Session = Depends(get_db)) -> dict[str, Any]:
    """The daily health report, compiled on demand (read-only)."""
    return health_report_svc.compile_health_report(db)


@router.post("/health-report/send")
async def send_health_report(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Compile the report AND fan it out through the notification
    channels (event type "report") — the same path the 15:45 IST
    scheduler uses. Manual trigger for testing / ad-hoc checks."""
    report = health_report_svc.compile_health_report(db)
    subject, body = health_report_svc.format_report(report)
    delivered = await event_bus.publish(
        health_report_svc.CHANNEL_SYSTEM_REPORT,
        {"subject": subject, "body": body, "report": report},
    )
    log.info("health_report.sent_manual", subscribers=delivered)
    return {"ok": True, "subject": subject, "subscribers": delivered, "report": report}
