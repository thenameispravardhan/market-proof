"""Analyzer service: subscribes to `announcements.new` and produces
`analyses` + `signals` rows.

Pipeline (per `announcements.new` event):
  1. Skip if an `analyses` row already exists for `announcement_id`
     (idempotency).
  2. Load the announcement, pick a prompt template (event-type-aware
     via the keyword heuristic, falling back to DEFAULT).
  3. Call DeepSeek; validate the JSON.
  4. Persist an `analyses` row.
  5. Run the rules engine against the analysis dict to get an action.
  6. Compute entry / stop-loss / target / RR (deterministic stub
     for v1 — see `_compute_trade_levels`).
  7. Run a minimal risk check (T1 stub: `confidence > 0`,
     `MAX_SIGNALS_PER_DAY`, etc.). Mark `approved` accordingly.
  8. Persist a `signals` row.
  9. Publish `signals.new` on the event bus. (The /ws relay picks it
     up and broadcasts to UI clients.)

Failures inside `_handle_one` are caught and logged — the service
stays alive across a malformed announcement or a 5xx from DeepSeek.

The service has the same lifecycle as `MonitorManager` (start/stop,
idempotent) and is wired into the FastAPI lifespan (T5 will add the
startup hook; for now there's a `run_forever` entry point for
standalone use).

Speed-trading rules (NEW):
  - STALENESS GATE: at step 0.5 (right after idempotency check, before
    the LLM call), if `(now - announcement.filed_at) > MAX_NEWS_AGE_SECONDS`
    we skip the DeepSeek call, write a placeholder analyses row tagged
    `reason="stale_news"`, and return. This implements the "no trades
    on old queued news" rule — the spike is gone, so even a correct
    analysis won't catch the move.
  - STARTUP SWEEP: when `Service.start()` runs we also pre-mark every
    announcement older than the threshold that has no analyses row.
    This protects against replay paths (a future manual replay tool, a
    bus redelivery, a stray cron job) firing the LLM on the backlog.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analyzer.deepseek_client import DeepSeekClient, DeepSeekError
from app.analyzer.prompts import (
    DEFAULT_EVENT_TYPE,
    append_announcement_context,
    detect_event_type,
    is_non_material,
    load_default_template,
    load_template,
    render_system_prompt,
    render_user_prompt,
)
from app.analyzer.rules_engine import (
    RuleMatch,
    enrich_analysis_context,
    evaluate as rules_evaluate,
    get_or_create_default_strategy,
    load_rules_for_strategy,
    publish_match,
)
from app.analyzer.schemas import (
    AnalysisResponse,
    EventType,
    analysis_to_dict,
)
from app.config import get_settings
from app.db.models import Analysis, Announcement, Signal
from app.logging_config import get_logger
from app.services.event_bus import event_bus

log = get_logger(__name__)


# Channel T4 (execution) subscribes to.
CHANNEL_NEW_SIGNAL = "signals.new"

# Re-export so callers (e.g. the T3 ws relay) can find it.
__all__ = ["Service", "CHANNEL_NEW_SIGNAL", "process_announcement"]


def _default_session_factory() -> Callable[[], Session]:
    """Lazy SessionLocal — tests that swap the engine still get picked up."""
    from app.db.session import SessionLocal
    return SessionLocal


# -- Service -------------------------------------------------------------


class Service:
    """Owns the analyzer loop and the DeepSeek client.

    Lifecycle:
        svc = Service()
        task = svc.start()           # subscribes to announcements.new
        ...
        svc.stop()
        await svc.wait_until_stopped()

    Tests drive `process_announcement` directly with a stubbed
    DeepSeek client; the loop is only tested in the smoke test.
    """

    def __init__(
        self,
        *,
        deepseek_client: Optional[DeepSeekClient] = None,
        session_factory: Optional[Callable[[], Session]] = None,
        analysis_already_exists: Optional[Callable[[Session, int], bool]] = None,
    ) -> None:
        self._deepseek = deepseek_client or DeepSeekClient()
        self._session_factory = session_factory or _default_session_factory()
        self._analysis_exists = analysis_already_exists or _default_analysis_exists
        self._stop_event: asyncio.Event = asyncio.Event()
        self._ready_event: asyncio.Event = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        self._sub_queue: Optional[asyncio.Queue[Any]] = None
        self._sub_channel: Optional[str] = None
        # Risk-check counters (sliding window per UTC day).
        self._signals_today: dict[str, int] = {}

    # -- lifecycle -------------------------------------------------------

    def start(self) -> asyncio.Task[None]:
        if self._task is not None and not self._task.done():
            return self._task
        self._stop_event.clear()
        self._ready_event.clear()
        # Pre-mark every stale announcement in the DB before we even
        # subscribe. This closes the "old queued news" hole: even if a
        # future tool republishes every announcement row, the analyses
        # table already has placeholders so `_analysis_exists` returns
        # True and the LLM is never called. Runs synchronously — the
        # query is indexable (announcement_id FK) and the writes are
        # idempotent.
        try:
            self._pre_mark_stale_announcements()
        except Exception:  # noqa: BLE001
            log.exception("analyzer.startup_sweep_failed")
        self._task = asyncio.create_task(self._run(), name="analyzer")
        return self._task

    def _pre_mark_stale_announcements(self) -> None:
        """One-shot sweep: mark every announcement older than the
        freshness threshold that has no analyses row.

        Idempotent: the existing `_default_analysis_exists` check
        inside `_store_failed_analysis` is a no-op when an analyses
        row already exists, so this is safe to run on every start().
        """
        from sqlalchemy import select
        from app.db.models import Analysis, Announcement

        settings = get_settings()
        max_age_s = int(getattr(settings, "MAX_NEWS_AGE_SECONDS", 90))
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_s)

        with self._session_factory() as session:
            # Find every announcement older than cutoff that has NO
            # analyses row yet. SQLite stores naive UTC datetimes, so
            # we compare against a naive cutoff (assumed UTC).
            cutoff_naive = cutoff.replace(tzinfo=None)
            stale_rows = (
                session.execute(
                    select(Announcement)
                    .where(Announcement.filed_at.isnot(None))
                    .where(Announcement.filed_at < cutoff_naive)
                )
                .scalars()
                .all()
            )
            if not stale_rows:
                log.info(
                    "analyzer.startup_sweep",
                    stale_found=0,
                    max_age_seconds=max_age_s,
                )
                return

            # Filter down to rows that have no analyses child.
            skipped = 0
            for ann in stale_rows:
                has = session.execute(
                    select(Analysis.id).where(Analysis.announcement_id == ann.id)
                ).first()
                if has is not None:
                    continue
                # Reuse the same placeholder shape as the per-event
                # gate so downstream consumers (UI, audit, queries)
                # see one consistent row format regardless of
                # whether the gate fired in-flight or at startup.
                session.add(
                    Analysis(
                        announcement_id=ann.id,
                        model="none",
                        sentiment="neutral",
                        sentiment_score=0.0,
                        confidence=0.0,
                        recommendation="HOLD",
                        rationale=(
                            f"Pre-marked at startup: filed_at older than "
                            f"{max_age_s}s (stale_news gate)."
                        ),
                        raw_response={
                            "event_type": "OTHER",
                            "error": "stale_news",
                            "pre_marked": True,
                            "max_age_seconds": max_age_s,
                        },
                    )
                )
                skipped += 1
            session.commit()
            log.info(
                "analyzer.startup_sweep",
                stale_found=len(stale_rows),
                pre_marked=skipped,
                max_age_seconds=max_age_s,
            )

    async def wait_until_ready(self, timeout: float = 2.0) -> None:
        """Block until the loop has subscribed to the event bus.

        Tests use this to avoid a race between `start()` and the first
        `event_bus.publish(...)`.
        """
        await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)

    def stop(self) -> None:
        self._stop_event.set()
        if self._sub_queue is not None and self._sub_channel is not None:
            event_bus.unsubscribe(self._sub_channel, self._sub_queue)
            self._sub_queue = None
            self._sub_channel = None

    async def wait_until_stopped(self) -> None:
        if self._task is None:
            return
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def aclose(self) -> None:
        await self._deepseek.aclose()

    # -- main loop -------------------------------------------------------

    async def _run(self) -> None:
        from app.monitors.base import CHANNEL_NEW  # avoid circular at import
        self._sub_channel = CHANNEL_NEW
        self._sub_queue = event_bus.subscribe(CHANNEL_NEW)
        log.info("analyzer.start", channel=CHANNEL_NEW)
        self._ready_event.set()
        try:
            while not self._stop_event.is_set():
                try:
                    event = await asyncio.wait_for(
                        self._sub_queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue
                try:
                    await self._handle_event(event.payload)
                except Exception:  # noqa: BLE001
                    # An unhandled error must not stop the loop.
                    log.exception("analyzer.handler_crashed")
        finally:
            log.info("analyzer.stop")
            if self._sub_queue is not None and self._sub_channel is not None:
                event_bus.unsubscribe(self._sub_channel, self._sub_queue)
                self._sub_queue = None
                self._sub_channel = None

    # -- public: per-announcement processor -----------------------------

    async def process_announcement(self, announcement_id: int) -> Optional[Signal]:
        """Process a single announcement end-to-end.

        Public entry point used by tests and by `_handle_event`.
        Returns the persisted `Signal` (or None on failure / no
        template).
        """
        return await self._process_in_session(announcement_id)

    async def _handle_event(self, payload: dict[str, Any]) -> Optional[Signal]:
        ann_id = payload.get("announcement_id")
        if not isinstance(ann_id, int):
            log.warning("analyzer.bad_payload", payload=payload)
            return None
        return await self._process_in_session(ann_id)

    async def _process_in_session(
        self, announcement_id: int
    ) -> Optional[Signal]:
        loop = asyncio.get_running_loop()

        # Step 1: load announcement + check idempotency.
        announcement, already = await loop.run_in_executor(
            None, _load_announcement_and_check,
            self._session_factory, announcement_id, self._analysis_exists,
        )
        if announcement is None:
            log.warning("analyzer.announcement_not_found", announcement_id=announcement_id)
            return None
        if already:
            log.info("analyzer.skip_existing_analysis", announcement_id=announcement_id)
            return None

        # Step 1.5: STALENESS GATE. The spike is gone; don't burn an LLM
        # call on a stale filing and don't open a position that's
        # guaranteed to be late. We persist a placeholder analysis row
        # (same shape as a real failure path) so a future replay won't
        # re-trigger this announcement, and we log the skip.
        settings = get_settings()
        max_age_s = int(getattr(settings, "MAX_NEWS_AGE_SECONDS", 90))
        filed_at = announcement.filed_at
        age_seconds: Optional[float] = None
        if filed_at is not None:
            # filed_at comes back from the DB as a naive datetime in our
            # SQLite schema; treat naive as UTC (matches monitor + DB
            # convention) before subtracting now.
            if filed_at.tzinfo is None:
                filed_at = filed_at.replace(tzinfo=timezone.utc)
            age_seconds = (datetime.now(timezone.utc) - filed_at).total_seconds()
            if age_seconds > max_age_s:
                log.warning(
                    "analyzer.stale_news_skipped",
                    announcement_id=announcement_id,
                    symbol=announcement.symbol,
                    age_seconds=round(age_seconds, 2),
                    max_age_seconds=max_age_s,
                    headline=announcement.headline,
                )
                await loop.run_in_executor(
                    None, _store_failed_analysis,
                    self._session_factory, announcement_id,
                    "stale_news",
                    {
                        "age_seconds": round(age_seconds, 2),
                        "max_age_seconds": max_age_s,
                        "filed_at": filed_at.isoformat(),
                    },
                )
                return None

        # Step 1.7: PRE-LLM NOISE FILTER. Clearly-administrative filings
        # (trading-window notices, compliance certificates, newspaper
        # publications, …) never move the stock on their own. Skip the
        # slow, paid LLM call for them — and because the analyzer
        # processes announcements serially, this also stops the queue
        # backing up behind junk so a real market-mover isn't stuck
        # waiting. We persist the usual placeholder analysis so the
        # announcement isn't retried.
        if getattr(settings, "PRE_LLM_FILTER_ENABLED", True) and is_non_material(
            announcement.headline or ""
        ):
            log.info(
                "analyzer.non_material_skipped",
                announcement_id=announcement_id,
                symbol=announcement.symbol,
                headline=announcement.headline,
            )
            await loop.run_in_executor(
                None, _store_failed_analysis,
                self._session_factory, announcement_id,
                "non_material_filing",
                {"headline": announcement.headline or ""},
            )
            return None

        # Step 2: pick prompt template (event-type heuristic + DEFAULT).
        detected = detect_event_type(
            announcement.headline or "", announcement.pdf_url
        )
        template = await loop.run_in_executor(
            None, _pick_template,
            self._session_factory, detected,
        )
        if template is None:
            log.error(
                "analyzer.no_template",
                announcement_id=announcement_id,
                detected_event_type=detected,
                headline=announcement.headline,
            )
            # Store a placeholder analysis so we don't loop on the
            # same announcement forever.
            await loop.run_in_executor(
                None, _store_failed_analysis,
                self._session_factory, announcement_id,
                "no_prompt_template",
                {"detected_event_type": detected, "reason": "no template"},
            )
            return None

        log.info(
            "analyzer.template_picked",
            announcement_id=announcement_id,
            detected_event_type=detected,
            template_event_type=template.event_type,
            template_version=template.version,
        )

        # Step 3: call DeepSeek. Use the URL we have — empty string
        # only if the announcement had no PDF.
        pdf_url = announcement.pdf_url or ""
        # Inject the DETECTED event type for context — the single DEFAULT
        # prompt handles every category, but telling the model what kind
        # of filing it's looking at still helps.
        system = render_system_prompt(template, event_type=detected)
        user = render_user_prompt(
            template,
            pdf_url=pdf_url,
            extra={
                "headline": announcement.headline or "",
                "symbol": announcement.symbol or "",
                "exchange": announcement.exchange or "",
            },
        )
        if "{{headline}}" not in template.user_template:
            # Template doesn't place the headline itself — append the
            # grounding block so the LLM analyses real text instead of
            # hallucinating from a URL it cannot open.
            user = append_announcement_context(
                user,
                symbol=announcement.symbol or "",
                exchange=announcement.exchange or "",
                headline=announcement.headline or "",
            )
        try:
            # LLM latency cap (RISK.md §5): news alpha is gone in seconds —
            # if DeepSeek is slow, discard the opportunity rather than place
            # a late trade.
            llm_timeout = float(getattr(settings, "LLM_TIMEOUT_SECONDS", 18.0))
            # Clamp the template's max_tokens against the global cap:
            # shorter completions generate faster, and a few hundred
            # tokens is plenty for the signal JSON. min() means an
            # operator can still set a *lower* per-template value, but
            # never blow past the global safety cap.
            max_tokens = min(
                int(template.max_tokens),
                int(getattr(settings, "LLM_MAX_TOKENS", 512)),
            )
            ds_result = await asyncio.wait_for(
                self._deepseek.complete(
                    system=system,
                    user=user,
                    model=template.model,
                    temperature=template.temperature,
                    max_tokens=max_tokens,
                ),
                timeout=llm_timeout,
            )
        except asyncio.TimeoutError:
            log.warning(
                "analyzer.llm_timeout",
                announcement_id=announcement_id,
                timeout_s=llm_timeout,
            )
            await loop.run_in_executor(
                None, _store_failed_analysis,
                self._session_factory, announcement_id,
                "llm_timeout",
                {"timeout_s": llm_timeout},
            )
            return None
        except DeepSeekError as e:
            log.error(
                "analyzer.deepseek_failed",
                announcement_id=announcement_id,
                error=str(e),
                status_code=e.status_code,
            )
            await loop.run_in_executor(
                None, _store_failed_analysis,
                self._session_factory, announcement_id,
                "deepseek_error",
                {"error": str(e), "status_code": e.status_code},
            )
            return None
        except Exception as e:  # noqa: BLE001
            log.exception(
                "analyzer.deepseek_unhandled",
                announcement_id=announcement_id,
            )
            await loop.run_in_executor(
                None, _store_failed_analysis,
                self._session_factory, announcement_id,
                "deepseek_unhandled",
                {"error": repr(e)},
            )
            return None

        # Step 4: validate the JSON. On failure store raw + OTHER.
        try:
            parsed_json = json.loads(ds_result.content)
        except json.JSONDecodeError as e:
            log.error(
                "analyzer.invalid_json",
                announcement_id=announcement_id,
                error=str(e),
            )
            await loop.run_in_executor(
                None, _store_failed_analysis,
                self._session_factory, announcement_id,
                "invalid_json",
                {"raw": ds_result.content[:2000], "error": str(e)},
            )
            return None

        try:
            response = AnalysisResponse.model_validate(parsed_json)
        except ValidationError as e:
            log.error(
                "analyzer.schema_validation_failed",
                announcement_id=announcement_id,
                error=str(e),
            )
            await loop.run_in_executor(
                None, _store_failed_analysis,
                self._session_factory, announcement_id,
                "schema_validation_failed",
                {"raw": parsed_json, "error": str(e)},
            )
            return None

        analysis_dict = analysis_to_dict(response)

        # Step 5+6+7+8: persist analysis, run rules, persist signal.
        signal, approved = await loop.run_in_executor(
            None, _persist_analysis_and_signal,
            self._session_factory, announcement, response, analysis_dict,
            ds_result, self._signals_today,
        )

        # Step 9: publish.
        await event_bus.publish(
            CHANNEL_NEW_SIGNAL,
            _signal_payload(signal, response, analysis_dict, approved=approved),
        )
        # Best-effort audit trail.
        try:
            from app.services.audit_service import log_event
            log_event(
                actor="system",
                action="signal.created",
                target=f"signal:{signal.id}",
                after=_signal_payload(signal, response, analysis_dict, approved=approved),
            )
        except Exception:  # pragma: no cover — optional
            pass

        return signal


# -- Helpers (sync, run in executor) ------------------------------------


def _load_announcement_and_check(
    session_factory: Callable[[], Session],
    announcement_id: int,
    exists_fn: Callable[[Session, int], bool],
) -> tuple[Optional[Announcement], bool]:
    with session_factory() as session:
        a = session.get(Announcement, announcement_id)
        if a is None:
            return None, False
        already = exists_fn(session, announcement_id)
        return a, already


def _default_analysis_exists(session: Session, announcement_id: int) -> bool:
    return session.execute(
        select(func.count(Analysis.id)).where(
            Analysis.announcement_id == announcement_id
        )
    ).scalar_one() > 0


def _pick_template(
    session_factory: Callable[[], Session], detected_event_type: str
) -> Optional[Any]:
    # Single-prompt mode: ONE editable prompt (the DEFAULT row) drives
    # every filing, regardless of the detected event type. Event
    # detection still runs to tag the announcement and give the prompt
    # context, but there's only one prompt to maintain and edit.
    with session_factory() as session:
        return load_default_template(session)


def _store_failed_analysis(
    session_factory: Callable[[], Session],
    announcement_id: int,
    reason: str,
    raw_payload: dict[str, Any],
) -> None:
    """Persist a placeholder analyses row when the real path fails.

    We do this so the same announcement doesn't trigger retries
    forever — once an `analyses` row exists for `announcement_id`,
    the service skips the message on the event bus.
    """
    with session_factory() as session:
        # Idempotency: don't double-insert.
        if _default_analysis_exists(session, announcement_id):
            return
        a = Analysis(
            announcement_id=announcement_id,
            model="none",
            sentiment="neutral",
            sentiment_score=0.0,
            confidence=0.0,
            recommendation="HOLD",
            rationale=f"Analyzer failed: {reason}",
            raw_response={"event_type": "OTHER", "error": reason, **raw_payload},
        )
        session.add(a)
        session.commit()


def _persist_analysis_and_signal(
    session_factory: Callable[[], Session],
    announcement: Announcement,
    response: AnalysisResponse,
    analysis_dict: dict[str, Any],
    ds_result: Any,
    signals_today: dict[str, int],
) -> tuple[Signal, bool]:
    """Persist the analysis, run rules, persist signal.

    Returns (signal, approved). The caller publishes.
    """
    with session_factory() as session:
        # Persist analysis row.
        cols = response.to_db_columns()
        a = Analysis(
            announcement_id=announcement.id,
            model=ds_result.model,
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
                "key_numbers": analysis_dict.get("key_numbers", {}),
                "model": ds_result.model,
                "tokens": {
                    "prompt": ds_result.prompt_tokens,
                    "completion": ds_result.completion_tokens,
                },
                "latency_ms": round(ds_result.latency_ms, 2),
                "cost_usd": round(ds_result.cost_usd, 6),
            },
        )
        session.add(a)
        session.flush()

        # Rules engine.
        strategy = get_or_create_default_strategy(session)
        rules = load_rules_for_strategy(session, strategy.id)
        # Add the symbol/exchange for the rule context, then enrich with the
        # cheaply-available market context (sector) so rules can gate on it.
        # Price / change_pct / adv_crore need a live quote and are only
        # filled when one is passed to enrich_analysis_context (not wired in
        # this offline analysis path yet) — they stay absent here, which the
        # engine treats as a fail-safe non-match.
        from app.risk.engine import load_sector_map

        eval_ctx = dict(analysis_dict)
        eval_ctx["symbol"] = announcement.symbol
        eval_ctx["exchange"] = announcement.exchange
        eval_ctx = enrich_analysis_context(eval_ctx, sector_map=load_sector_map())
        match = rules_evaluate(eval_ctx, rules)

        # Trade levels (deterministic stub for v1).
        levels = _compute_trade_levels(analysis_dict)

        # Risk check (T1 stub).
        approved, deny_reason = _risk_check(response, match, signals_today)
        # Position size comes from action_params, with a default of
        # the global MAX_SINGLE_POSITION_PCT.
        position_size_pct = float(
            match.action_params.get("position_size_pct")
            or get_settings().MAX_SINGLE_POSITION_PCT
        )
        if not approved:
            position_size_pct = 0.0

        s = Signal(
            analysis_id=a.id,
            strategy_id=strategy.id,
            rule_id=match.rule_id,
            symbol=announcement.symbol,
            action=match.action,
            confidence=response.confidence,
            position_size_pct=position_size_pct,
            rationale=match.rationale or "",
            status="pending" if approved else "blocked",
        )
        # Extra fields not on the model — we round-trip via rationale
        # so the UI can show entry/SL/target/RR. (T1's signals table
        # doesn't have dedicated columns; we pack them into rationale.)
        if levels is not None:
            s.rationale = (
                f"{match.rationale or ''} "
                f"entry={levels['entry']:.2f} sl={levels['stop_loss']:.2f} "
                f"target={levels['target']:.2f} rr={levels['rr']:.2f}"
            ).strip()
        if not approved and deny_reason:
            s.rationale = (s.rationale or "") + f" | denied: {deny_reason}"

        session.add(s)
        session.commit()
        session.refresh(s)

        return s, approved


def _risk_check(
    response: AnalysisResponse,
    match: RuleMatch,
    signals_today: dict[str, int],
) -> tuple[bool, Optional[str]]:
    """T1 stub: minimum sanity checks before approving a signal.

    Real risk logic lives in T4; this is just enough so the service
    exercises the full code path. Returns (approved, deny_reason).
    """
    settings = get_settings()
    if response.confidence is None or float(response.confidence) <= 0.0:
        return False, "confidence <= 0"
    if match.action == "BLOCK":
        return False, "rule.action == BLOCK"
    if match.action == "HOLD":
        # We still persist the HOLD signal (for observability) but
        # mark it blocked so T4 doesn't try to trade it.
        return False, "rule.action == HOLD"

    # MAX_SIGNALS_PER_DAY — sliding window per UTC day.
    # The dict is mutated in-place so the caller's view is updated.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today not in signals_today:
        # Day rolled over (or first call) — reset the window.
        signals_today.clear()
        signals_today[today] = 0
    n = signals_today.get(today, 0) + 1
    signals_today[today] = n  # always track attempts, even when denied
    if n > settings.MAX_SIGNALS_PER_DAY:
        return False, f"MAX_SIGNALS_PER_DAY={settings.MAX_SIGNALS_PER_DAY} exceeded"
    return True, None


def _compute_trade_levels(
    analysis_dict: dict[str, Any],
) -> Optional[dict[str, float]]:
    """Deterministic entry/SL/target/RR for v1.

    We don't have a price feed yet (T4 wires that up). For now, use
    the deal_value_inr_crore (if present) as a stand-in for "entry",
    and derive a 5% stop-loss, 15% target on the sentiment. RR = 3.0
    when both are derivable, else None.

    Real implementation is T4's job.
    """
    sent = analysis_dict.get("sentiment_score")
    deal = analysis_dict.get("deal_value_inr_crore")
    if sent is None and deal is None:
        return None
    entry = float(deal) if deal is not None else 100.0
    # SL distance scales with |sentiment_score|; cap at 5%.
    sl_dist = min(0.05, max(0.01, abs(float(sent or 0)) / 100.0 * 0.5 + 0.01))
    target_dist = sl_dist * 3.0  # 3:1 reward/risk
    sl = entry * (1.0 - sl_dist)
    target = entry * (1.0 + target_dist)
    return {
        "entry": entry,
        "stop_loss": sl,
        "target": target,
        "rr": target_dist / sl_dist,
    }


def _signal_payload(
    signal: Signal,
    response: AnalysisResponse,
    analysis_dict: dict[str, Any],
    *,
    approved: bool,
) -> dict[str, Any]:
    return {
        "signal_id": signal.id,
        "analysis_id": signal.analysis_id,
        "symbol": signal.symbol,
        "action": signal.action,
        "approved": approved,
        "confidence": signal.confidence,
        "position_size_pct": signal.position_size_pct,
        "rule_id": signal.rule_id,
        "event_type": str(response.event_type),
        "sentiment": str(response.sentiment),
        "sentiment_score": response.sentiment_score,
        "summary": response.summary,
        "key_numbers": response.key_numbers.model_dump(),
        "rationale": signal.rationale,
        "status": signal.status,
    }


# -- Module-level convenience for ad-hoc use ----------------------------


async def process_announcement(announcement_id: int) -> Optional[Signal]:
    """One-shot: spin up a service, process one announcement, close."""
    svc = Service()
    try:
        return await svc.process_announcement(announcement_id)
    finally:
        await svc.aclose()
