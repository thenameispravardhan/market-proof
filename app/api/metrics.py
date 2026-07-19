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

from datetime import datetime, timezone

from app.db.models import Analysis, Announcement, Signal, Trade
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


def _ms_between(later: Optional[datetime], earlier: Optional[datetime]) -> Optional[float]:
    """Milliseconds from `earlier` to `later`, or None if either is missing.

    Naive datetimes are treated as UTC (the DB stores UTC). Clamped at 0 —
    a tiny negative from clock skew or an estimated filed_at is noise, not
    a real negative latency.
    """
    if later is None or earlier is None:
        return None
    if later.tzinfo is None:
        later = later.replace(tzinfo=timezone.utc)
    if earlier.tzinfo is None:
        earlier = earlier.replace(tzinfo=timezone.utc)
    return max(0.0, (later - earlier).total_seconds() * 1000.0)


@router.get("/execution-latency")
def execution_latency(
    window: int = Query(100, ge=10, le=500),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Per-layer latency of the last `window` FILLED trades, reconstructed
    end-to-end by walking Trade → Signal → Analysis → Announcement.

    Four layers, each the wall-time a filing spends in one stage:
      detection_ms       exchange filed_at → the bot received it
                         (exchange publish lag + scrape). Not ours to
                         control; shown for context.
      analysis_ms        the bot's own processing (PDF + LLM), = timings.total_ms
      signal_to_order_ms signal created → order placed (risk checks + routing)
      order_to_fill_ms   order placed → broker confirmed the fill

    Paper trades fill synthetically, so the execution stages are near-zero
    on paper data; they become real once live.
    """
    trades = (
        db.execute(
            select(Trade)
            .where(Trade.executed_at.is_not(None))
            .order_by(Trade.id.desc())
            .limit(window)
        )
        .scalars()
        .all()
    )

    # Batch-load the chain to avoid per-trade round-trips.
    signal_ids = {t.signal_id for t in trades if t.signal_id is not None}
    signals = (
        {s.id: s for s in db.execute(select(Signal).where(Signal.id.in_(signal_ids))).scalars()}
        if signal_ids else {}
    )
    analysis_ids = {s.analysis_id for s in signals.values() if s.analysis_id is not None}
    analyses = (
        {a.id: a for a in db.execute(select(Analysis).where(Analysis.id.in_(analysis_ids))).scalars()}
        if analysis_ids else {}
    )
    ann_ids = {a.announcement_id for a in analyses.values() if a.announcement_id is not None}
    anns = (
        {n.id: n for n in db.execute(select(Announcement).where(Announcement.id.in_(ann_ids))).scalars()}
        if ann_ids else {}
    )

    rows: list[dict[str, Any]] = []
    for t in trades:
        sig = signals.get(t.signal_id) if t.signal_id is not None else None
        ana = analyses.get(sig.analysis_id) if sig and sig.analysis_id is not None else None
        ann = anns.get(ana.announcement_id) if ana and ana.announcement_id is not None else None

        analysis_ms: Optional[float] = None
        if ana is not None and isinstance(ana.raw_response, dict):
            timings = ana.raw_response.get("timings")
            if isinstance(timings, dict) and timings.get("total_ms") is not None:
                analysis_ms = float(timings["total_ms"])

        rows.append(
            {
                "trade_id": t.id,
                "symbol": t.symbol,
                "side": t.side,
                "executed_at": t.executed_at.isoformat() if t.executed_at else None,
                "detection_ms": _ms_between(ann.received_at, ann.filed_at) if ann else None,
                "analysis_ms": analysis_ms,
                "signal_to_order_ms": _ms_between(t.created_at, sig.created_at) if sig else None,
                "order_to_fill_ms": _ms_between(t.executed_at, t.created_at),
            }
        )

    def _vals(key: str) -> list[float]:
        return [float(r[key]) for r in rows if r[key] is not None]

    # ---- detection race: per-exchange publish lag ----------------------
    # Computed over recent ANNOUNCEMENTS (not just traded ones) — far more
    # samples, and it answers "which exchange publishes faster?" directly.
    # received_at - filed_at ≈ exchange publish lag + our poll wait.
    recent_anns = (
        db.execute(
            select(Announcement)
            .where(Announcement.filed_at.is_not(None))
            .order_by(Announcement.id.desc())
            .limit(300)
        )
        .scalars()
        .all()
    )
    by_exchange: dict[str, list[float]] = {}
    by_source: dict[str, list[float]] = {}
    for n in recent_anns:
        d = _ms_between(n.received_at, n.filed_at)
        if d is not None:
            by_exchange.setdefault((n.exchange or "?").upper(), []).append(d)
            by_source.setdefault(
                _source_label(n.source, n.exchange), []
            ).append(d)
    detection_by_exchange = {
        exch: _stat_block(vals) for exch, vals in sorted(by_exchange.items())
    }
    detection_by_source = {
        src: _stat_block(vals) for src, vals in sorted(by_source.items())
    }

    # ---- bot-side share: live tick telemetry from the monitors ---------
    # avg fetch+parse+store per tick, plus the actual gap between ticks.
    # The bot's worst-case detection contribution ≈ gap + tick duration;
    # everything beyond that in `detection_by_exchange` is the exchange's
    # own publish lag, which no amount of polling can remove.
    from app.monitors.base import MONITOR_TICK_STATS

    stages = ("detection_ms", "analysis_ms", "signal_to_order_ms", "order_to_fill_ms")
    return {
        "window": len(rows),
        "stages": {s: _stat_block(_vals(s)) for s in stages},
        "detection_by_exchange": detection_by_exchange,
        "detection_by_source": detection_by_source,
        "detection_samples": len(recent_anns),
        "monitor_ticks": dict(MONITOR_TICK_STATS),
        # Newest-first, capped, for the per-trade detail table.
        "series": rows[:40],
    }


def _source_label(source: Optional[str], exchange: Optional[str]) -> str:
    """Announcement.source (the monitor's source_url) -> short channel
    label for the detection race: NSE-API / NSE-RSS / BSE-API / ..."""
    exch = (exchange or "?").upper()
    s = (source or "").lower()
    if "rss" in s or ".xml" in s:
        return f"{exch}-RSS"
    if not s:
        return exch
    return f"{exch}-API"


@router.get("/edge")
def edge_lookup(
    symbol: str = Query(...),
    action: str = Query(...),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Edge Memory: the bot's own track record on signals like this one.
    `null` edge = not enough history yet (the engine fails open)."""
    from app.services import historical_edge as he

    edge = he.compute_edge(db, symbol=symbol, action=action)
    return {
        "symbol": symbol.upper(),
        "action": action.upper(),
        "edge": edge.to_dict() if edge is not None else None,
    }


@router.get("/edge/book")
def edge_book(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Overall track record by action — the whole book's expectancy."""
    from app.services import historical_edge as he

    return {"book": he.book_expectancy(db)}


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
