"""/api/research — the measurement layer the strategy is judged on.

  GET /api/research/replay       Cost-aware event replay over recorded
                                 signals on real 1-minute candles: expectancy
                                 in R with bootstrap CIs, split by block
                                 reason, event type and confidence bucket.
  GET /api/research/funnel       Where signals go: taken vs blocked, and the
                                 block rate decomposed by reason code.
  GET /api/research/calibration  Reliability of the model's confidence
                                 against what the stock actually did, per
                                 provider (model) and event type, with ECE.

All read-only. The replay reads candle files, so it runs in a worker
thread and is capped by `limit`; the full-history run is
scripts/event_replay.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Analysis, Announcement, ShadowAnalysis, Signal, SignalOutcome
from app.db.session import get_db
from app.research import replay as rp
from app.research import stats

router = APIRouter(prefix="/api/research", tags=["research"])

# A filing "moved" when |30-minute return| reaches this — the same label the
# README's confidence-inversion table and the Model preview use.
MOVER_THRESHOLD_PCT = 1.5


def _since(days: int) -> datetime:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)


@router.get("/replay")
async def research_replay(
    days: int = Query(60, ge=1, le=730),
    limit: int = Query(3000, ge=1, le=20000),
    entry_delay_s: Optional[float] = Query(None, ge=0, le=600),
    slippage_bps: Optional[float] = Query(None, ge=0, le=500),
    notional: Optional[float] = Query(None, gt=0, le=1e8),
    include_hypothetical: bool = Query(True, description="replay HOLD signals on the analysis' side"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    events = rp.load_events(db, since=_since(days), limit=limit, include_hypothetical=include_hypothetical)
    params = rp.ReplayParams.from_settings(
        get_settings(), entry_delay_s=entry_delay_s, slippage_bps=slippage_bps, notional=notional)

    def _run() -> dict[str, Any]:
        result = rp.run_replay(events, rp.ParquetCandleSource(), params)
        body = rp.report(result, capital=float(getattr(get_settings(), "PORTFOLIO_VALUE", 1e6)))
        body["events_loaded"] = len(events)
        return body

    return await run_in_threadpool(_run)


@router.get("/funnel")
def research_funnel(days: int = Query(30, ge=1, le=730), db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.execute(
        select(Signal.status, Signal.action, Signal.rationale).where(Signal.created_at >= _since(days))
    ).all()
    total = len(rows)
    by_status: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    by_action: dict[str, int] = {}
    for status, action, rationale in rows:
        st = str(status or "unknown")
        by_status[st] = by_status.get(st, 0) + 1
        by_action[str(action or "?")] = by_action.get(str(action or "?"), 0) + 1
        reason = rp.block_reason(st, rationale)
        if reason:
            by_reason[reason] = by_reason.get(reason, 0) + 1
    blocked = by_status.get("blocked", 0)
    return {
        "days": days,
        "signals": total,
        "blocked": blocked,
        "block_rate": round(blocked / total, 4) if total else None,
        "by_status": by_status,
        "by_action": by_action,
        "by_block_reason": [
            {"reason": k, "n": v, "share_of_blocked": round(v / blocked, 4) if blocked else None}
            for k, v in sorted(by_reason.items(), key=lambda kv: -kv[1])
        ],
    }


@router.get("/calibration")
def research_calibration(
    days: int = Query(90, ge=1, le=730),
    n_bins: int = Query(10, ge=2, le=20),
    mover_pct: float = Query(MOVER_THRESHOLD_PCT, gt=0, le=20),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Does the stated confidence mean anything? Outcome = the filing moved
    |30m| >= `mover_pct`. Reported overall, per model and per event type,
    with AUC (does confidence RANK movers above non-movers at all?) and ECE
    (are the numbers themselves honest?)."""
    rows = db.execute(
        select(SignalOutcome.move_30m_pct, Signal.confidence, Analysis.confidence,
               Analysis.model, Announcement.event_type, SignalOutcome.created_at)
        .join(Signal, SignalOutcome.signal_id == Signal.id)
        .outerjoin(Analysis, Signal.analysis_id == Analysis.id)
        .outerjoin(Announcement, Analysis.announcement_id == Announcement.id)
        .where(SignalOutcome.move_30m_pct.isnot(None), SignalOutcome.created_at >= _since(days))
    ).all()
    samples = []
    for move, sconf, aconf, model, etype, created in rows:
        conf = aconf if aconf is not None else sconf
        if conf is None:
            continue
        samples.append((float(conf), int(abs(float(move)) >= mover_pct), model or "unknown",
                        etype or "UNKNOWN", created.strftime("%Y-%m") if created else "unknown"))

    def block(sel: list[tuple]) -> dict[str, Any]:
        probs = [s[0] for s in sel]
        ys = [s[1] for s in sel]
        auc = stats.roc_auc(probs, ys)
        return {
            **stats.ece(probs, ys, n_bins=n_bins),
            "base_rate": round(sum(ys) / len(ys), 4) if ys else None,
            "auc": None if auc is None else round(auc, 4),
        }

    by_model: dict[str, list] = {}
    by_event: dict[str, list] = {}
    by_month: dict[str, list] = {}
    for s in samples:
        by_model.setdefault(s[2], []).append(s)
        by_event.setdefault(s[3], []).append(s)
        by_month.setdefault(s[4], []).append(s)
    return {
        "days": days,
        "mover_pct": mover_pct,
        "overall": block(samples),
        # Calibration drifts with the regime (the mover base rate moves
        # between periods), so it is monitored month by month.
        "by_month": [{"month": k, "n": len(v), "ece": block(v)["ece"], "auc": block(v)["auc"],
                      "base_rate": block(v)["base_rate"]} for k, v in sorted(by_month.items())],
        "by_model": {k: block(v) for k, v in by_model.items()},
        "by_event_type": {k: block(v) for k, v in sorted(by_event.items(), key=lambda kv: -len(kv[1])) if len(v) >= 20},
    }


@router.get("/shadow")
def research_shadow(
    days: int = Query(90, ge=1, le=730),
    mover_pct: float = Query(MOVER_THRESHOLD_PCT, gt=0, le=20),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Live model vs shadow SLM on the SAME filings, scored against the
    measured 30-minute move. The bar for switching LLM_PROVIDER is a paired
    AUC difference whose CI clears zero, not a better-looking average."""
    since = _since(days)
    status_rows = db.execute(
        select(ShadowAnalysis.status, ShadowAnalysis.latency_ms).where(ShadowAnalysis.created_at >= since)
    ).all()
    by_status: dict[str, int] = {}
    lat = []
    for st, ms in status_rows:
        by_status[st] = by_status.get(st, 0) + 1
        if st == "ok" and ms is not None:
            lat.append(float(ms))
    lat.sort()

    rows = db.execute(
        select(ShadowAnalysis, Analysis.confidence, Analysis.recommendation, Analysis.model,
               SignalOutcome.move_30m_pct)
        .join(Analysis, ShadowAnalysis.analysis_id == Analysis.id)
        .join(SignalOutcome, SignalOutcome.signal_id == ShadowAnalysis.signal_id)
        .where(ShadowAnalysis.status == "ok", ShadowAnalysis.created_at >= since,
               SignalOutcome.move_30m_pct.isnot(None))
    ).all()
    live, shadow, ys, agree = [], [], [], 0
    live_model = None
    for sh, conf, rec, model, move in rows:
        if conf is None or sh.confidence is None:
            continue
        live_model = live_model or model
        live.append(float(conf))
        shadow.append(float(sh.confidence))
        ys.append(int(abs(float(move)) >= mover_pct))
        agree += int(str(rec or "").upper() == str(sh.recommendation or "").upper())

    def pct(p: float) -> Optional[float]:
        return round(lat[int(p * (len(lat) - 1))], 1) if lat else None

    return {
        "days": days,
        "mover_pct": mover_pct,
        "shadow_calls": by_status,
        "shadow_latency_ms": {"p50": pct(0.5), "p90": pct(0.9)},
        "paired": len(ys),
        "live_model": live_model,
        "recommendation_agreement": round(agree / len(ys), 4) if ys else None,
        "auc_live_vs_shadow": stats.paired_auc_difference(live, shadow, ys) if ys else None,
        "ece_live": stats.ece(live, ys)["ece"] if ys else None,
        "ece_shadow": stats.ece(shadow, ys)["ece"] if ys else None,
    }


@router.get("/meta-label")
async def research_meta_label(
    days: int = Query(180, ge=7, le=730),
    limit: int = Query(20000, ge=100, le=50000),
    entry_delay_s: Optional[float] = Query(None, ge=0, le=600),
    slippage_bps: Optional[float] = Query(None, ge=0, le=500),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Fit and evaluate the meta-labeling model on replayed trades (label:
    the trade ended positive after costs under the bot's own exits). A
    research report — nothing here gates or sizes a live order."""
    from app.research import meta_label

    events = rp.load_events(db, since=_since(days), limit=limit)
    params = rp.ReplayParams.from_settings(get_settings(), entry_delay_s=entry_delay_s,
                                           slippage_bps=slippage_bps)

    def _run() -> dict[str, Any]:
        result = rp.run_replay(events, rp.ParquetCandleSource(), params)
        body = meta_label.run(result.trades)
        body["replayed_trades"] = len(result.trades)
        return body

    return await run_in_threadpool(_run)


@router.get("/results-xbrl")
async def research_results_xbrl(url: str = Query(..., description="exchange-hosted XBRL instance URL")
                                ) -> dict[str, Any]:
    """Numeric surprise (revenue / EBITDA / PAT YoY and QoQ, margin change)
    parsed from a results filing's XBRL."""
    from app.research.results_xbrl import fetch_and_parse

    try:
        return await fetch_and_parse(url)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"fetch failed: {str(e)[:200]}"}


# ---------------------------------------------------------------------------
# Pre-registered evaluation windows
# ---------------------------------------------------------------------------


@router.get("/windows")
def list_windows(db: Session = Depends(get_db)) -> dict[str, Any]:
    from app.db.models import EvaluationWindow
    from app.research import windows

    rows = db.execute(select(EvaluationWindow).order_by(EvaluationWindow.id.desc()).limit(20)).scalars().all()
    return {"windows": [windows.report(db, w) for w in rows]}


@router.post("/windows")
def start_window(payload: dict[str, Any] = Body(...), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Declare a window: {name, hypothesis, weeks}. The decision-relevant
    configuration is hashed now; changing it before the end invalidates the
    window as evidence (and the report says which key moved)."""
    from app.research import windows

    try:
        w = windows.start(db, name=str(payload.get("name") or "paper window"),
                          hypothesis=str(payload.get("hypothesis") or ""),
                          weeks=int(payload.get("weeks") or 6))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return windows.report(db, w)


@router.post("/windows/{window_id}/close")
def close_window(window_id: int, abandon: bool = Query(False), db: Session = Depends(get_db)) -> dict[str, Any]:
    from app.research import windows

    try:
        w = windows.close(db, window_id, abandon=abandon)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return windows.report(db, w)


# ---------------------------------------------------------------------------
# Per-signal "why" card
# ---------------------------------------------------------------------------


@router.get("/why/{signal_id}")
def why(signal_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Everything behind one decision on one card: the filing, what the
    model said and why, the numbers it read, the rule that fired, every
    risk check that blocked it, and what the price did afterwards."""
    from app.db.models import RiskEvent, Trade

    sig = db.get(Signal, signal_id)
    if sig is None:
        raise HTTPException(status_code=404, detail="signal not found")
    an = db.get(Analysis, sig.analysis_id) if sig.analysis_id else None
    ann = db.get(Announcement, an.announcement_id) if an is not None else None
    raw = (an.raw_response or {}) if an is not None else {}
    parsed = raw.get("parsed") if isinstance(raw.get("parsed"), dict) else raw
    risk = [
        {"code": e.event_type, "message": e.message, "at": e.created_at.isoformat() if e.created_at else None}
        # risk_events has no signal column (the id lives in JSON context), so
        # scan only the window in which this signal could have been gated.
        for e in db.execute(select(RiskEvent).where(
            RiskEvent.created_at >= sig.created_at - timedelta(minutes=1),
            RiskEvent.created_at <= sig.created_at + timedelta(minutes=15),
        ).order_by(RiskEvent.id)).scalars()
        if isinstance(e.context, dict) and e.context.get("signal_id") == signal_id
    ]
    trades = [
        {"side": t.side, "qty": t.quantity, "price": t.price, "status": t.status, "pnl": t.pnl,
         "r_multiple": t.r_multiple, "slippage_pct": t.slippage_pct, "order_type": t.order_type}
        for t in db.execute(select(Trade).where(Trade.signal_id == signal_id).order_by(Trade.id)).scalars()
    ]
    out = db.execute(select(SignalOutcome).where(SignalOutcome.signal_id == signal_id)).scalars().first()
    rationale = sig.rationale or ""
    return {
        "signal": {"id": sig.id, "symbol": sig.symbol, "action": sig.action, "status": sig.status,
                   "confidence": sig.confidence, "created_at": sig.created_at.isoformat() if sig.created_at else None,
                   "rule_id": sig.rule_id, "rule_rationale": rationale.split(" | ")[0].strip()},
        "block_reason": rp.block_reason(sig.status, rationale),
        "filing": None if ann is None else {
            "headline": ann.headline, "event_type": ann.event_type, "exchange": ann.exchange,
            "pdf_url": ann.pdf_url, "filed_at": ann.filed_at.isoformat() if ann.filed_at else None,
            "received_at": ann.received_at.isoformat() if ann.received_at else None},
        "analysis": None if an is None else {
            "model": an.model, "sentiment": an.sentiment, "sentiment_score": an.sentiment_score,
            "confidence": an.confidence, "recommendation": an.recommendation,
            "summary": parsed.get("summary") if isinstance(parsed, dict) else None,
            "reasoning": (parsed.get("reasoning") if isinstance(parsed, dict) else None) or an.rationale,
            "key_numbers": parsed.get("key_numbers") if isinstance(parsed, dict) else None,
            "timings": raw.get("timings"),
            "saw_filing_text": bool((raw.get("pdf_extraction") or {}).get("ok")) if isinstance(raw.get("pdf_extraction"), dict) else False},
        "risk_checks_blocked": risk,
        "orders": trades,
        "outcome": None if out is None else {
            "price_at_signal": out.price_at_signal, "move_5m_pct": out.move_5m_pct,
            "move_30m_pct": out.move_30m_pct, "note": out.note},
    }
