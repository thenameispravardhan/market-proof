"""Shadow scoring: the SLM reads the same filing as the live model, off the
decision path.

`LLM_PROVIDER` is a hard switch: one model decides and nothing records the
other. That makes "does the SLM beat DeepSeek?" unanswerable on live data,
and live data is the only window that is out-of-sample by construction
(it postdates every training cutoff). With LLM_SHADOW_ENABLED, every
LLM-track filing is ALSO scored by the SLM after the live signal has been
emitted, and the SLM's verdict is stored in `shadow_analyses`.

Guarantees, because this runs next to live trading:
  * scheduled only after the live signal is published, so it never delays
    an entry;
  * bounded: at most MAX_CONCURRENT calls in flight and MAX_PENDING queued;
    beyond that a filing is skipped and counted, never queued without
    limit on a 2 GB box;
  * every failure (timeout, bad JSON, endpoint down) is a stored row, not
    an exception into the analyzer.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any, Callable, Optional

from app.analyzer.slm_adapter import build_prompt, market_context_for, to_analysis
from app.config import get_settings
from app.logging_config import get_logger

log = get_logger(__name__)

MAX_CONCURRENT = 2
MAX_PENDING = 20


def _default_session_factory():
    from app.db.session import SessionLocal

    return SessionLocal


class ShadowScorer:
    def __init__(self, *, client_factory: Optional[Callable[[str, str], Any]] = None,
                 session_factory: Optional[Callable[[], Any]] = None) -> None:
        self._client_factory = client_factory
        self._session_factory = session_factory
        self._client: Any = None
        self._client_key: Optional[tuple[str, str]] = None
        self._sem: Optional[asyncio.Semaphore] = None
        self._pending = 0
        self._tasks: set[asyncio.Task[None]] = set()
        self.skipped = 0

    # -- config ----------------------------------------------------------

    @staticmethod
    def enabled(settings: Any = None) -> bool:
        s = settings or get_settings()
        return (bool(getattr(s, "LLM_SHADOW_ENABLED", False))
                and getattr(s, "LLM_PROVIDER", "deepseek") != "slm"
                and bool((getattr(s, "LLM_SLM_ENDPOINT", "") or "").strip()))

    def _get_client(self, endpoint: str, api_key: str) -> Any:
        key = (endpoint, api_key)
        if self._client is None or self._client_key != key:
            if self._client_factory is not None:
                self._client = self._client_factory(endpoint, api_key)
            else:
                from app.analyzer.deepseek_client import DeepSeekClient

                self._client = DeepSeekClient(endpoint=endpoint, api_key=api_key or "-",
                                              max_retries=0)
            self._client_key = key
        return self._client

    # -- scheduling ------------------------------------------------------

    def schedule(self, *, announcement_id: Optional[int], analysis_id: Optional[int],
                 signal_id: Optional[int], symbol: str, headline: str, filed_at: Optional[datetime],
                 filing_text: str, pdf_url: Optional[str]) -> bool:
        """Queue one shadow call. Returns False when skipped (disabled or
        at capacity). Must be called from the event loop."""
        if not self.enabled():
            return False
        if self._pending >= MAX_PENDING:
            self.skipped += 1
            log.info("shadow.skipped_at_capacity", pending=self._pending, skipped=self.skipped)
            return False
        self._pending += 1
        task = asyncio.get_running_loop().create_task(self._score(
            announcement_id=announcement_id, analysis_id=analysis_id, signal_id=signal_id,
            symbol=symbol, headline=headline, filed_at=filed_at, filing_text=filing_text,
            pdf_url=pdf_url))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def drain(self) -> None:
        """Wait for in-flight shadow calls (tests, shutdown)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _score(self, **kw: Any) -> None:
        if self._sem is None:
            self._sem = asyncio.Semaphore(MAX_CONCURRENT)
        try:
            async with self._sem:
                row = await self._call(**kw)
            await asyncio.get_running_loop().run_in_executor(None, self._store, row)
        except Exception:  # noqa: BLE001 — shadow must never surface into the analyzer
            log.exception("shadow.crashed")
        finally:
            self._pending -= 1

    async def _call(self, *, announcement_id, analysis_id, signal_id, symbol, headline,
                    filed_at, filing_text, pdf_url) -> dict[str, Any]:
        s = get_settings()
        model = str(getattr(s, "LLM_SLM_MODEL", "") or "tradebot-slm-v1")
        base = {"announcement_id": announcement_id, "analysis_id": analysis_id,
                "signal_id": signal_id, "model": model}
        client = self._get_client(str(s.LLM_SLM_ENDPOINT).strip(), str(getattr(s, "LLM_SLM_API_KEY", "") or ""))
        system, user = build_prompt(symbol=symbol, filed_at=str(filed_at or ""), headline=headline,
                                    filing_text=filing_text, **market_context_for(symbol, filed_at))
        t0 = time.perf_counter()
        try:
            res = await asyncio.wait_for(
                client.complete(system=system, user=user, model=model, temperature=0.0,
                                max_tokens=int(getattr(s, "LLM_MAX_TOKENS", 300) or 300) + 200,
                                reasoning_effort=None, thinking=True, stream=False),
                timeout=float(getattr(s, "LLM_SHADOW_TIMEOUT_SECONDS", 90.0)))
        except asyncio.TimeoutError:
            return {**base, "status": "timeout", "latency_ms": (time.perf_counter() - t0) * 1000}
        except Exception as e:  # noqa: BLE001
            return {**base, "status": "error", "error": str(e)[:500],
                    "latency_ms": (time.perf_counter() - t0) * 1000}
        latency = (time.perf_counter() - t0) * 1000
        try:
            mapped, raw = to_analysis(res.content, headline=headline, pdf_url=pdf_url)
        except Exception as e:  # noqa: BLE001
            return {**base, "status": "error", "error": f"invalid_json: {e}"[:500],
                    "raw": {"content": (res.content or "")[:2000]}, "latency_ms": latency}
        return {
            **base, "status": "ok", "latency_ms": latency, "raw": raw,
            "mover": bool(raw.get("mover")),
            "direction": str(raw.get("direction") or "FLAT").upper()[:8],
            "confidence": mapped.get("confidence"),
            "sentiment_score": mapped.get("sentiment_score"),
            "recommendation": mapped.get("recommendation"),
        }

    def _store(self, row: dict[str, Any]) -> None:
        from app.db.models import ShadowAnalysis

        factory = self._session_factory or _default_session_factory()
        with factory() as db:
            db.add(ShadowAnalysis(**row))
            db.commit()


shadow_scorer = ShadowScorer()
