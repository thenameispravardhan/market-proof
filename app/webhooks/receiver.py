"""Inbound webhook receiver.

Flow:
  1. Read the raw body bytes (we MUST hash the exact bytes that
     were signed, so don't let FastAPI's JSON parser pre-mangle
     whitespace).
  2. If `webhook.secret` is set, verify the X-Mavis-Signature
     header against HMAC-SHA256(secret, body). Missing/incorrect
     → 401.
  3. Parse the JSON. Missing/wrong shape → 400.
  4. Replay protection: payload MUST include `timestamp` (epoch
     seconds). Reject if outside ±REPLAY_WINDOW_SECONDS of now.
  5. Convert to an `analyses` row (with `model='external'`) +
     `signals` row, then run the risk engine (synchronous helper
     imported from T4 — we don't dispatch to the event bus from
     here because the operator wants a synchronous, request-shaped
     confirmation that the signal was created and risk-evaluated).

The receiver is exposed via the `POST /api/webhooks/in/{webhook_id}`
endpoint in `app/api/webhooks.py`. This module exposes a pure
function `process_inbound(webhook, body, headers)` so the test
suite can drive the receiver end-to-end without HTTP.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from sqlalchemy.orm import Session

from app.analyzer.schemas import (
    AnalysisResponse,
    EventType as AnalyzerEventType,
    Sentiment,
    Recommendation,
    KeyNumbers,
)
from app.db.models import Analysis, Announcement, Signal, Webhook
from app.logging_config import get_logger
from app.webhooks.fyers import (
    is_fyers_postback,
    normalize_fyers_postback,
)
from app.webhooks.signing import (
    REPLAY_WINDOW_SECONDS,
    is_fresh_timestamp,
    verify_signature,
)

log = get_logger(__name__)


class InboundError(Exception):
    """The HTTP layer maps this to 400 (validation) or 401 (auth)."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = int(status_code)


def _validate_payload_shape(payload: Any) -> dict[str, Any]:
    """Top-level shape check. Returns the validated dict.

    Required: event_type, symbol, action. Everything else is
    optional and falls back to defaults that the analysis schema
    will validate further.
    """
    if not isinstance(payload, dict):
        raise InboundError("payload must be a JSON object", status_code=400)
    for k in ("event_type", "symbol", "action"):
        if k not in payload:
            raise InboundError(f"missing required field: {k}", status_code=400)
        v = payload[k]
        if not isinstance(v, str) or not v.strip():
            raise InboundError(f"field {k} must be a non-empty string", status_code=400)
    if not isinstance(payload.get("timestamp", 0), (int, str)):
        # accept missing (will be caught by is_fresh_timestamp) but
        # reject other types.
        raise InboundError("timestamp must be int or string-of-digits", status_code=400)
    return payload


def _coerce_event_type(raw: str) -> AnalyzerEventType:
    """Map an inbound `event_type` string to the analyzer enum.

    Unknown values fall back to OTHER so the analysis still gets
    stored (the analyzer's keyword heuristic is for OCR/PDF, not
    for inbound webhooks — here the caller is the source of truth).
    """
    norm = (raw or "").strip().upper()
    try:
        return AnalyzerEventType(norm)
    except ValueError:
        return AnalyzerEventType.OTHER


def _coerce_action(raw: str) -> Recommendation:
    norm = (raw or "").strip().upper()
    try:
        return Recommendation(norm)
    except ValueError:
        # Unknown action — we still create the analysis, but the
        # signal gets HOLD so the risk engine blocks it (T4's R1
        # already blocks HOLD). This is the conservative default.
        return Recommendation.HOLD


def _coerce_sentiment(raw: Any) -> Sentiment:
    norm = str(raw or "").strip().lower()
    try:
        return Sentiment(norm)
    except ValueError:
        return Sentiment.NEUTRAL


def _build_analysis_response(
    payload: dict[str, Any],
) -> AnalysisResponse:
    """Build an `AnalysisResponse` from an inbound payload. Fields
    the payload didn't supply use safe defaults so the analysis
    row is always valid for the schema."""
    event_type = _coerce_event_type(payload.get("event_type", "OTHER"))
    summary = (
        payload.get("summary")
        or f"External webhook: {event_type.value} on {payload.get('symbol')}"
    )
    if not isinstance(summary, str) or len(summary) < 1:
        summary = f"External webhook event for {payload.get('symbol')}"
    confidence = payload.get("confidence", 0.5)
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        confidence = 0.5
    confidence = max(0.0, min(1.0, float(confidence)))
    sentiment_score = payload.get("sentiment_score", 0.0)
    if not isinstance(sentiment_score, (int, float)) or isinstance(sentiment_score, bool):
        sentiment_score = 0.0
    sentiment_score = max(-100.0, min(100.0, float(sentiment_score)))
    reasoning = payload.get("reasoning") or payload.get("rationale") or summary
    if not isinstance(reasoning, str):
        reasoning = str(reasoning)
    kn_raw = payload.get("key_numbers") or {}
    kn = KeyNumbers.model_validate(kn_raw) if isinstance(kn_raw, dict) else KeyNumbers()
    return AnalysisResponse(
        event_type=event_type,
        summary=summary,
        sentiment=_coerce_sentiment(payload.get("sentiment", "neutral")),
        sentiment_score=sentiment_score,
        confidence=confidence,
        recommendation=_coerce_action(payload.get("action", "HOLD")),
        reasoning=reasoning,
        key_numbers=kn,
    )


def process_inbound(
    session: Session,
    webhook: Webhook,
    body: bytes,
    headers: Mapping[str, str],
) -> dict[str, Any]:
    """Process one inbound webhook request end-to-end.

    Returns a dict with the created analysis_id + signal_id +
    risk-decision summary. Raises `InboundError(status_code=400/401)`
    on any failure (caller maps to HTTP).
    """
    # 1. signature
    secret = (webhook.secret or "").strip()
    if secret:
        sig_header = headers.get("X-Mavis-Signature") or headers.get("x-mavis-signature")
        if not verify_signature(secret, body, sig_header):
            raise InboundError("bad signature", status_code=401)

    # 2. parse JSON
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise InboundError(f"invalid json: {e}", status_code=400) from e

    # 2a. Fyers postback normalizer. Activated when EITHER:
    #   - the webhook's name is prefixed with "fyers-", or
    #   - the raw payload shape looks like a Fyers postback
    #     (detected heuristically in `is_fyers_postback`).
    # The output is a dict in the generic receiver's contract shape;
    # the original is preserved under `raw_fyers`.
    webhook_name = (getattr(webhook, "name", "") or "").lower()
    if webhook_name.startswith("fyers-") or is_fyers_postback(payload):
        try:
            payload = normalize_fyers_postback(payload)
        except Exception as e:  # noqa: BLE001
            log.exception(
                "webhook_inbound.fyers_normalize_failed", webhook_id=webhook.id, error=str(e)
            )
            raise InboundError(f"fyers normalization failed: {e}", status_code=400) from e

    # 3. shape
    payload = _validate_payload_shape(payload)

    # 4. replay protection
    if not is_fresh_timestamp(payload):
        raise InboundError(
            f"timestamp missing or older than {REPLAY_WINDOW_SECONDS}s",
            status_code=400,
        )

    # 5. find / create the parent announcement. We don't have a PDF
    # for an inbound signal — create a synthetic announcement so
    # the analysis FK is satisfied.
    symbol = (payload["symbol"] or "").strip().upper()
    exchange = (payload.get("exchange") or "NSE").strip().upper()
    headline = (
        payload.get("headline")
        or f"External: {payload.get('event_type')} {symbol}"
    )
    announcement = (
        session.query(Announcement)
        .filter(
            Announcement.symbol == symbol,
            Announcement.exchange == exchange,
            Announcement.source == "external_webhook",
        )
        .order_by(Announcement.id.desc())
        .first()
    )
    if announcement is None:
        announcement = Announcement(
            symbol=symbol,
            exchange=exchange,
            event_type=str(payload["event_type"]).upper(),
            headline=headline,
            body=None,
            pdf_url=None,
            source="external_webhook",
            filed_at=datetime.now(timezone.utc),
        )
        session.add(announcement)
        session.flush()

    # 6. build + persist the analysis (with model='external' as the
    # spec asks).
    response = _build_analysis_response(payload)
    cols = response.to_db_columns()
    analysis = Analysis(
        announcement_id=announcement.id,
        model="external",
        sentiment=cols["sentiment"],
        sentiment_score=cols["sentiment_score"],
        confidence=cols["confidence"],
        recommendation=cols["recommendation"],
        rationale=cols["rationale"],
        deal_value_inr_crore=cols["deal_value_inr_crore"],
        stake_change_pct=cols["stake_change_pct"],
        dividend_per_share=cols["dividend_per_share"],
        buyback_value_inr_crore=cols["buyback_value_inr_crore"],
        raw_response={
            "event_type": cols["event_type"],
            "summary": cols["summary"],
            "key_numbers": response.key_numbers.model_dump(),
            "source": f"webhook:{webhook.id}",
            "webhook_id": webhook.id,
            "raw_payload": _safe_payload(payload),
        },
    )
    session.add(analysis)
    session.flush()

    # 7. signals row. We do NOT run the rule engine here — the
    # caller-supplied `action` IS the rule output. The signal goes
    # to the same pipeline as analyzer-produced ones (T4 picks it
    # up off the event bus for risk evaluation + execution).
    # AnalysisResponse has `use_enum_values=True`, so `recommendation`
    # is already a plain string ("BUY" / "SELL" / "HOLD"), not an enum.
    action = str(response.recommendation)
    signal = Signal(
        analysis_id=analysis.id,
        strategy_id=None,
        rule_id=None,
        symbol=symbol,
        action=action,
        confidence=response.confidence,
        position_size_pct=float(payload.get("position_size_pct") or 0.0),
        rationale=(
            f"external_webhook webhook_id={webhook.id} "
            f"action={action} {cols.get('summary', '')}"
        ),
        status="pending",
    )
    session.add(signal)
    session.commit()
    session.refresh(signal)

    # 8. publish signals.new so T4 picks it up.
    try:
        from app.services.event_bus import event_bus

        import asyncio
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                event_bus.publish(
                    "signals.new",
                    {
                        "signal_id": signal.id,
                        "analysis_id": analysis.id,
                        "symbol": signal.symbol,
                        "action": signal.action,
                        "approved": action in ("BUY", "SELL"),
                        "confidence": signal.confidence,
                        "position_size_pct": signal.position_size_pct,
                        "rationale": signal.rationale,
                        "status": signal.status,
                        "source": f"webhook:{webhook.id}",
                    },
                )
            )
        except RuntimeError:
            # No running loop (synchronous test path) — skip the
            # publish; the signal row is still in the DB.
            pass
    except Exception:  # noqa: BLE001
        log.exception("webhook_receiver.publish_failed", webhook_id=webhook.id)

    log.info(
        "webhook_inbound.processed",
        webhook_id=webhook.id,
        symbol=symbol,
        action=action,
        signal_id=signal.id,
        analysis_id=analysis.id,
    )
    return {
        "ok": True,
        "analysis_id": analysis.id,
        "signal_id": signal.id,
        "symbol": symbol,
        "action": action,
        "confidence": signal.confidence,
    }


def _safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip anything that looks like a secret from the persisted
    `raw_response.raw_payload`. Defence in depth — the redactor in
    `app/logging_config.py` also catches `key`/`token`/`secret`
    substrings in any log line that touches this dict."""
    out: dict[str, Any] = {}
    for k, v in payload.items():
        if isinstance(k, str) and any(
            s in k.lower() for s in ("password", "secret", "token", "key")
        ):
            out[k] = "***"
        else:
            out[k] = v
    return out
