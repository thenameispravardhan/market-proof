"""The Dataset API — the customizable ML training-set surface.

Joins EVERYTHING the pipeline knows about each signal into one wide,
column-selectable table:

  announcements (the news) + analyses (the AI verdict) + signals (the
  decision) + planned levels (entry/SL/target parsed from the
  rationale) + trades (what execution actually did) + signal_outcomes
  (coarse +5m/+30m samples) + dataset_features (the 1-minute-candle
  reaction features and horizon targets from the DatasetBuilder).

Endpoints:
  GET  /api/dataset/columns    Column catalog: every available column,
                               grouped by category, tagged with its ML
                               role — meta | feature (safe model input)
                               | target (label only; using a target as
                               an input is look-ahead bias).
  GET  /api/dataset            Filtered, column-selected rows (preview).
  GET  /api/dataset/stats      Coverage: enrichment status, label
                               balance, per-event-type counts.
  GET  /api/dataset/export     The full set as CSV or JSONL.
  POST /api/dataset/backfill   Run an enrichment batch NOW (the
                               background builder also runs one every
                               DATASET_ENRICH_INTERVAL_SECONDS).
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Analysis,
    Announcement,
    DatasetFeature,
    Signal,
    SignalOutcome,
    Trade,
)
from app.db.session import get_db
from app.logging_config import get_logger

router = APIRouter(prefix="/api/dataset", tags=["dataset"])
log = get_logger(__name__)

# Signal statuses that mean "the trade went to the broker".
_TAKEN_STATUSES = ("filled", "closed")

# =========================================================================
# Column catalog
# =========================================================================
# role: "meta"    — identity / data-quality, not a model input
#       "feature" — knowable at (or within 5 minutes of) decision time;
#                   safe input for a 15-minute-horizon model
#       "target"  — only knowable at/after the horizon; training LABEL.
# source: which joined object the value comes from ("features" = the
# DatasetFeature JSON blob).

_C = lambda key, label, category, role, description, source: {  # noqa: E731
    "key": key, "label": label, "category": category,
    "role": role, "description": description, "source": source,
}

COLUMN_SPECS: list[dict[str, str]] = [
    # ---- identity ------------------------------------------------------
    _C("outcome_id", "Outcome ID", "identity", "meta", "signal_outcomes row id (null on shadow rows)", "outcome"),
    _C("signal_id", "Signal ID", "identity", "meta", "signals row id", "outcome"),
    _C("announcement_id", "Announcement ID", "identity", "meta", "announcements row id", "announcement"),
    _C("row_source", "Row source", "identity", "meta", "signal (bot acted / was blocked) or shadow (analyzed, never signaled — the negatives)", "derived"),
    _C("cross_listing_key", "Cross-listing key", "identity", "meta", "symbol + filed-minute: NSE+BSE copies of the same news share a key — dedup on it or twins leak across train/val", "derived"),
    _C("symbol", "Symbol", "identity", "meta", "NSE/BSE ticker", "outcome"),
    _C("signal_time", "Signal time (UTC)", "identity", "meta", "when the signal fired (shadow: when analyzed)", "outcome"),
    # ---- news ----------------------------------------------------------
    _C("exchange", "Exchange", "news", "feature", "NSE or BSE", "announcement"),
    _C("event_type", "Event type", "news", "feature", "ORDER_WIN, Q1_RESULTS, …", "announcement"),
    _C("headline", "Headline", "news", "feature", "raw filing headline (text feature)", "announcement"),
    _C("filed_at", "Filed at (UTC)", "news", "meta", "exchange filing timestamp", "announcement"),
    _C("news_age_s", "News age (s)", "news", "feature", "filing→signal latency; stale news = weaker edge", "derived"),
    # ---- session / market context ---------------------------------------
    _C("minute_since_open", "Minute since open", "context", "feature", "IST minutes after 09:15 — a 9:31 spike ≠ a 14:50 spike", "features"),
    _C("day_of_week", "Day of week", "context", "feature", "0=Mon .. 4=Fri (IST)", "features"),
    _C("nifty_ret_1m_pct", "NIFTY ret +1m %", "context", "feature", "concurrent index move — did the whole tape move?", "features"),
    _C("nifty_ret_5m_pct", "NIFTY ret +5m %", "context", "feature", "concurrent index move over the 5m window", "features"),
    _C("excess_ret_5m_pct", "Excess ret 5m %", "context", "feature", "stock 5m return minus NIFTY — the news-specific move", "features"),
    # ---- analysis ------------------------------------------------------
    _C("model", "Model", "analysis", "meta", "deepseek-chat or fast-track", "analysis"),
    _C("sentiment", "Sentiment", "analysis", "feature", "positive | neutral | negative", "analysis"),
    _C("sentiment_score", "Sentiment score", "analysis", "feature", "-100..100", "analysis"),
    _C("confidence", "Confidence", "analysis", "feature", "0..1", "analysis"),
    _C("recommendation", "Recommendation", "analysis", "feature", "AI BUY/SELL/HOLD", "analysis"),
    _C("deal_value_inr_crore", "Deal value (₹cr)", "analysis", "feature", "order/deal size", "analysis"),
    _C("stake_change_pct", "Stake change %", "analysis", "feature", "M&A stake delta", "analysis"),
    _C("dividend_per_share", "Dividend/share", "analysis", "feature", "announced DPS", "analysis"),
    _C("buyback_value_inr_crore", "Buyback (₹cr)", "analysis", "feature", "buyback size", "analysis"),
    _C("analysis_latency_ms", "Analysis latency (ms)", "analysis", "feature", "LLM pipeline time (timings.total_ms)", "analysis"),
    # ---- signal --------------------------------------------------------
    _C("action", "Action", "signal", "feature", "BUY | SELL | HOLD | BLOCK", "outcome"),
    _C("signal_status", "Signal status", "signal", "meta", "pending/blocked/filled/closed/expired", "signal"),
    _C("position_size_pct", "Size %", "signal", "feature", "rule-suggested position size", "signal"),
    _C("strategy_id", "Strategy", "signal", "meta", "owning strategy id", "signal"),
    _C("rule_id", "Rule", "signal", "meta", "matching rule id", "signal"),
    _C("trade_taken", "Trade taken", "signal", "meta", "signal reached the broker", "derived"),
    # ---- planned levels (parsed from the signal rationale) -------------
    _C("planned_entry", "Planned entry", "levels", "feature", "entry the analyzer computed", "levels"),
    _C("planned_stop", "Planned stop", "levels", "feature", "initial stop-loss", "levels"),
    _C("planned_target", "Planned target", "levels", "feature", "profit target", "levels"),
    _C("planned_rr", "Planned R:R", "levels", "feature", "reward:risk at plan time", "levels"),
    # ---- execution (what really happened — post-hoc) --------------------
    _C("entry_fill_price", "Entry fill", "execution", "meta", "actual broker fill price", "trades"),
    _C("slippage_pct", "Slippage %", "execution", "meta", "fill vs intended entry", "trades"),
    _C("realized_pnl", "Realized P&L", "execution", "target", "closed-trade rupee P&L", "trades"),
    _C("r_multiple", "R multiple", "execution", "target", "P&L in initial-risk units", "trades"),
    # ---- outcome samples (the coarse OutcomeLogger probes) --------------
    _C("price_at_signal", "Price at signal", "outcome_samples", "feature", "Fyers quote at T0", "outcome"),
    _C("move_5m_pct", "Move +5m %", "outcome_samples", "feature", "quote-probe move at T+5m", "outcome"),
    _C("move_30m_pct", "Move +30m %", "outcome_samples", "target", "quote-probe move at T+30m (beyond horizon)", "outcome"),
    _C("ai_was_correct", "AI correct (5m)", "outcome_samples", "target", "5m move agreed with the action", "derived"),
    # ---- market reaction (1-min candles, T-10..T+5 — safe features) -----
    _C("pre_move_5m_pct", "Pre-news drift %", "reaction", "feature", "move in the 5 min BEFORE the signal", "features"),
    _C("pre_volume_avg_5m", "Pre-news avg vol", "reaction", "feature", "baseline 1-min volume", "features"),
    _C("price_1m", "Price +1m", "reaction", "feature", "close 1 min after signal", "features"),
    _C("price_2m", "Price +2m", "reaction", "feature", "close 2 min after signal", "features"),
    _C("price_3m", "Price +3m", "reaction", "feature", "close 3 min after signal", "features"),
    _C("price_4m", "Price +4m", "reaction", "feature", "close 4 min after signal", "features"),
    _C("price_5m", "Price +5m", "reaction", "feature", "close 5 min after signal", "features"),
    _C("ret_1m_pct", "Ret +1m %", "reaction", "feature", "return at T+1 (the acceleration)", "features"),
    _C("ret_2m_pct", "Ret +2m %", "reaction", "feature", "return at T+2", "features"),
    _C("ret_3m_pct", "Ret +3m %", "reaction", "feature", "return at T+3", "features"),
    _C("ret_4m_pct", "Ret +4m %", "reaction", "feature", "return at T+4", "features"),
    _C("ret_5m_pct", "Ret +5m %", "reaction", "feature", "return at T+5", "features"),
    _C("high_5m", "High 0-5m", "reaction", "feature", "max high in first 5 min", "features"),
    _C("low_5m", "Low 0-5m", "reaction", "feature", "min low in first 5 min", "features"),
    _C("mfe_5m_pct", "MFE 5m %", "reaction", "feature", "max favorable excursion by T+5", "features"),
    _C("mae_5m_pct", "MAE 5m %", "reaction", "feature", "max adverse excursion by T+5", "features"),
    _C("time_to_1pct_5m_s", "Time to 1% (≤5m)", "reaction", "feature", "seconds to first 1% favorable spike", "features"),
    _C("hit_1pct_within_5m", "Hit 1% ≤5m", "reaction", "feature", "violent reaction flag", "features"),
    _C("slope_1m_3m_pct_per_min", "Slope 1→3m", "reaction", "feature", "accelerating or fading?", "features"),
    _C("volume_1m", "Volume 1m", "reaction", "feature", "signal-minute volume", "features"),
    _C("volume_surge_1m", "Volume surge ×", "reaction", "feature", "signal-minute vol / pre-news avg", "features"),
    _C("vwap_dev_5m_pct", "VWAP dev 5m %", "reaction", "feature", "price vs 5-min VWAP", "features"),
    _C("range_breakout_2m", "Range breakout ≤2m", "reaction", "feature", "broke the signal-minute extreme", "features"),
    _C("vol_expansion_ratio", "Vol expansion ×", "reaction", "feature", "post vs pre 1-min return stdev", "features"),
    # ---- horizon targets (T+15 — training labels ONLY) -------------------
    _C("price_15m", "Price +15m", "targets", "target", "close at the horizon", "features"),
    _C("ret_15m_pct", "Ret +15m %", "targets", "target", "the Model-A regression target", "features"),
    _C("high_15m", "High 0-15m", "targets", "target", "the peak — Model-B target", "features"),
    _C("low_15m", "Low 0-15m", "targets", "target", "the trough", "features"),
    _C("mfe_15m_pct", "MFE 15m %", "targets", "target", "max favorable excursion over horizon", "features"),
    _C("mae_15m_pct", "MAE 15m %", "targets", "target", "max adverse excursion (stop-sizing label)", "features"),
    _C("time_to_1pct_15m_s", "Time to 1% (≤15m)", "targets", "target", "seconds to 1% within horizon", "features"),
    _C("time_to_peak_min", "Time to peak (min)", "targets", "target", "minute of the favorable extreme", "features"),
    _C("retrace_from_peak_pct", "Retrace from peak %", "targets", "target", "peak given back by horizon close", "features"),
    _C("label_15m", "Label 15m", "targets", "target", "UP / DOWN / FLAT (classification target)", "features"),
    _C("day_high", "Day high after signal", "targets", "target", "highest price from signal to close (same IST day)", "features"),
    _C("ret_day_high_pct", "Ret to day high %", "targets", "target", "the full extent of the day's move — beyond the horizon", "features"),
    # ---- data quality ----------------------------------------------------
    _C("enrich_status", "Enrichment", "quality", "meta", "complete/partial/no_candles/after_hours/too_old/pending", "df"),
    _C("outcome_note", "Outcome note", "quality", "meta", "quote-probe data quality", "outcome"),
    _C("candles_post", "Candles found", "quality", "meta", "post-signal 1-min candles aligned", "features"),
    _C("features_version", "Feature schema ver.", "quality", "meta", "compute schema version of this row", "features"),
]

# ---- mid-trajectory minutes 6..14 (the full shape of the move) ----------
# price_{k}m / ret_{k}m for every minute between the T+5 feature window and
# the T+15 horizon. These are look-ahead TARGETS (not knowable at a T0/T+5
# decision) — they let a model learn the trajectory CURVE, not just the
# endpoints. Generated from the default horizon so there's one source of
# truth, and appended here so the hand-authored feature/target columns
# above stay readable. (price_15m/ret_15m live in the targets block.)
_TRAJECTORY_HORIZON = 15
for _m in range(6, _TRAJECTORY_HORIZON):
    COLUMN_SPECS.append(
        _C(f"price_{_m}m", f"Price +{_m}m", "trajectory", "target",
           f"close {_m} min after signal (as-of for sparse names)", "features")
    )
    COLUMN_SPECS.append(
        _C(f"ret_{_m}m_pct", f"Ret +{_m}m %", "trajectory", "target",
           f"return at T+{_m} (trajectory shape)", "features")
    )

_VALID_KEYS = {c["key"] for c in COLUMN_SPECS}
_FEATURE_JSON_KEYS = {c["key"] for c in COLUMN_SPECS if c["source"] == "features"}


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _ai_was_correct(action: str, move_5m_pct: Optional[float]) -> Optional[bool]:
    if move_5m_pct is None:
        return None
    if action == "BUY":
        return move_5m_pct > 0
    if action == "SELL":
        return move_5m_pct < 0
    return None


# =========================================================================
# Row assembly
# =========================================================================


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _signal_stmt(
    *,
    event_type: Optional[str],
    action: Optional[str],
    symbol: Optional[str],
    taken: Optional[str],
    label: Optional[str],
    enriched_only: bool,
    since: Optional[str],
    until: Optional[str],
):
    """Signal-source rows: one per signal_outcomes row."""
    stmt = (
        select(SignalOutcome, Signal, Analysis, Announcement, DatasetFeature)
        .join(Signal, SignalOutcome.signal_id == Signal.id, isouter=True)
        .join(Analysis, Signal.analysis_id == Analysis.id, isouter=True)
        .join(Announcement, Analysis.announcement_id == Announcement.id, isouter=True)
        .join(DatasetFeature, DatasetFeature.outcome_id == SignalOutcome.id, isouter=True)
    )
    if event_type:
        stmt = stmt.where(Announcement.event_type == event_type.upper())
    if action:
        stmt = stmt.where(SignalOutcome.action == action.upper())
    if symbol:
        stmt = stmt.where(SignalOutcome.symbol == symbol.upper())
    if taken == "taken":
        stmt = stmt.where(Signal.status.in_(_TAKEN_STATUSES))
    elif taken == "blocked":
        stmt = stmt.where(
            (Signal.status.is_(None)) | (~Signal.status.in_(_TAKEN_STATUSES))
        )
    if label:
        stmt = stmt.where(
            DatasetFeature.features["label_15m"].as_string() == label.upper()
        )
    if enriched_only:
        stmt = stmt.where(DatasetFeature.status == "complete")
    if (dt := _parse_dt(since)) is not None:
        stmt = stmt.where(SignalOutcome.created_at >= dt)
    if (dt := _parse_dt(until)) is not None:
        stmt = stmt.where(SignalOutcome.created_at <= dt)
    return stmt


def _shadow_stmt(
    *,
    event_type: Optional[str],
    action: Optional[str],
    symbol: Optional[str],
    taken: Optional[str],
    label: Optional[str],
    enriched_only: bool,
    since: Optional[str],
    until: Optional[str],
):
    """Shadow rows: analyzed announcements that never produced a signal
    outcome — the rejected universe (negatives). Returns None when the
    `taken` filter excludes shadow rows entirely (they were never taken)."""
    if taken == "taken":
        return None
    outcome_exists = (
        select(SignalOutcome.id)
        .join(Signal, SignalOutcome.signal_id == Signal.id)
        .where(Signal.analysis_id == Analysis.id)
    ).exists()
    stmt = (
        select(Announcement, Analysis, Signal, DatasetFeature)
        .join(Analysis, Analysis.announcement_id == Announcement.id)
        .join(Signal, Signal.analysis_id == Analysis.id, isouter=True)
        .join(
            DatasetFeature,
            DatasetFeature.announcement_id == Announcement.id,
            isouter=True,
        )
        .where(~outcome_exists)
    )
    if event_type:
        stmt = stmt.where(Announcement.event_type == event_type.upper())
    if action:
        stmt = stmt.where(
            func.upper(func.coalesce(Signal.action, Analysis.recommendation))
            == action.upper()
        )
    if symbol:
        stmt = stmt.where(Announcement.symbol == symbol.upper())
    if label:
        stmt = stmt.where(
            DatasetFeature.features["label_15m"].as_string() == label.upper()
        )
    if enriched_only:
        stmt = stmt.where(DatasetFeature.status == "complete")
    if (dt := _parse_dt(since)) is not None:
        stmt = stmt.where(Analysis.created_at >= dt)
    if (dt := _parse_dt(until)) is not None:
        stmt = stmt.where(Analysis.created_at <= dt)
    return stmt


def _trade_aggregates(db: Session, signal_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Per-signal execution facts reduced from the trades table: first
    non-blocked fill price, first slippage, summed realized pnl, last
    r_multiple."""
    if not signal_ids:
        return {}
    rows = (
        db.execute(
            select(Trade)
            .where(Trade.signal_id.in_(signal_ids))
            .order_by(Trade.id.asc())
        )
        .scalars()
        .all()
    )
    agg: dict[int, dict[str, Any]] = {}
    for t in rows:
        a = agg.setdefault(
            t.signal_id,
            {"entry_fill_price": None, "slippage_pct": None,
             "realized_pnl": None, "r_multiple": None},
        )
        if t.status != "blocked" and a["entry_fill_price"] is None:
            a["entry_fill_price"] = t.price
        if a["slippage_pct"] is None and t.slippage_pct is not None:
            a["slippage_pct"] = t.slippage_pct
        if t.pnl is not None:
            a["realized_pnl"] = (a["realized_pnl"] or 0.0) + t.pnl
        if t.r_multiple is not None:
            a["r_multiple"] = t.r_multiple
    return agg


def _cross_listing_key(
    ann: Optional[Announcement],
    outcome: Optional[SignalOutcome],
) -> Optional[str]:
    """NSE + BSE copies of the same news share a key (symbol + filed
    minute) so they can be collapsed at export time."""
    if ann is not None and ann.filed_at is not None:
        return f"{(ann.symbol or '').upper()}|{ann.filed_at.strftime('%Y%m%d%H%M')}"
    if ann is not None:
        return f"ann:{ann.id}"
    if outcome is not None:
        return f"out:{outcome.id}"
    return None


def _assemble_row(
    outcome: Optional[SignalOutcome],
    signal: Optional[Signal],
    analysis: Optional[Analysis],
    ann: Optional[Announcement],
    df: Optional[DatasetFeature],
    trades: dict[str, Any],
) -> dict[str, Any]:
    feats: dict[str, Any] = (df.features or {}) if df is not None else {}
    # Shadow rows have no outcome: the action is what the AI recommended.
    act = str(
        (outcome.action if outcome is not None else None)
        or getattr(signal, "action", None)
        or getattr(analysis, "recommendation", None)
        or ""
    ).upper()
    status = getattr(signal, "status", None)
    anchor = (
        outcome.created_at
        if outcome is not None
        else getattr(analysis, "created_at", None)
    )

    news_age_s: Optional[float] = None
    decided_at = getattr(signal, "created_at", None) or anchor
    if ann is not None and ann.filed_at and decided_at:
        delta = (decided_at - ann.filed_at).total_seconds()
        news_age_s = round(delta, 1) if delta >= 0 else None

    latency_ms: Optional[float] = None
    if analysis is not None and isinstance(analysis.raw_response, dict):
        timings = analysis.raw_response.get("timings")
        if isinstance(timings, dict) and timings.get("total_ms") is not None:
            try:
                latency_ms = float(timings["total_ms"])
            except (TypeError, ValueError):
                latency_ms = None

    levels: dict[str, float] = {}
    if signal is not None and signal.rationale:
        from app.execution.manager import parse_rationale_levels

        levels = parse_rationale_levels(signal.rationale)

    row: dict[str, Any] = {
        "outcome_id": outcome.id if outcome is not None else None,
        "signal_id": (
            outcome.signal_id if outcome is not None else getattr(signal, "id", None)
        ),
        "announcement_id": getattr(ann, "id", None),
        "row_source": "signal" if outcome is not None else "shadow",
        "cross_listing_key": _cross_listing_key(ann, outcome),
        "symbol": (
            outcome.symbol if outcome is not None else getattr(ann, "symbol", None)
        ),
        "signal_time": _iso(anchor),
        "exchange": getattr(ann, "exchange", None),
        "event_type": getattr(ann, "event_type", None),
        "headline": getattr(ann, "headline", None),
        "filed_at": _iso(getattr(ann, "filed_at", None)),
        "news_age_s": news_age_s,
        "model": getattr(analysis, "model", None),
        "sentiment": getattr(analysis, "sentiment", None),
        "sentiment_score": getattr(analysis, "sentiment_score", None),
        "confidence": getattr(analysis, "confidence", None),
        "recommendation": getattr(analysis, "recommendation", None),
        "deal_value_inr_crore": getattr(analysis, "deal_value_inr_crore", None),
        "stake_change_pct": getattr(analysis, "stake_change_pct", None),
        "dividend_per_share": getattr(analysis, "dividend_per_share", None),
        "buyback_value_inr_crore": getattr(analysis, "buyback_value_inr_crore", None),
        "analysis_latency_ms": latency_ms,
        "action": act,
        "signal_status": status,
        "position_size_pct": getattr(signal, "position_size_pct", None),
        "strategy_id": getattr(signal, "strategy_id", None),
        "rule_id": getattr(signal, "rule_id", None),
        "trade_taken": (status in _TAKEN_STATUSES) if status else False,
        "planned_entry": levels.get("entry"),
        "planned_stop": levels.get("stop_loss"),
        "planned_target": levels.get("target"),
        "planned_rr": levels.get("rr"),
        "entry_fill_price": trades.get("entry_fill_price"),
        "slippage_pct": trades.get("slippage_pct"),
        "realized_pnl": trades.get("realized_pnl"),
        "r_multiple": trades.get("r_multiple"),
        "price_at_signal": (
            outcome.price_at_signal
            if outcome is not None
            else feats.get("baseline_price")
        ),
        "move_5m_pct": outcome.move_5m_pct if outcome is not None else None,
        "move_30m_pct": outcome.move_30m_pct if outcome is not None else None,
        "ai_was_correct": _ai_was_correct(
            act, outcome.move_5m_pct if outcome is not None else None
        ),
        "enrich_status": getattr(df, "status", None) or "pending",
        "outcome_note": outcome.note if outcome is not None else None,
    }
    for key in _FEATURE_JSON_KEYS:
        row[key] = feats.get(key)
    return row


# Hard bound on rows materialised in Python for dedup/split/health passes.
_FULL_FETCH_CAP = 100_000


def _dedup_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per cross_listing_key. Prefer the signal-source row over
    the shadow twin, then the enriched row; order is preserved."""
    def _quality(r: dict[str, Any]) -> tuple[int, int]:
        return (
            0 if r.get("row_source") == "signal" else 1,
            0 if r.get("enrich_status") == "complete" else 1,
        )

    best: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = r.get("cross_listing_key")
        if not key:
            continue
        if key not in best or _quality(r) < _quality(best[key]):
            best[key] = r
    chosen = set(map(id, best.values()))
    return [r for r in rows if not r.get("cross_listing_key") or id(r) in chosen]


def _dataset_rows(
    db: Session,
    *,
    limit: int,
    offset: int = 0,
    columns: Optional[list[str]] = None,
    source: str = "all",
    dedup: bool = False,
    split: Optional[str] = None,
    split_ratio: float = 0.8,
    **filters: Any,
) -> dict[str, Any]:
    """Fetch, merge (signal + shadow), assemble, and post-process rows.

    dedup / split need the FULL filtered set to be correct, so those
    paths fetch everything (capped); the plain preview path fetches only
    the requested window per source."""
    full_pass = dedup or split is not None
    fetch_n = _FULL_FETCH_CAP if full_pass else offset + limit

    tuples: list[tuple[Any, ...]] = []  # (anchor, outcome, signal, analysis, ann, df)
    totals = 0
    if source in ("all", "signal"):
        stmt = _signal_stmt(**filters)
        totals += db.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar_one()
        for outcome, signal, analysis, ann, df in db.execute(
            stmt.order_by(SignalOutcome.id.desc()).limit(fetch_n)
        ).all():
            tuples.append((outcome.created_at, outcome, signal, analysis, ann, df))
    if source in ("all", "shadow"):
        stmt = _shadow_stmt(**filters)
        if stmt is not None:
            totals += db.execute(
                select(func.count()).select_from(stmt.subquery())
            ).scalar_one()
            for ann, analysis, signal, df in db.execute(
                stmt.order_by(Announcement.id.desc()).limit(fetch_n)
            ).all():
                tuples.append((analysis.created_at, None, signal, analysis, ann, df))

    # Newest first; per-source ties broken deterministically by the ids.
    _epoch = datetime(1970, 1, 1)
    tuples.sort(key=lambda t: t[0] or _epoch, reverse=True)

    signal_ids = [
        (o.signal_id if o is not None else getattr(s, "id", None))
        for _, o, s, *_ in tuples
    ]
    trade_aggs = _trade_aggregates(db, [i for i in signal_ids if i is not None])

    rows: list[dict[str, Any]] = []
    for (_, outcome, signal, analysis, ann, df), sig_id in zip(tuples, signal_ids):
        rows.append(
            _assemble_row(
                outcome, signal, analysis, ann, df, trade_aggs.get(sig_id, {})
            )
        )

    if dedup:
        rows = _dedup_rows(rows)
    if split in ("train", "val"):
        # Chronological split: oldest `split_ratio` of the set trains,
        # the newest remainder validates — random splits leak regime.
        rows.reverse()  # oldest first
        cut = int(len(rows) * split_ratio)
        rows = rows[:cut] if split == "train" else rows[cut:]
    if full_pass:
        totals = len(rows)
    rows = rows[offset : offset + limit]

    if columns:
        rows = [{k: r.get(k) for k in columns} for r in rows]
    return {"total": totals, "count": len(rows), "rows": rows}


def _parse_columns(columns: Optional[str]) -> Optional[list[str]]:
    """Validate a csv column selection against the SIGNAL catalog, preserving
    catalog order. None/empty → all columns.

    Signal-grain only. The announcement dataset has its own 97-column schema
    and must go through `_split_columns` — see the note there.
    """
    if not columns:
        return None
    wanted = {c.strip() for c in columns.split(",") if c.strip()}
    selected = [c["key"] for c in COLUMN_SPECS if c["key"] in wanted]
    return selected or None


def _split_columns(columns: Optional[str]) -> Optional[list[str]]:
    """Split a csv column selection WITHOUT filtering it against COLUMN_SPECS.

    The announcement dataset is a different grain with a different catalog, and
    `_parse_columns` keeps only names present in the SIGNAL specs. Running the
    announcement page's 97 columns through it left the three names the two
    schemas happen to share — symbol, exchange, headline — and silently dropped
    uid, announced_at, attachment_url and everything else, so the table
    rendered a wall of "—" under correct headers.

    Validation still happens, just against the right schema: announcement_rows
    and announcement_export_path both intersect with `describe <table>`, so an
    unknown name is dropped there rather than reaching the SELECT.
    """
    if not columns:
        return None
    return [c.strip() for c in columns.split(",") if c.strip()] or None


# =========================================================================
# Endpoints
# =========================================================================


@router.get("/columns")
def dataset_columns(
    source: str = Query("signals", pattern="^(signals|announcements)$"),
) -> dict[str, Any]:
    """The column catalog, plus ready-made column presets.

    Two datasets share this page. `signals` is the per-SIGNAL training set built
    from the trading DB; `announcements` is the merged per-ANNOUNCEMENT dataset
    (history + everything collected live). They are different grains, not two
    views of one table, so each brings its own catalog.
    """
    if source == "announcements":
        from app.api.warehouse import announcement_column_specs
        specs = announcement_column_specs()
        feats = [c["key"] for c in specs if c["role"] == "feature"]
        meta = [c["key"] for c in specs if c["role"] == "meta"]
        return {
            "columns": specs,
            "presets": {
                "everything": [c["key"] for c in specs],
                "features_only": meta[:4] + feats,
                "targets": [c["key"] for c in specs if c["role"] == "target"],
            },
        }

    features = [c["key"] for c in COLUMN_SPECS if c["role"] == "feature"]
    meta = [c["key"] for c in COLUMN_SPECS if c["role"] == "meta"]
    targets = [c["key"] for c in COLUMN_SPECS if c["role"] == "target"]
    return {
        "columns": COLUMN_SPECS,
        "presets": {
            "everything": [c["key"] for c in COLUMN_SPECS],
            # Model A — direction classifier: features + the direction labels.
            "model_a_direction": ["outcome_id", "symbol", "signal_time"]
            + features
            + ["ret_15m_pct", "label_15m"],
            # Model B — peak predictor: features + the peak/excursion labels.
            "model_b_peak": ["outcome_id", "symbol", "signal_time"]
            + features
            + ["high_15m", "mfe_15m_pct", "mae_15m_pct",
               "time_to_peak_min", "retrace_from_peak_pct"],
            "features_only": meta + features,
            "targets": targets,
        },
    }


@router.get("")
def dataset_rows(
    limit: int = Query(200, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    columns: Optional[str] = Query(None, description="csv of column keys"),
    source: str = Query("all", pattern="^(all|signal|shadow|announcements)$"),
    dedup: bool = Query(False, description="collapse NSE/BSE cross-listings"),
    event_type: Optional[str] = Query(None),
    action: Optional[str] = Query(None, description="BUY | SELL"),
    symbol: Optional[str] = Query(None),
    taken: Optional[str] = Query(None, description="taken | blocked"),
    label: Optional[str] = Query(None, description="UP | DOWN | FLAT"),
    enriched_only: bool = Query(False),
    since: Optional[str] = Query(None, description="ISO date/datetime (UTC)"),
    until: Optional[str] = Query(None, description="ISO date/datetime (UTC)"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if source == "announcements":
        from app.api.warehouse import announcement_rows
        return announcement_rows(
            limit=limit, offset=offset, columns=_split_columns(columns),
            symbol=symbol, event_type=event_type, since=since, until=until,
            enriched_only=enriched_only)
    return _dataset_rows(
        db,
        limit=limit,
        offset=offset,
        columns=_parse_columns(columns),
        source=source,
        dedup=dedup,
        event_type=event_type,
        action=action,
        symbol=symbol,
        taken=taken,
        label=label,
        enriched_only=enriched_only,
        since=since,
        until=until,
    )


@router.get("/stats")
def dataset_stats(
    source: str = Query("signals", pattern="^(signals|announcements)$"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Coverage report: how much of the dataset is enriched + labeled."""
    if source == "announcements":
        from app.api.warehouse import announcement_stats
        return announcement_stats()

    signal_total = db.execute(select(func.count(SignalOutcome.id))).scalar_one()
    shadow_stmt = _shadow_stmt(
        event_type=None, action=None, symbol=None, taken=None,
        label=None, enriched_only=False, since=None, until=None,
    )
    # In the flattened subquery the FIRST id column is announcements.id.
    shadow_sub = shadow_stmt.subquery() if shadow_stmt is not None else None
    shadow_total = (
        db.execute(
            select(func.count(func.distinct(shadow_sub.c.id)))
        ).scalar_one()
        if shadow_sub is not None
        else 0
    ) or 0
    total = signal_total + shadow_total

    by_status = {
        s: n
        for s, n in db.execute(
            select(DatasetFeature.status, func.count(DatasetFeature.id)).group_by(
                DatasetFeature.status
            )
        ).all()
    }
    enriched_total = sum(by_status.values())
    by_status["pending"] = max(0, total - enriched_total)

    labels = {
        (lbl or "unlabeled"): n
        for lbl, n in db.execute(
            select(
                DatasetFeature.features["label_15m"].as_string(),
                func.count(DatasetFeature.id),
            )
            .where(DatasetFeature.status == "complete")
            .group_by(DatasetFeature.features["label_15m"].as_string())
        ).all()
    }

    # Event-type counts over BOTH sources.
    event_counts: dict[str, int] = {}
    for et, n in db.execute(
        select(Announcement.event_type, func.count(SignalOutcome.id))
        .select_from(SignalOutcome)
        .join(Signal, SignalOutcome.signal_id == Signal.id, isouter=True)
        .join(Analysis, Signal.analysis_id == Analysis.id, isouter=True)
        .join(Announcement, Analysis.announcement_id == Announcement.id, isouter=True)
        .group_by(Announcement.event_type)
    ).all():
        event_counts[et or "UNKNOWN"] = event_counts.get(et or "UNKNOWN", 0) + n
    if shadow_sub is not None:
        for et, n in db.execute(
            select(shadow_sub.c.event_type, func.count()).group_by(
                shadow_sub.c.event_type
            )
        ).all():
            event_counts[et or "UNKNOWN"] = event_counts.get(et or "UNKNOWN", 0) + n
    by_event = sorted(
        ({"event_type": k, "samples": v} for k, v in event_counts.items()),
        key=lambda e: e["samples"],
        reverse=True,
    )

    bounds = db.execute(
        select(func.min(SignalOutcome.created_at), func.max(SignalOutcome.created_at))
    ).one()

    return {
        "total_rows": total,
        "sources": {"signal": signal_total, "shadow": shadow_total},
        "enrichment": by_status,
        "label_balance": labels,
        "by_event_type": by_event,
        "first_row_at": _iso(bounds[0]) if bounds[0] else None,
        "last_row_at": _iso(bounds[1]) if bounds[1] else None,
        "column_count": len(COLUMN_SPECS),
    }


@router.get("/export")
def dataset_export(
    format: str = Query("csv", pattern="^(csv|jsonl|parquet)$"),
    limit: int = Query(20000, ge=1, le=100000),
    columns: Optional[str] = Query(None),
    source: str = Query("all", pattern="^(all|signal|shadow|announcements)$"),
    dedup: bool = Query(False, description="collapse NSE/BSE cross-listings"),
    split: Optional[str] = Query(
        None, pattern="^(train|val)$",
        description="chronological split slice (oldest ratio → train)",
    ),
    split_ratio: float = Query(0.8, gt=0.0, lt=1.0),
    event_type: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
    taken: Optional[str] = Query(None),
    label: Optional[str] = Query(None),
    enriched_only: bool = Query(False),
    since: Optional[str] = Query(None),
    until: Optional[str] = Query(None),
    db: Session = Depends(get_db),
) -> PlainTextResponse:
    """The selected slice as a downloadable training file. With
    split=train|val the rows come out in CHRONOLOGICAL order — the
    oldest `split_ratio` of the filtered set trains, the newest
    remainder validates (a random split leaks regime)."""
    if source == "announcements":
        # The whole dataset, not a page of it. 303k rows x 97 columns is ~330 MB
        # of CSV: DuckDB COPYs it straight to a temp file with flat memory and
        # we stream that back in chunks. Building it as a Python string (what
        # the signal path does) would allocate it twice on a box with ~1 GB
        # free and take the trading process with it.
        import os as _os

        from fastapi.responses import StreamingResponse

        from app.api.warehouse import announcement_export_path

        kind = {"csv": "csv", "jsonl": "json", "parquet": "parquet"}[format]
        path = announcement_export_path(
            kind, columns=_split_columns(columns), symbol=symbol,
            event_type=event_type, since=since, until=until,
            enriched_only=enriched_only)
        stamp = datetime.utcnow().strftime("%Y%m%d")
        ext = {"csv": "csv", "jsonl": "jsonl", "parquet": "parquet"}[format]

        def _stream():
            try:
                with open(path, "rb") as fh:
                    while chunk := fh.read(1 << 20):   # 1 MB at a time
                        yield chunk
            finally:
                # The temp file is this request's alone; drop it either way.
                try:
                    _os.remove(path)
                except OSError:
                    pass

        return StreamingResponse(
            _stream(),
            media_type={"csv": "text/csv",
                        "jsonl": "application/x-ndjson",
                        "parquet": "application/vnd.apache.parquet"}[format],
            headers={
                "Content-Disposition": f"attachment; filename=announcements_{stamp}.{ext}",
                "Content-Length": str(path.stat().st_size),
            },
        )

    selected = _parse_columns(columns) or [c["key"] for c in COLUMN_SPECS]
    result = _dataset_rows(
        db,
        limit=limit,
        columns=selected,
        source=source,
        dedup=dedup,
        split=split,
        split_ratio=split_ratio,
        event_type=event_type,
        action=action,
        symbol=symbol,
        taken=taken,
        label=label,
        enriched_only=enriched_only,
        since=since,
        until=until,
    )
    return _write_export(result, selected, format, split)


def _write_export(result: dict, selected: list[str], format: str,
                  split: Optional[str] = None) -> PlainTextResponse:
    """Render rows as CSV or JSONL. Shared by both dataset sources so the
    announcement export and the signal export cannot drift apart."""
    stamp = datetime.utcnow().strftime("%Y%m%d")
    name = f"dataset_{split}_{stamp}" if split else f"dataset_{stamp}"
    if format == "jsonl":
        body = "\n".join(json.dumps(r, default=str) for r in result["rows"])
        return PlainTextResponse(
            body,
            media_type="application/x-ndjson",
            headers={
                "Content-Disposition": f"attachment; filename={name}.jsonl"
            },
        )
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=selected, extrasaction="ignore")
    writer.writeheader()
    for r in result["rows"]:
        writer.writerow(r)
    return PlainTextResponse(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={name}.csv"},
    )


# ---- column health / leakage report ---------------------------------------


def _pearson(pairs: list[tuple[float, float]]) -> Optional[float]:
    n = len(pairs)
    if n < 3:
        return None
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    cov = sum((x - mx) * (y - my) for x, y in pairs)
    vx = sum((x - mx) ** 2 for x, _ in pairs)
    vy = sum((y - my) ** 2 for _, y in pairs)
    if vx <= 0 or vy <= 0:
        return None
    return cov / (vx * vy) ** 0.5


def _as_number(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    return None


@router.get("/health")
def dataset_health(
    target: str = Query("ret_15m_pct"),
    limit: int = Query(5000, ge=10, le=20000),
    source: str = Query("all", pattern="^(all|signal|shadow)$"),
    enriched_only: bool = Query(False),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Per-column health over the (sampled) dataset: null rate, basic
    stats, and correlation with the chosen target. A FEATURE correlating
    ~1.0 with the target is look-ahead leakage, not alpha — flagged."""
    if target not in _VALID_KEYS:
        target = "ret_15m_pct"
    result = _dataset_rows(
        db,
        limit=limit,
        source=source,
        event_type=None, action=None, symbol=None, taken=None,
        label=None, enriched_only=enriched_only, since=None, until=None,
    )
    rows = result["rows"]
    n = len(rows)
    target_vals = [_as_number(r.get(target)) for r in rows]

    report = []
    for spec in COLUMN_SPECS:
        key = spec["key"]
        raw = [r.get(key) for r in rows]
        non_null = [v for v in raw if v is not None]
        nums = [x for v in non_null if (x := _as_number(v)) is not None]
        entry: dict[str, Any] = {
            "key": key,
            "role": spec["role"],
            "category": spec["category"],
            "null_rate": round(1.0 - len(non_null) / n, 4) if n else None,
            "numeric": bool(nums) and len(nums) == len(non_null),
        }
        if entry["numeric"] and nums:
            mean = sum(nums) / len(nums)
            entry["mean"] = round(mean, 4)
            entry["std"] = round(
                (sum((x - mean) ** 2 for x in nums) / len(nums)) ** 0.5, 4
            )
            entry["min"] = round(min(nums), 4)
            entry["max"] = round(max(nums), 4)
            pairs = [
                (x, t)
                for v, t in zip(raw, target_vals)
                if t is not None and (x := _as_number(v)) is not None
            ]
            corr = _pearson(pairs) if key != target else 1.0
            entry["corr_target"] = round(corr, 4) if corr is not None else None
            entry["corr_n"] = len(pairs)
            entry["leak_warning"] = bool(
                spec["role"] == "feature"
                and key != target
                and corr is not None
                and abs(corr) >= 0.9
                and len(pairs) >= 30
            )
        else:
            entry["distinct"] = len(set(map(str, non_null)))
        report.append(entry)

    return {"target": target, "rows_sampled": n, "columns": report}


# ---- HOLD calibration: is the bot passing on movers? ----------------------


@router.get("/calibration")
def dataset_calibration(
    move_threshold_pct: float = Query(
        1.5, gt=0.0, le=50.0,
        description="a filing 'moved' if its 15m swing (max of MFE/MAE) >= this",
    ),
    big_threshold_pct: float = Query(3.0, gt=0.0, le=100.0),
    limit: int = Query(20000, ge=100, le=100000),
    event_type: Optional[str] = Query(None),
    since: Optional[str] = Query(None, description="ISO date/datetime (UTC)"),
    until: Optional[str] = Query(None, description="ISO date/datetime (UTC)"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """HOLD calibration — of the filings the bot DECLINED, how many
    actually moved?

    Uses the real 15-minute forward reaction the DatasetBuilder already
    labels on each row (enriched/complete rows only), broken down by event
    type, LLM confidence, and sentiment, plus the taken-vs-declined mover-
    rate comparison that says whether the selection is discriminating.
    NSE/BSE cross-listings are collapsed so one move isn't counted twice.
    """
    from app.services.hold_calibration import (
        compute_calibration,
        normalise_dataset_row,
    )

    result = _dataset_rows(
        db,
        limit=limit,
        source="all",
        dedup=True,            # collapse NSE/BSE twins
        enriched_only=True,    # only rows with a complete 15m label
        event_type=event_type,
        action=None, symbol=None, taken=None, label=None,
        since=since, until=until,
    )
    normalised = [normalise_dataset_row(r) for r in result["rows"]]
    report = compute_calibration(
        normalised,
        move_threshold_pct=move_threshold_pct,
        big_threshold_pct=big_threshold_pct,
    )
    report["rows_analysed"] = len(normalised)
    report["window"] = {"since": since, "until": until}
    return report


def _get_builder(request: Request):
    """The lifespan's shared builder (its run lock + progress state make
    it the single source of truth); created lazily if missing."""
    from app.services.dataset_builder import DatasetBuilder

    builder = getattr(request.app.state, "dataset_builder", None)
    if builder is None:
        builder = DatasetBuilder()
        request.app.state.dataset_builder = builder
    return builder


@router.post("/backfill")
async def dataset_backfill(
    request: Request,
    limit: int = Query(100, ge=1, le=1000),
    full: bool = Query(
        False,
        description="run batches in the background until the WHOLE history "
        "is enriched; poll /api/dataset/backfill/status for progress",
    ),
    retry_failed: bool = Query(
        False,
        description="give rows stuck at no_candles/error/partial fresh "
        "attempts before running (an explicit operator run earns fresh "
        "retries — e.g. after Fyers reconnects or an enrichment fix)",
    ),
    rebuild: bool = Query(
        False,
        description="also RE-ENRICH already-complete rows whose feature "
        "schema is out of date (e.g. to add the full minute-by-minute "
        "trajectory to rows enriched by an older version). Implies full.",
    ),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Run an enrichment batch immediately, or (full=true) start the
    run-until-done history backfill. Needs a connected Fyers account
    for candles — without one everything lands in no_candles and is
    retried later. One history fetch covers every pending row of a
    symbol, and after-hours filings are settled without any API call,
    so a full backfill is far cheaper than row-count × calls."""
    builder = _get_builder(request)
    reset = 0
    if retry_failed:
        from sqlalchemy import update

        reset = db.execute(
            update(DatasetFeature)
            .where(DatasetFeature.status.in_(("no_candles", "error", "partial")))
            .values(attempts=0)
        ).rowcount
        db.commit()
    if full or rebuild:
        progress = builder.start_full_backfill(batch=200, rebuild=rebuild)
        return {"ok": True, "full": True, "retried": reset, **progress}
    counts = await builder.run_once(limit=limit)
    return {"ok": True, "full": False, "retried": reset, **counts}


@router.get("/backfill/status")
async def dataset_backfill_status(request: Request) -> dict[str, Any]:
    """Live progress of the full backfill + how much is still pending.

    Deliberately cheap: while the backfill runs, `remaining` is derived
    from the start snapshot minus progress (no DB hit — the UI polls
    this every few seconds); idle, it is a 30s-cached count."""
    import asyncio

    builder = _get_builder(request)
    progress = dict(builder.backfill_progress)
    remaining = builder.remaining_estimate() if progress.get("running") else None
    if remaining is None:
        remaining = await asyncio.get_running_loop().run_in_executor(
            None, builder.count_pending_cached
        )
    return {
        "progress": progress,
        "remaining": remaining,
        "collector": dict(builder.collector),
    }
