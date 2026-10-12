"""Mover model — status, variant comparison, backtest and what-if scoring.

Everything the Model page needs. The scorer itself is pure and stdlib-only
(`app/services/mover_model.py`); this layer is the read-only window onto it
plus one preview endpoint so the operator can try a threshold on real
history before switching the gate on.

  GET  /api/model/status      Artifact metadata + every variant's holdout
                              metrics. Safe when the artifact is missing —
                              `available: false` with the reason.
  GET  /api/model/variants    Just the variant list (picker refreshes).
  POST /api/model/score       What-if: score an arbitrary filing. Accepts
                              either raw feature values or the live-shaped
                              fields (symbol / headline / sentiment / …).
  GET  /api/model/preview     Replay the model over recorded signals and
                              report what the CURRENT threshold would have
                              blocked, against what those signals actually
                              did. The number that should decide whether
                              MODEL_GATE_ENABLED is worth turning on.
  POST /api/model/reload      Re-read the artifact from disk after a
                              re-export, without restarting the bot.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Analysis, Announcement, Signal, SignalOutcome
from app.db.session import get_db
from app.logging_config import get_logger
from app.services import mover_model as mm

router = APIRouter(prefix="/api/model", tags=["model"])
log = get_logger(__name__)


@router.get("/status")
def model_status() -> dict[str, Any]:
    """Artifact + variants + the live toggle state, in one call."""
    s = get_settings()
    return {
        **mm.status(),
        "settings": {
            "MODEL_ENABLED": bool(getattr(s, "MODEL_ENABLED", False)),
            "MODEL_GATE_ENABLED": bool(getattr(s, "MODEL_GATE_ENABLED", False)),
            "MODEL_VARIANT": getattr(s, "MODEL_VARIANT", "") or "",
            "MODEL_MIN_PROBABILITY": float(getattr(s, "MODEL_MIN_PROBABILITY", 0.15)),
            "MODEL_MIN_COVERAGE": float(getattr(s, "MODEL_MIN_COVERAGE", 0.5)),
        },
    }


@router.get("/variants")
def model_variants() -> dict[str, Any]:
    return {"variants": mm.variants()}


@router.post("/reload")
def model_reload() -> dict[str, Any]:
    """Re-read live_model.json. Use after re-running export_live.py."""
    mm.load(force=True)
    st = mm.status()
    log.info("model.reloaded", available=st["available"], built_at=st.get("built_at"))
    return st


@router.post("/score")
def model_score(
    payload: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    """Score one hypothetical filing.

    `features` (raw model inputs) wins when present; otherwise the live-shaped
    keys are run through the same `build_features` the risk engine uses, so
    the preview cannot drift from production scoring."""
    variant = payload.get("variant") or (getattr(get_settings(), "MODEL_VARIANT", "") or None)
    if isinstance(payload.get("features"), dict):
        feats = dict(payload["features"])
    else:
        feats = mm.build_features(
            symbol=str(payload.get("symbol") or ""),
            headline=payload.get("headline"),
            event_type=payload.get("event_type"),
            category=payload.get("category"),
            sentiment=payload.get("sentiment"),
            sentiment_score=payload.get("sentiment_score"),
            confidence=payload.get("confidence"),
            recommendation=payload.get("recommendation"),
            last_price=payload.get("last_price"),
            market_cap_cr=payload.get("market_cap_cr"),
            news_age_seconds=payload.get("news_age_seconds"),
        )
    s = mm.score(feats, variant)
    settings = get_settings()
    v, reason = mm.verdict(
        s,
        min_probability=float(
            payload.get("min_probability",
                        getattr(settings, "MODEL_MIN_PROBABILITY", 0.15))
        ),
        min_coverage=float(
            payload.get("min_coverage", getattr(settings, "MODEL_MIN_COVERAGE", 0.5))
        ),
    )
    return {
        "features": feats,
        "score": s.to_dict() if s is not None else None,
        "verdict": v,
        "reason": reason,
    }


@router.get("/preview")
def model_preview(
    variant: Optional[str] = Query(None),
    min_probability: Optional[float] = Query(None, ge=0.0, le=1.0),
    min_coverage: Optional[float] = Query(None, ge=0.0, le=1.0),
    limit: int = Query(500, ge=1, le=5000),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Replay the model over recorded signals that have a 30-minute outcome.

    This is the only honest way to set the threshold: it reports what the
    gate WOULD have blocked and whether those filings actually moved. A gate
    that blocks mostly non-movers is earning its keep; one that blocks movers
    at the same rate as it blocks everything is just cutting volume.

    Read-only, and it never mutates the live toggles.
    """
    settings = get_settings()
    thr = float(min_probability if min_probability is not None
                else getattr(settings, "MODEL_MIN_PROBABILITY", 0.15))
    cov = float(min_coverage if min_coverage is not None
                else getattr(settings, "MODEL_MIN_COVERAGE", 0.5))
    key = variant or (getattr(settings, "MODEL_VARIANT", "") or None)

    rows = db.execute(
        select(SignalOutcome, Signal, Analysis, Announcement)
        .join(Signal, SignalOutcome.signal_id == Signal.id)
        .outerjoin(Analysis, Signal.analysis_id == Analysis.id)
        .outerjoin(Announcement, Analysis.announcement_id == Announcement.id)
        .where(SignalOutcome.move_30m_pct.isnot(None))
        .order_by(SignalOutcome.id.desc())
        .limit(limit)
    ).all()

    # A "mover" here matches the model's own target as closely as the live
    # table allows: |30-minute move| over 1.5%. It is the RAW move, not
    # market-adjusted — signal_outcomes has no index leg — so treat this as
    # the direction of the effect, not a reproduction of the offline AUC.
    scored: list[dict[str, Any]] = []
    for outcome, sig, analysis, ann in rows:
        feats = mm.build_features(
            symbol=outcome.symbol or "",
            headline=getattr(ann, "headline", None),
            event_type=getattr(ann, "event_type", None),
            category=getattr(ann, "event_type", None),
            sentiment=getattr(analysis, "sentiment", None),
            sentiment_score=getattr(analysis, "sentiment_score", None),
            confidence=getattr(analysis, "confidence", None) or sig.confidence,
            recommendation=getattr(analysis, "recommendation", None),
            filed_at=getattr(ann, "filed_at", None),
            last_price=outcome.price_at_signal,
        )
        s = mm.score(feats, key)
        if s is None:
            continue
        v, _ = mm.verdict(s, min_probability=thr, min_coverage=cov)
        scored.append({
            "symbol": outcome.symbol,
            "action": outcome.action,
            "probability": s.probability,
            "percentile": s.percentile,
            "coverage": s.coverage,
            "verdict": v,
            "move_30m_pct": outcome.move_30m_pct,
            "mover": abs(float(outcome.move_30m_pct)) >= 1.5,
        })

    def rate(sel: list[dict[str, Any]]) -> Optional[float]:
        return round(sum(r["mover"] for r in sel) / len(sel), 4) if sel else None

    blocked = [r for r in scored if r["verdict"] == "block"]
    allowed = [r for r in scored if r["verdict"] == "allow"]
    abstain = [r for r in scored if r["verdict"] == "insufficient"]
    return {
        "variant": key or mm.status().get("default_variant"),
        "min_probability": thr,
        "min_coverage": cov,
        "n_scored": len(scored),
        "base_mover_rate": rate(scored),
        "blocked": {"n": len(blocked), "mover_rate": rate(blocked)},
        "allowed": {"n": len(allowed), "mover_rate": rate(allowed)},
        "insufficient": {"n": len(abstain), "mover_rate": rate(abstain)},
        # Newest 100 for the table; the aggregates above cover the full slice.
        "rows": scored[:100],
    }


async def _slm_post(endpoint: str, body: dict[str, Any], api_key: str) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=90.0) as c:
        r = await c.post(endpoint, json=body, headers={"Authorization": f"Bearer {api_key or '-'}"})
        r.raise_for_status()
        return r.json()


MIN_FILING_WORDS = 6


@router.post("/slm-chat")
async def slm_chat(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """Chat page: one message = one filing (first line headline, rest text),
    rendered in the trained prompt format and answered by the SLM at
    LLM_SLM_ENDPOINT. Read-only: nothing here reaches rules or orders."""
    from datetime import datetime, timezone
    import time

    from fastapi import HTTPException

    from app.analyzer.slm_adapter import build_prompt, market_context_for, to_analysis

    s = get_settings()
    endpoint = (getattr(s, "LLM_SLM_ENDPOINT", "") or "").strip()
    if not endpoint:
        raise HTTPException(503, "LLM_SLM_ENDPOINT is not set (Settings → AI analysis)")
    message = str(payload.get("message") or "").strip()[:20000]
    if not message:
        raise HTTPException(422, "message is empty")
    symbol = str(payload.get("symbol") or "").strip().upper()[:32]
    headline, _, text = message.partition("\n")
    # The SLM only knows one task (filing -> trade JSON); "hi" gets a random
    # BUY/SELL. ponytail: word-count heuristic, a real headline is ~10-40 words;
    # replace with a trained "not a filing" class if it misfires.
    if len(message.split()) < MIN_FILING_WORDS:
        return {"model": None, "latency_ms": 0, "prompt": "", "content": "", "not_filing": True}
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # stored-UTC convention
    system, user = build_prompt(symbol=symbol, filed_at=now.isoformat(sep=" ", timespec="seconds"),
                                headline=headline.strip()[:1000], filing_text=text.strip(),
                                **market_context_for(symbol, now))
    model = str(getattr(s, "LLM_SLM_MODEL", "") or "tradebot-slm-v3")
    t0 = time.perf_counter()
    try:
        res = await _slm_post(endpoint, {"model": model, "temperature": 0, "max_tokens": 500,
                                         "messages": [{"role": "system", "content": system},
                                                      {"role": "user", "content": user}]},
                              str(getattr(s, "LLM_SLM_API_KEY", "") or ""))
        content = res["choices"][0]["message"]["content"]
    except Exception as e:  # noqa: BLE001 — surface, never crash the page
        raise HTTPException(502, f"SLM unreachable: {str(e)[:200]}")
    out: dict[str, Any] = {"model": model, "latency_ms": round((time.perf_counter() - t0) * 1000),
                           "prompt": user, "content": content}
    try:
        out["mapped"], _ = to_analysis(content, headline=headline)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"invalid_json: {e}"[:300]
    return out
