"""Dataset builder — turns every signal AND every analyzed filing into
an ML training row.

The OutcomeLogger already records coarse outcomes (price at signal,
+5m, +30m). This service goes further: once a signal's reaction window
has fully elapsed (DATASET_HORIZON_MINUTES, default 15), it fetches
1-MINUTE Fyers candles around the signal time and computes the full
price-trajectory vector:

  FEATURES (knowable at trade time / T+1..T+5 — safe model inputs):
    pre-news drift + volume baseline, price/return at T+1..T+5,
    5-minute high/low, MFE/MAE within 5m, time-to-first-1%-spike
    (within 5m), 1m→3m slope, volume surge, VWAP deviation,
    opening-range breakout, volatility-expansion ratio.

  TARGETS (only knowable at T+horizon — training labels ONLY; using
  them as inputs is look-ahead bias):
    close/high/low at the horizon, MFE/MAE over the horizon,
    time-to-peak, retrace-from-peak, time-to-1%-within-horizon,
    and the categorical UP/DOWN/FLAT label.

Plus CONTEXT features on every row: IST minute-since-open and
day-of-week, the concurrent NIFTY 1m/5m move (separates "the stock
moved on news" from "everything moved"), and the stock's 5m return in
excess of NIFTY.

TWO ROW SOURCES share the table:
  signal rows — one per signal_outcomes row (the bot acted or was
    risk-blocked).
  shadow rows — analyzed announcements that never produced a signal
    (noise-filtered, HOLD fallback, stale). These are the NEGATIVE
    examples: a materiality pre-filter model can only learn what NOT
    to trade if the rejected universe is in the dataset. Anchored at
    the analysis timestamp (when the bot COULD have acted).

Everything lands in `dataset_features.features` (JSON) so adding a
feature never needs a schema migration. `/api/dataset` joins this with
announcements + analyses + signals + outcomes + trades into the
exportable training set.

Design constraints (mirrors the OutcomeLogger):
  - PASSIVE: never influences trading; every failure is swallowed and
    recorded in the row's status/note.
  - Fyers-only pricing: candles come through the same fetch_history
    helper the ATR provider uses; no public-feed fallback. No candles
    (Fyers disconnected, symbol unresolvable) → status "no_candles",
    retried up to DATASET_MAX_ATTEMPTS. History comes from the REST
    /data/history endpoint — the WebSocket only carries live ticks.
  - A signal near market close can never get a full horizon window —
    those rows stay "partial" (early features present, targets None).

Backfill efficiency (how "fill the whole history" stays cheap):
  - One history fetch per SYMBOL per batch: fetch_history returns a
    date RANGE, so a single call covers every pending row for that
    symbol in the window — the per-(symbol,day) grouping trick, done
    at the range level. Rows are processed symbol-sorted so the cache
    hits cluster; the cache clears per batch to bound memory.
  - Filings outside the IST session (evenings, weekends — a huge slice
    of the shadow universe) can never have a reaction window. They are
    marked "after_hours" TERMINALLY, before any API call, when
    ENFORCE_MARKET_HOURS is on.
  - Cache-miss fetches are paced by DATASET_FETCH_DELAY_SECONDS to
    stay far inside Fyers' rate limits.
  - `run_full()` loops batches until nothing is pending, exposing
    live progress for POST /api/dataset/backfill?full=true.
"""
from __future__ import annotations

import asyncio
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.logging_config import get_logger

log = get_logger(__name__)

# How many minutes of PRE-news candles the features need (volume
# baseline, pre-news volatility, pre-news drift).
PRE_WINDOW_MINUTES = 10

# How far back before the signal the BASELINE may come from. Illiquid
# BSE small-caps (SME / trade-to-trade segments) often have no candle
# in the exact signal minute — the last trade up to an hour earlier is
# still the true "price at signal" for such names, and it is strictly
# pre-signal so there is no look-ahead.
BASELINE_LOOKBACK_MINUTES = 60

# Statuses the periodic loop retries (until DATASET_MAX_ATTEMPTS).
RETRYABLE_STATUSES = ("no_candles", "partial", "error")

# Feature-schema version. Bump when compute_reaction_features changes its
# emitted keys so a `rebuild` backfill can find and re-enrich rows that
# were computed by an older schema (e.g. before the full minute-by-minute
# trajectory landed). Stamped into every features blob as
# "features_version".
#   1 = initial (price_1m..5m, price_15m, aggregates)
#   2 = full minute-by-minute trajectory price_1m..price_{horizon}m
FEATURES_VERSION = 2

# Candle tuple layout from Fyers: [epoch_s, open, high, low, close, volume]
_TS, _OPEN, _HIGH, _LOW, _CLOSE, _VOL = range(6)


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _round(v: Optional[float], nd: int = 4) -> Optional[float]:
    return round(v, nd) if v is not None else None


# =========================================================================
# Pure feature computation (unit-testable, no I/O)
# =========================================================================


def compute_reaction_features(
    candles: Sequence[Sequence[float]],
    signal_epoch: int,
    *,
    direction: int = 1,
    baseline_price: Optional[float] = None,
    horizon_minutes: int = 15,
    flat_threshold_pct: float = 0.3,
    spike_threshold_pct: float = 1.0,
    session_close_epoch: Optional[int] = None,
) -> tuple[Optional[dict[str, Any]], str]:
    """Compute the reaction feature/target vector from 1-minute candles.

    `candles` is the raw Fyers list [[epoch_s, o, h, l, c, v], ...]
    (any order, any span — this function aligns them to the signal
    minute). `direction` is +1 for BUY, -1 for SELL: favorable/adverse
    excursions (MFE/MAE) and the spike-time features are measured in
    the trade's direction; raw prices/returns stay direction-agnostic.

    SPARSE symbols (BSE SME / trade-to-trade names that trade a few
    times an hour) use AS-OF pricing: "price at T+k" is the close of
    the last candle at-or-before minute k — the true last-traded price,
    and strictly look-ahead-free. A checkpoint is only filled as far as
    the session provably reached: up to the symbol's own last trade, or
    up to `session_close_epoch` when the caller supplies it.

    Returns (features_dict | None, status) where status is one of
    "complete" (a horizon price could be established), "partial" (early
    trades but the horizon lies beyond the observable session) or
    "no_candles" (no baseline or no post-signal trade at all).
    """
    m0 = signal_epoch - (signal_epoch % 60)  # the signal's minute bucket
    by_minute: dict[int, Sequence[float]] = {}
    for c in candles or ():
        if not isinstance(c, (list, tuple)) or len(c) < 6:
            continue
        try:
            offset = (int(c[_TS]) - m0) // 60
        except (TypeError, ValueError):
            continue
        if -BASELINE_LOOKBACK_MINUTES <= offset <= horizon_minutes:
            by_minute[offset] = c

    sorted_offsets = sorted(by_minute)
    post_offsets = [k for k in sorted_offsets if k >= 0]

    def _asof_close(k: int) -> Optional[float]:
        """Close of the last candle at-or-before minute k (never after)."""
        best: Optional[Sequence[float]] = None
        for off in sorted_offsets:
            if off > k:
                break
            best = by_minute[off]
        return float(best[_CLOSE]) if best is not None else None

    c0 = by_minute.get(0)
    baseline = (
        float(baseline_price)
        if baseline_price
        else (float(c0[_OPEN]) if c0 is not None else _asof_close(-1))
    )
    if not baseline or baseline <= 0 or not post_offsets:
        return None, "no_candles"
    direction = -1 if direction < 0 else 1

    # As-of fills are only trustworthy while the session provably ran:
    # the symbol's own last trade proves it, and the session close (when
    # supplied) extends it — a signal near the close must NOT forward-
    # fill past the bell.
    max_fill = post_offsets[-1]
    if session_close_epoch is not None:
        max_fill = max(
            max_fill,
            min(horizon_minutes, (int(session_close_epoch) - m0) // 60),
        )

    def _px(offset: int) -> Optional[float]:
        c = by_minute.get(offset)
        if c is not None:
            return float(c[_CLOSE])
        return _asof_close(offset) if offset <= max_fill else None

    def _ret(price: Optional[float]) -> Optional[float]:
        return (price - baseline) / baseline * 100.0 if price is not None else None

    feats: dict[str, Any] = {
        "features_version": FEATURES_VERSION,
        "horizon_minutes": horizon_minutes,
        "direction": direction,
        "baseline_price": _round(baseline),
        "candles_pre": sum(1 for k in by_minute if -PRE_WINDOW_MINUTES <= k < 0),
        "candles_post": len(post_offsets),
    }

    # ---- pre-news context (features) ----------------------------------
    # Volume/volatility baselines stay within the tight PRE_WINDOW so a
    # dense and a sparse symbol are measured over the same clock window
    # (the deeper BASELINE_LOOKBACK candles only anchor prices).
    pre_offsets = [k for k in sorted_offsets if -PRE_WINDOW_MINUTES <= k < 0]
    pre_vols = [float(by_minute[k][_VOL]) for k in pre_offsets[-5:]]
    feats["pre_volume_avg_5m"] = _round(
        sum(pre_vols) / len(pre_vols) if pre_vols else None, 2
    )
    pre5 = _asof_close(-5)
    feats["pre_move_5m_pct"] = _round(
        (baseline - pre5) / pre5 * 100.0 if pre5 and pre5 > 0 else None
    )

    # ---- full minute-by-minute trajectory T+1..T+horizon ---------------
    # price_{k}m / ret_{k}m for EVERY minute so a model can learn the
    # SHAPE of the move, not just its endpoints. Minutes 1..5 are
    # decision-time FEATURES; 6..horizon are look-ahead TARGETS (see the
    # role split in the /api/dataset column catalog). As-of pricing means
    # a sparse symbol still gets a value at every minute up to max_fill.
    for k in range(1, horizon_minutes + 1):
        px = _px(k)
        feats[f"price_{k}m"] = _round(px)
        feats[f"ret_{k}m_pct"] = _round(_ret(px))

    def _window_high_low(upto: int) -> tuple[Optional[float], Optional[float]]:
        highs = [float(by_minute[k][_HIGH]) for k in range(0, upto + 1) if k in by_minute]
        lows = [float(by_minute[k][_LOW]) for k in range(0, upto + 1) if k in by_minute]
        return (max(highs) if highs else None, min(lows) if lows else None)

    def _excursions(upto: int) -> tuple[Optional[float], Optional[float]]:
        """(MFE, MAE) % in the trade's direction over minutes 0..upto."""
        hi, lo = _window_high_low(upto)
        if hi is None or lo is None:
            return None, None
        up = (hi - baseline) / baseline * 100.0
        down = (lo - baseline) / baseline * 100.0
        if direction == 1:
            return up, down
        return -down, -up

    hi5, lo5 = _window_high_low(5)
    feats["high_5m"] = _round(hi5)
    feats["low_5m"] = _round(lo5)
    mfe5, mae5 = _excursions(5)
    feats["mfe_5m_pct"] = _round(mfe5)
    feats["mae_5m_pct"] = _round(mae5)

    def _time_to_spike(upto: int) -> Optional[int]:
        """Seconds (upper bound, 1-min granularity) until the favorable
        excursion first reaches spike_threshold_pct. None if never."""
        for k in range(0, upto + 1):
            c = by_minute.get(k)
            if c is None:
                continue
            if direction == 1:
                move = (float(c[_HIGH]) - baseline) / baseline * 100.0
            else:
                move = (baseline - float(c[_LOW])) / baseline * 100.0
            if move >= spike_threshold_pct:
                return (k + 1) * 60
        return None

    feats["time_to_1pct_5m_s"] = _time_to_spike(5)
    feats["hit_1pct_within_5m"] = feats["time_to_1pct_5m_s"] is not None

    r1, r3 = feats["ret_1m_pct"], feats["ret_3m_pct"]
    feats["slope_1m_3m_pct_per_min"] = _round(
        (r3 - r1) / 2.0 if r1 is not None and r3 is not None else None
    )

    # Volume surge: the signal minute's volume vs the pre-news baseline.
    # No as-of here — a missing minute-0 candle means zero traded volume
    # is knowable, not that an older bar's volume applies.
    v0 = float(c0[_VOL]) if c0 is not None else None
    feats["volume_1m"] = _round(v0, 2)
    pre_avg = feats["pre_volume_avg_5m"]
    feats["volume_surge_1m"] = _round(
        v0 / pre_avg if v0 is not None and pre_avg else None, 2
    )

    # VWAP deviation over the first 5 minutes (typical price × volume).
    pv = vol_sum = 0.0
    for k in range(0, 6):
        c = by_minute.get(k)
        if c is None:
            continue
        typical = (float(c[_HIGH]) + float(c[_LOW]) + float(c[_CLOSE])) / 3.0
        pv += typical * float(c[_VOL])
        vol_sum += float(c[_VOL])
    p5 = feats["price_5m"]
    feats["vwap_dev_5m_pct"] = _round(
        (p5 - pv / vol_sum) / (pv / vol_sum) * 100.0
        if vol_sum > 0 and p5 is not None
        else None
    )

    # Opening-range breakout: did minutes 1-2 break the signal-minute
    # extreme in the trade's direction? Needs the actual minute-0 bar.
    c1, c2 = by_minute.get(1), by_minute.get(2)
    if c0 is None or (c1 is None and c2 is None):
        feats["range_breakout_2m"] = None
    elif direction == 1:
        or_high = float(c0[_HIGH])
        feats["range_breakout_2m"] = any(
            c is not None and float(c[_HIGH]) > or_high for c in (c1, c2)
        )
    else:
        or_low = float(c0[_LOW])
        feats["range_breakout_2m"] = any(
            c is not None and float(c[_LOW]) < or_low for c in (c1, c2)
        )

    # Volatility expansion: stdev of 1m close-to-close returns in the
    # first 3 post-news minutes vs the pre-news window.
    def _minute_returns(offsets: list[int]) -> list[float]:
        rets = []
        for a, b in zip(offsets, offsets[1:]):
            ca, cb = by_minute.get(a), by_minute.get(b)
            if ca is None or cb is None or float(ca[_CLOSE]) <= 0:
                continue
            rets.append((float(cb[_CLOSE]) - float(ca[_CLOSE])) / float(ca[_CLOSE]) * 100.0)
        return rets

    post_rets = _minute_returns([0, 1, 2, 3])
    pre_rets = _minute_returns(pre_offsets)
    if len(post_rets) >= 2 and len(pre_rets) >= 3:
        pre_sd = statistics.pstdev(pre_rets)
        feats["vol_expansion_ratio"] = _round(
            statistics.pstdev(post_rets) / pre_sd if pre_sd > 1e-9 else None, 2
        )
    else:
        feats["vol_expansion_ratio"] = None

    # ---- horizon targets (labels ONLY — look-ahead beyond T+5) ---------
    ph = _px(horizon_minutes)
    feats["price_15m"] = _round(ph)
    ret_h = _ret(ph)
    feats["ret_15m_pct"] = _round(ret_h)
    hi_h, lo_h = _window_high_low(horizon_minutes)
    feats["high_15m"] = _round(hi_h)
    feats["low_15m"] = _round(lo_h)
    mfe_h, mae_h = _excursions(horizon_minutes)
    feats["mfe_15m_pct"] = _round(mfe_h)
    feats["mae_15m_pct"] = _round(mae_h)
    feats["time_to_1pct_15m_s"] = _time_to_spike(horizon_minutes)

    # Minute of the favorable extreme + how much of the peak was given
    # back by the horizon close (the "did I need a trailing stop" label).
    peak_minute: Optional[int] = None
    peak_val: Optional[float] = None
    for k in range(0, horizon_minutes + 1):
        c = by_minute.get(k)
        if c is None:
            continue
        val = float(c[_HIGH]) if direction == 1 else -float(c[_LOW])
        if peak_val is None or val > peak_val:
            peak_val, peak_minute = val, k
    feats["time_to_peak_min"] = peak_minute
    feats["retrace_from_peak_pct"] = _round(
        mfe_h - ret_h * direction if mfe_h is not None and ret_h is not None else None
    )

    if ret_h is None:
        feats["label_15m"] = None
    elif ret_h > flat_threshold_pct:
        feats["label_15m"] = "UP"
    elif ret_h < -flat_threshold_pct:
        feats["label_15m"] = "DOWN"
    else:
        feats["label_15m"] = "FLAT"

    # Day-high targets: the peak of the rest of the trading day (same
    # IST calendar day, from the signal minute to the close) — the
    # "how high did it ultimately go" label beyond the 15m horizon.
    ist_shift = 19800  # +05:30
    ist_day = (signal_epoch + ist_shift) // 86400
    day_highs = [
        float(c[_HIGH])
        for c in candles or ()
        if isinstance(c, (list, tuple))
        and len(c) >= 6
        and int(c[_TS]) >= m0
        and (int(c[_TS]) + ist_shift) // 86400 == ist_day
    ]
    day_high = max(day_highs) if day_highs else None
    feats["day_high"] = _round(day_high)
    feats["ret_day_high_pct"] = _round(
        (day_high - baseline) / baseline * 100.0 if day_high is not None else None
    )

    status = "complete" if ph is not None else "partial"
    return feats, status


def compute_time_context(anchor_utc: datetime) -> dict[str, Any]:
    """Session-clock features from the (naive-UTC) anchor time. A 9:31
    order win trades nothing like a 14:50 one — the model needs to see
    the clock."""
    ist = anchor_utc + timedelta(hours=5, minutes=30)
    return {
        "minute_since_open": ist.hour * 60 + ist.minute - (9 * 60 + 15),
        "day_of_week": ist.weekday(),  # 0 = Monday
    }


def compute_index_context(
    index_candles: Sequence[Sequence[float]], signal_epoch: int
) -> dict[str, Any]:
    """Concurrent NIFTY move over the same reaction window (market
    regime: did the whole tape move, or just this stock?)."""
    m0 = signal_epoch - (signal_epoch % 60)
    by_minute: dict[int, Sequence[float]] = {}
    for c in index_candles or ():
        if not isinstance(c, (list, tuple)) or len(c) < 6:
            continue
        try:
            offset = (int(c[_TS]) - m0) // 60
        except (TypeError, ValueError):
            continue
        if 0 <= offset <= 5:
            by_minute[offset] = c
    out: dict[str, Any] = {"nifty_ret_1m_pct": None, "nifty_ret_5m_pct": None}
    c0 = by_minute.get(0)
    if c0 is None or float(c0[_OPEN]) <= 0:
        return out
    base = float(c0[_OPEN])
    for k, key in ((1, "nifty_ret_1m_pct"), (5, "nifty_ret_5m_pct")):
        c = by_minute.get(k)
        if c is not None:
            out[key] = _round((float(c[_CLOSE]) - base) / base * 100.0)
    return out


# =========================================================================
# Default candle source (Fyers-only, like everything else)
# =========================================================================


async def _default_candle_fn(symbol: str, signal_time: datetime) -> list[Any]:
    """1-minute candles covering the signal's day via the Fyers history
    endpoint. Returns [] when Fyers isn't connected / symbol unknown."""
    try:
        from app.api.market import fetch_history
        from app.execution.symbols import resolve_fyers_symbol

        full = resolve_fyers_symbol(symbol) or symbol
        days = (_utcnow_naive().date() - signal_time.date()).days + 2
        return await fetch_history(full, resolution="1", days=max(1, min(days, 35)))
    except Exception:  # noqa: BLE001 — telemetry never raises
        return []


async def _default_index_fn(signal_time: datetime) -> list[Any]:
    """1-minute NIFTY 50 candles for the concurrent-market-move context.
    Same fail-safe contract as the stock candles."""
    try:
        from app.api.market import fetch_history

        days = (_utcnow_naive().date() - signal_time.date()).days + 2
        return await fetch_history(
            "NSE:NIFTY50-INDEX", resolution="1", days=max(1, min(days, 35))
        )
    except Exception:  # noqa: BLE001
        return []


def _default_session_factory() -> Callable[[], Session]:
    from app.db.session import SessionLocal
    return SessionLocal


def _session_close_epoch(anchor_utc: datetime, settings: Any) -> int:
    """Epoch second of the IST market close on the anchor's trading day
    — the bound for as-of price fills on sparse symbols."""
    try:
        hh, mm = str(getattr(settings, "MARKET_CLOSE_IST", "15:30")).split(":")
        close_h, close_m = int(hh), int(mm)
    except (ValueError, AttributeError):
        close_h, close_m = 15, 30
    ist = anchor_utc + timedelta(hours=5, minutes=30)
    close_ist = ist.replace(hour=close_h, minute=close_m, second=0, microsecond=0)
    close_utc = close_ist - timedelta(hours=5, minutes=30)
    return int(close_utc.replace(tzinfo=timezone.utc).timestamp())


# =========================================================================
# The background service
# =========================================================================


class DatasetBuilder:
    """Passive signal_outcomes → dataset_features enricher.

    Runs a batch every DATASET_ENRICH_INTERVAL_SECONDS; `run_once` is
    also callable directly (the POST /api/dataset/backfill path and
    tests). Lifecycle mirrors the OutcomeLogger.
    """

    def __init__(
        self,
        *,
        session_factory: Optional[Callable[[], Session]] = None,
        candle_fn: Optional[Callable[[str, datetime], Awaitable[list[Any]]]] = None,
        index_fn: Optional[Callable[[datetime], Awaitable[list[Any]]]] = None,
        market_hours_check: Optional[bool] = None,
    ) -> None:
        self._session_factory = session_factory or _default_session_factory()
        self._candle_fn = candle_fn
        self._index_fn = index_fn
        # None → follow ENFORCE_MARKET_HOURS (off in paper testing so
        # synthetic any-hour test signals still enrich).
        self._market_hours_check = market_hours_check
        # Per-batch caches: one NIFTY fetch per trading day touched, and
        # one stock-history fetch per symbol (the range call covers every
        # pending row for that symbol). Cleared per batch to bound memory.
        self._index_cache: dict[Any, list[Any]] = {}
        self._candle_cache: dict[str, tuple[int, list[Any]]] = {}
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        self._run_lock = asyncio.Lock()
        # PRIVATE executor for the builder's per-row DB work. A full
        # backfill queues hundreds of executor jobs per batch — on the
        # shared default executor that queue starves every other
        # run_in_executor user (the status endpoint, sync API handlers)
        # and the whole HTTP surface appears to hang.
        self._executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="dataset-builder"
        )
        # When True, pickers also select already-enriched rows whose
        # features blob predates FEATURES_VERSION — so a schema bump can
        # be rolled out over the whole history. Set only for the duration
        # of a `rebuild` backfill (the periodic loop never rebuilds).
        self._include_stale = False
        # Full-history backfill (run_full) task + live progress snapshot.
        self._full_task: Optional[asyncio.Task[None]] = None
        self.backfill_progress: dict[str, Any] = {"running": False}
        # (monotonic_ts, counts) cache for count_pending — status polls
        # must never turn into a query-per-poll load.
        self._pending_cache: Optional[tuple[float, dict[str, int]]] = None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> asyncio.Task[None]:
        if self._task is not None and not self._task.done():
            return self._task
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="dataset-builder")
        return self._task

    def stop(self) -> None:
        self._stop_event.set()
        if self._full_task is not None and not self._full_task.done():
            self._full_task.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def wait_until_stopped(self) -> None:
        if self._task is None:
            return
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def _run(self) -> None:
        log.info("dataset_builder.start")
        try:
            while not self._stop_event.is_set():
                settings = get_settings()
                interval = max(
                    10.0, float(getattr(settings, "DATASET_ENRICH_INTERVAL_SECONDS", 120.0))
                )
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                    return
                except asyncio.TimeoutError:
                    pass
                if not bool(getattr(settings, "DATASET_BUILDER_ENABLED", True)):
                    continue
                try:
                    await self.run_once()
                except Exception:  # noqa: BLE001
                    log.exception("dataset_builder.batch_failed")
        finally:
            log.info("dataset_builder.stop")

    # -- one enrichment batch ----------------------------------------------

    async def run_once(self, limit: int = 25) -> dict[str, int]:
        """Enrich up to `limit` pending outcomes. Serialised by a lock so
        the periodic loop and a manual backfill never double-process."""
        async with self._run_lock:
            return await self._run_batch(limit)

    # -- full-history backfill ----------------------------------------------

    def start_full_backfill(
        self, batch: int = 200, *, rebuild: bool = False
    ) -> dict[str, Any]:
        """Kick off (or report) the run-until-done backfill task. Returns
        the live progress snapshot. Idempotent while running.

        rebuild=True also re-enriches already-complete rows whose feature
        schema predates FEATURES_VERSION — used to roll a new feature set
        (e.g. the full minute-by-minute trajectory) over the whole
        history."""
        if self._full_task is None or self._full_task.done():
            self._include_stale = bool(rebuild)
            self.backfill_progress = {
                "running": True,
                "rebuild": bool(rebuild),
                "batches": 0,
                "processed": 0, "shadow": 0, "complete": 0, "partial": 0,
                "no_candles": 0, "after_hours": 0, "too_old": 0, "errors": 0,
                "started_at": _utcnow_naive().isoformat(),
                "finished_at": None,
            }
            self._full_task = asyncio.create_task(
                self.run_full(batch), name="dataset-full-backfill"
            )
        return dict(self.backfill_progress)

    async def run_full(self, batch: int = 200) -> dict[str, Any]:
        """Loop batches until nothing is pending. Rate limiting happens
        per candle fetch (DATASET_FETCH_DELAY_SECONDS), so no extra
        inter-batch sleep is needed."""
        progress = self.backfill_progress
        if not progress.get("running"):
            # run_full called directly (tests) — seed a snapshot.
            progress = self.backfill_progress = {
                "running": True, "batches": 0, "processed": 0, "shadow": 0,
                "complete": 0, "partial": 0, "no_candles": 0,
                "after_hours": 0, "too_old": 0, "errors": 0,
                "started_at": _utcnow_naive().isoformat(), "finished_at": None,
            }
        # One pending count up front: the status endpoint derives the
        # live "remaining" from this snapshot minus progress instead of
        # re-counting the DB on every poll.
        try:
            snap = await asyncio.get_running_loop().run_in_executor(
                self._executor, self.count_pending
            )
            progress["remaining_start"] = snap
        except Exception:  # noqa: BLE001
            progress["remaining_start"] = None
        try:
            while not self._stop_event.is_set():
                counts = await self.run_once(limit=batch)
                if counts["processed"] == 0:
                    break
                progress["batches"] += 1
                for k, v in counts.items():
                    progress[k] = progress.get(k, 0) + v
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("dataset_builder.full_backfill_failed")
        finally:
            progress["running"] = False
            progress["finished_at"] = _utcnow_naive().isoformat()
            self._include_stale = False  # rebuild is a one-shot
            log.info("dataset_builder.full_backfill_done", **{
                k: v for k, v in progress.items() if isinstance(v, int)
            })
        return dict(progress)

    def remaining_estimate(self) -> Optional[dict[str, int]]:
        """Cheap remaining estimate while the full backfill runs: the
        start snapshot minus what progress says was handled. No DB hit."""
        progress = self.backfill_progress
        snap = progress.get("remaining_start")
        if not isinstance(snap, dict):
            return None
        shadow_done = int(progress.get("shadow", 0) or 0)
        signal_done = int(progress.get("processed", 0) or 0) - shadow_done
        return {
            "signal": max(0, int(snap.get("signal", 0)) - signal_done),
            "shadow": max(0, int(snap.get("shadow", 0)) - shadow_done),
        }

    def count_pending_cached(self, ttl: float = 30.0) -> dict[str, int]:
        """count_pending behind a small TTL cache — status polls must
        never become a query-per-poll load. Sync; run in an executor."""
        now = time.monotonic()
        if self._pending_cache is not None and now - self._pending_cache[0] < ttl:
            return self._pending_cache[1]
        counts = self.count_pending()
        self._pending_cache = (time.monotonic(), counts)
        return counts

    def count_pending(self) -> dict[str, int]:
        """How many rows still await enrichment (for the status API).
        Sync — call via an executor from async contexts."""
        from app.db.models import (
            Analysis,
            Announcement,
            DatasetFeature,
            Signal,
            SignalOutcome,
        )

        settings = get_settings()
        horizon = int(getattr(settings, "DATASET_HORIZON_MINUTES", 15))
        max_attempts = int(getattr(settings, "DATASET_MAX_ATTEMPTS", 3))
        cutoff = _utcnow_naive() - timedelta(minutes=horizon + 1)
        retryable = self._eligible(DatasetFeature, max_attempts)
        with self._session_factory() as session:
            signal_n = session.execute(
                select(func.count(SignalOutcome.id))
                .select_from(SignalOutcome)
                .join(
                    DatasetFeature,
                    DatasetFeature.outcome_id == SignalOutcome.id,
                    isouter=True,
                )
                .where(SignalOutcome.created_at <= cutoff)
                .where(retryable)
            ).scalar_one()
            outcome_exists = (
                select(SignalOutcome.id)
                .join(Signal, SignalOutcome.signal_id == Signal.id)
                .where(Signal.analysis_id == Analysis.id)
            ).exists()
            shadow_n = session.execute(
                select(func.count(func.distinct(Announcement.id)))
                .select_from(Announcement)
                .join(Analysis, Analysis.announcement_id == Announcement.id)
                .join(
                    DatasetFeature,
                    DatasetFeature.announcement_id == Announcement.id,
                    isouter=True,
                )
                .where(Analysis.created_at <= cutoff)
                .where(retryable)
                .where(~outcome_exists)
            ).scalar_one()
        return {"signal": int(signal_n or 0), "shadow": int(shadow_n or 0)}

    async def _run_batch(self, limit: int) -> dict[str, int]:
        settings = get_settings()
        horizon = int(getattr(settings, "DATASET_HORIZON_MINUTES", 15))
        max_attempts = int(getattr(settings, "DATASET_MAX_ATTEMPTS", 3))
        max_age_days = int(getattr(settings, "DATASET_MAX_AGE_DAYS", 30))
        loop = asyncio.get_running_loop()
        counts = {
            "processed": 0, "shadow": 0, "complete": 0, "partial": 0,
            "no_candles": 0, "after_hours": 0, "too_old": 0, "errors": 0,
        }
        self._index_cache.clear()
        self._candle_cache.clear()
        check_hours = (
            self._market_hours_check
            if self._market_hours_check is not None
            else bool(getattr(settings, "ENFORCE_MARKET_HOURS", True))
        )

        cutoff = _utcnow_naive() - timedelta(minutes=horizon + 1)
        # Signal rows first (the bot acted on these — highest value),
        # shadow rows (analyzed-but-never-signaled filings) fill the
        # rest of the batch.
        pending = await loop.run_in_executor(
            self._executor, self._pick_pending, cutoff, limit, max_attempts
        )
        work: list[tuple[str, int, Optional[int], str, str, Any, Optional[float]]] = [
            ("signal", outcome_id, signal_id, symbol or "", action or "", created_at, baseline)
            for outcome_id, signal_id, symbol, action, created_at, baseline in pending
        ]
        if len(work) < limit:
            shadow = await loop.run_in_executor(
                self._executor,
                self._pick_pending_shadow, cutoff, limit - len(work), max_attempts,
            )
            work.extend(
                ("shadow", ann_id, None, symbol or "", reco or "", created_at, None)
                for ann_id, symbol, reco, created_at in shadow
            )

        # Symbol-sorted processing clusters the per-symbol candle-cache
        # hits (which rows were SELECTED was decided above — signal rows
        # first — so this only changes processing order).
        work.sort(key=lambda w: w[3])

        from app.risk.market_clock import is_market_open

        for kind, key_id, signal_id, symbol, action, created_at, baseline in work:
            counts["processed"] += 1
            if kind == "shadow":
                counts["shadow"] += 1
            try:
                if created_at is None or not symbol:
                    status, feats, note = "error", None, "missing_signal_time"
                elif (_utcnow_naive() - created_at).days > max_age_days:
                    status, feats, note = "too_old", None, "beyond_candle_history"
                elif check_hours and not is_market_open(created_at):
                    # Evening/weekend filing: no reaction window can ever
                    # exist. Terminal — and decided WITHOUT an API call.
                    status, feats, note = "after_hours", None, "outside_market_hours"
                else:
                    candles = await self._get_candles(symbol, created_at, settings)
                    signal_epoch = int(
                        created_at.replace(tzinfo=timezone.utc).timestamp()
                    )
                    feats, status = compute_reaction_features(
                        candles,
                        signal_epoch,
                        direction=-1 if action.upper() == "SELL" else 1,
                        baseline_price=baseline,
                        horizon_minutes=horizon,
                        flat_threshold_pct=float(
                            getattr(settings, "DATASET_FLAT_THRESHOLD_PCT", 0.3)
                        ),
                        spike_threshold_pct=float(
                            getattr(settings, "DATASET_SPIKE_THRESHOLD_PCT", 1.0)
                        ),
                        session_close_epoch=_session_close_epoch(
                            created_at, settings
                        ),
                    )
                    if feats is not None:
                        feats.update(compute_time_context(created_at))
                        idx = compute_index_context(
                            await self._index_candles(created_at), signal_epoch
                        )
                        feats.update(idx)
                        r5, n5 = feats.get("ret_5m_pct"), idx.get("nifty_ret_5m_pct")
                        feats["excess_ret_5m_pct"] = _round(
                            r5 - n5 if r5 is not None and n5 is not None else None
                        )
                    note = None
            except Exception as e:  # noqa: BLE001 — one bad row never stops the batch
                log.warning(
                    "dataset_builder.row_failed", kind=kind, key_id=key_id, error=str(e)
                )
                status, feats, note = "error", None, str(e)[:120]

            key = "errors" if status == "error" else status
            counts[key] = counts.get(key, 0) + 1
            await loop.run_in_executor(
                self._executor, self._upsert, kind, key_id, signal_id, symbol,
                created_at, status, feats, note, max_attempts,
            )

        if counts["processed"]:
            log.info("dataset_builder.batch", **counts)
        return counts

    async def _index_candles(self, signal_time: datetime) -> list[Any]:
        """NIFTY 1-min candles, one fetch per trading day per batch."""
        day = signal_time.date()
        if day not in self._index_cache:
            fn = self._index_fn or _default_index_fn
            try:
                self._index_cache[day] = await fn(signal_time) or []
            except Exception:  # noqa: BLE001
                self._index_cache[day] = []
        return self._index_cache[day]

    async def _get_candles(
        self, symbol: str, anchor: datetime, settings: Any
    ) -> list[Any]:
        """Stock 1-min candles via the per-batch symbol cache. The range
        fetch covers [anchor..today], so one call serves every pending
        row for the symbol whose anchor is not older than the cached
        range. Cache-miss fetches are paced by
        DATASET_FETCH_DELAY_SECONDS to respect Fyers rate limits."""
        key = symbol.upper()
        days_needed = (_utcnow_naive().date() - anchor.date()).days + 2
        cached = self._candle_cache.get(key)
        if cached is not None and cached[0] >= days_needed:
            return cached[1]
        delay = float(getattr(settings, "DATASET_FETCH_DELAY_SECONDS", 0.25))
        if delay > 0:
            await asyncio.sleep(delay)
        candle_fn = self._candle_fn or _default_candle_fn
        candles = await candle_fn(symbol, anchor) or []
        self._candle_cache[key] = (days_needed, candles)
        return candles

    # -- sync DB helpers (run in executor) ---------------------------------

    def _eligible(self, DF: Any, max_attempts: int) -> Any:
        """SQLAlchemy predicate for 'this row needs (re)enrichment': never
        enriched, or a retryable status with attempts left. During a
        `rebuild` run it also matches already-enriched rows whose feature
        schema predates FEATURES_VERSION (old rows carry no version key →
        coalesce to 1)."""
        cond = (DF.id.is_(None)) | (
            DF.status.in_(RETRYABLE_STATUSES) & (DF.attempts < max_attempts)
        )
        if self._include_stale:
            cond = cond | (
                DF.status.in_(("complete", "partial"))
                & (
                    func.coalesce(DF.features["features_version"].as_integer(), 1)
                    < FEATURES_VERSION
                )
            )
        return cond

    def _pick_pending(
        self, cutoff: datetime, limit: int, max_attempts: int
    ) -> list[tuple[Any, ...]]:
        from app.db.models import DatasetFeature, SignalOutcome

        with self._session_factory() as session:
            stmt = (
                select(
                    SignalOutcome.id,
                    SignalOutcome.signal_id,
                    SignalOutcome.symbol,
                    SignalOutcome.action,
                    SignalOutcome.created_at,
                    SignalOutcome.price_at_signal,
                )
                .join(
                    DatasetFeature,
                    DatasetFeature.outcome_id == SignalOutcome.id,
                    isouter=True,
                )
                .where(SignalOutcome.created_at <= cutoff)
                .where(self._eligible(DatasetFeature, max_attempts))
                .order_by(SignalOutcome.id.desc())
                .limit(limit)
            )
            return [tuple(r) for r in session.execute(stmt).all()]

    def _pick_pending_shadow(
        self, cutoff: datetime, limit: int, max_attempts: int
    ) -> list[tuple[Any, ...]]:
        """Analyzed announcements with NO signal outcome — the rejected
        universe (noise-filtered, HOLD, stale). Anchored at the analysis
        timestamp: the moment the bot could have acted."""
        from app.db.models import (
            Analysis,
            Announcement,
            DatasetFeature,
            Signal,
            SignalOutcome,
        )

        with self._session_factory() as session:
            outcome_exists = (
                select(SignalOutcome.id)
                .join(Signal, SignalOutcome.signal_id == Signal.id)
                .where(Signal.analysis_id == Analysis.id)
            ).exists()
            stmt = (
                select(
                    Announcement.id,
                    Announcement.symbol,
                    Analysis.recommendation,
                    Analysis.created_at,
                )
                .join(Analysis, Analysis.announcement_id == Announcement.id)
                .join(
                    DatasetFeature,
                    DatasetFeature.announcement_id == Announcement.id,
                    isouter=True,
                )
                .where(Analysis.created_at <= cutoff)
                .where(self._eligible(DatasetFeature, max_attempts))
                .where(~outcome_exists)
                .order_by(Announcement.id.desc(), Analysis.id.desc())
                .limit(limit * 2)  # headroom for multi-analysis dedupe below
            )
            seen: set[int] = set()
            out: list[tuple[Any, ...]] = []
            for ann_id, symbol, reco, created_at in session.execute(stmt).all():
                if ann_id in seen:
                    continue
                seen.add(ann_id)
                out.append((ann_id, symbol, reco, created_at))
                if len(out) >= limit:
                    break
            return out

    def _upsert(
        self,
        kind: str,
        key_id: int,
        signal_id: Optional[int],
        symbol: str,
        signal_time: Optional[datetime],
        status: str,
        feats: Optional[dict[str, Any]],
        note: Optional[str],
        max_attempts: int,
    ) -> None:
        from app.db.models import DatasetFeature

        with self._session_factory() as session:
            key_col = (
                DatasetFeature.outcome_id
                if kind == "signal"
                else DatasetFeature.announcement_id
            )
            row = session.execute(
                select(DatasetFeature).where(key_col == key_id)
            ).scalars().first()
            if row is None:
                row = DatasetFeature(
                    outcome_id=key_id if kind == "signal" else None,
                    announcement_id=key_id if kind == "shadow" else None,
                    signal_id=signal_id,
                    symbol=symbol or "",
                    signal_time=signal_time,
                )
                session.add(row)
            row.status = status
            row.note = note
            row.attempts = (row.attempts or 0) + 1
            # terminal states never retry
            if status in ("complete", "too_old", "after_hours"):
                row.attempts = max(row.attempts, max_attempts)
            if feats is not None:
                row.features = feats
            session.commit()
