"""Volatility helpers — ATR-based stops + a volatility-regime seam.

The bot's initial stop used to be a flat percentage of entry
(`DEFAULT_SL_PCT`). That over-stops quiet names and under-stops fast
movers — the second case is where the "whipsaw on volatile news" and
"holding losers" pains come from. This module computes a volatility-
adaptive stop distance (Average True Range) so the stop is wide enough
to survive a volatile name's noise and tight enough to cut the loss on a
quiet one. Research (Chandelier / ATR exits) shows this materially beats
a fixed-% stop and reduces whipsaw.

Everything here is **fail-safe**: when ATR (or VIX) data is unavailable
the provider returns ``None`` and the caller falls back to the existing
percentage stop / no-op — we never fabricate a number, matching the
`RISK.md` "never a phantom guard" philosophy. The live ATR path is the
Fyers history-candle provider, which the lifespan wires in for every mode;
with no connected Fyers account it returns nothing and the caller falls
back to the percentage stop.

Design:
  - `compute_atr(candles, period)` — Wilder's ATR over OHLC candles.
  - `VolatilityProvider` — `atr(symbol) -> float | None`. Two impls:
        `NullVolatilityProvider`      (always None — the safe default)
        `FyersCandleVolatilityProvider` (real ATR from Fyers history
                                       candles — the live default)
  - `stop_distance(...)` — the ATR×mult stop distance, clamped to a sane
    band of entry and falling back to the percentage stop when ATR is None.
  - `VolatilityRegime` — `india_vix() -> float | None` + `vix_risk_multiplier`
    for a high-VIX position-size throttle (no-op until the VIX feed lands).
"""
from __future__ import annotations

import time
from collections import deque
from typing import (
    Any,
    Awaitable,
    Callable,
    Deque,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    runtime_checkable,
)

from app.logging_config import get_logger

log = get_logger(__name__)


# ---- ATR ---------------------------------------------------------------


def _hlc(candle: Any) -> Optional[tuple[float, float, float]]:
    """Extract (high, low, close) from a candle in any common shape.

    Accepts:
      - (high, low, close) tuples/lists,
      - Fyers-style [ts, open, high, low, close, volume] lists (len >= 5),
      - dicts with high/low/close keys,
      - objects with .high/.low/.close attributes.
    Returns None when the candle can't be interpreted.
    """
    try:
        if isinstance(candle, Mapping):
            return (float(candle["high"]), float(candle["low"]), float(candle["close"]))
        if isinstance(candle, (list, tuple)):
            if len(candle) == 3:
                h, l, c = candle
                return (float(h), float(l), float(c))
            if len(candle) >= 5:
                # Fyers history row: [ts, open, high, low, close, (volume)].
                return (float(candle[2]), float(candle[3]), float(candle[4]))
            return None
        h = getattr(candle, "high", None)
        l = getattr(candle, "low", None)
        c = getattr(candle, "close", None)
        if h is None or l is None or c is None:
            return None
        return (float(h), float(l), float(c))
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def compute_atr(candles: Sequence[Any], period: int = 14) -> Optional[float]:
    """Wilder's Average True Range over `candles` (oldest → newest).

    Returns None when there aren't enough valid candles (need > period so
    the first true range has a previous close). Never raises on malformed
    input — bad candles are skipped and, if too few remain, returns None.
    """
    if period < 1:
        return None
    rows = [hlc for hlc in (_hlc(c) for c in candles) if hlc is not None]
    if len(rows) < period + 1:
        return None
    true_ranges: list[float] = []
    prev_close = rows[0][2]
    for high, low, close in rows[1:]:
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
        prev_close = close
    if len(true_ranges) < period:
        return None
    # Seed with the simple average of the first `period` TRs, then smooth.
    atr = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period
    return float(atr) if atr > 0 else None


# ---- Providers ---------------------------------------------------------


@runtime_checkable
class VolatilityProvider(Protocol):
    """Returns the current ATR (price units) for a symbol, or None."""

    def atr(self, symbol: str) -> Optional[float]: ...


class NullVolatilityProvider:
    """The safe default: no ATR data, ever. Callers fall back to the %
    stop. Used until a real candle feed is wired (Phase 5)."""

    def atr(self, symbol: str) -> Optional[float]:  # noqa: D401
        return None


def resolve_atr(
    provider: Optional[VolatilityProvider], symbol: str
) -> Optional[float]:
    """Best-effort ATR lookup that never raises (a provider hiccup must
    not break order placement — we just fall back to the % stop)."""
    if provider is None:
        return None
    try:
        atr = provider.atr(symbol)
    except Exception:  # noqa: BLE001
        log.debug("volatility.atr_provider_failed", symbol=symbol)
        return None
    if atr is None or atr <= 0:
        return None
    return float(atr)


# ---- Stop distance -----------------------------------------------------


def stop_distance(
    *,
    entry: float,
    atr: Optional[float],
    mult: float,
    default_pct: float,
    min_pct: float,
    max_pct: float,
    smallcap_price: float,
    smallcap_pct: float,
) -> float:
    """Stop distance (price units) for `entry`.

    ATR path:  distance = atr × mult, clamped to [floor, max_pct%·entry].
    Fallback:  distance = default_pct%·entry (when atr is None/≤0).

    The floor is `min_pct`% of entry, or `smallcap_pct`% for sub-
    `smallcap_price` names (which are noisier in percentage terms). The
    result is always a positive number so the caller can size against it.
    """
    entry = float(entry)
    if entry <= 0:
        return 0.0
    floor_pct = smallcap_pct if entry < smallcap_price else min_pct
    floor = entry * (float(floor_pct) / 100.0)
    ceil = entry * (float(max_pct) / 100.0)
    if atr is not None and atr > 0:
        dist = float(atr) * float(mult)
    else:
        dist = entry * (float(default_pct) / 100.0)
    # Clamp into [floor, ceil]; guard against an inverted band.
    lo, hi = min(floor, ceil), max(floor, ceil)
    return max(lo, min(dist, hi))


# ---- Volatility regime (India VIX) seam --------------------------------


@runtime_checkable
class VolatilityRegime(Protocol):
    """Returns the current India VIX level, or None when unavailable."""

    def india_vix(self) -> Optional[float]: ...


class NullVolatilityRegime:
    """Safe default: no VIX data. The VIX gate / throttle no-op until the
    NSE:INDIAVIX feed is wired (Phase 5)."""

    def india_vix(self) -> Optional[float]:
        return None


# Async fetch callbacks the live (Fyers) providers depend on. Injected by
# the lifespan so this module never imports the broker layer (and never
# reaches for a non-Fyers source — see the Fyers-only-pricing rule).
CandleFetch = Callable[[str, str, int], Awaitable[Sequence[Any]]]
VixFetch = Callable[[], Awaitable[Optional[float]]]


class FyersCandleVolatilityProvider:
    """ATR from Fyers intraday candles, cached per symbol with a TTL.

    `atr(symbol)` is sync (the risk path is sync) and returns the last
    cached ATR (or None). `refresh(symbol)` does the async fetch + ATR
    compute and is awaited by the manager on the async path *before*
    sizing, so a freshly-seen symbol is warm on its first trade. Every
    failure (no Fyers account, short series, broker error) is swallowed →
    None → the % stop. The injected `fetch_candles` is the only data path,
    so this never reintroduces a non-Fyers source.
    """

    def __init__(
        self,
        fetch_candles: CandleFetch,
        *,
        period: int = 14,
        resolution: str = "5",
        lookback_days: int = 5,
        ttl_s: float = 300.0,
    ) -> None:
        self._fetch = fetch_candles
        self._period = int(period)
        self._resolution = str(resolution)
        self._days = int(lookback_days)
        self._ttl = float(ttl_s)
        self._cache: dict[str, tuple[float, float]] = {}  # sym -> (atr, monotonic ts)

    def atr(self, symbol: str) -> Optional[float]:
        e = self._cache.get(symbol.upper().strip())
        return e[0] if e else None

    async def refresh(self, symbol: str) -> None:
        """Refresh the cached ATR for `symbol` if the entry is missing or
        older than the TTL. Awaited by the manager before sizing."""
        sym = symbol.upper().strip()
        e = self._cache.get(sym)
        now = time.monotonic()
        if e is not None and (now - e[1]) < self._ttl:
            return  # still fresh
        try:
            candles = await self._fetch(sym, self._resolution, self._days)
        except Exception:  # noqa: BLE001
            log.debug("volatility.candle_fetch_failed", symbol=sym)
            return
        atr = compute_atr(candles or [], self._period)
        if atr is not None and atr > 0:
            self._cache[sym] = (float(atr), now)


class FyersVolatilityRegime:
    """India VIX from the Fyers index quote, cached with a TTL.

    `india_vix()` is sync (returns the cached value or None); `refresh()`
    is awaited by the manager's pre-entry gate before the VIX checks /
    size throttle. Fail-safe — any error leaves the value None, so the
    gate and throttle no-op.
    """

    def __init__(self, fetch_vix: VixFetch, *, ttl_s: float = 60.0) -> None:
        self._fetch = fetch_vix
        self._ttl = float(ttl_s)
        self._value: Optional[float] = None
        self._ts: float = 0.0

    def india_vix(self) -> Optional[float]:
        return self._value

    async def refresh(self) -> None:
        now = time.monotonic()
        if self._value is not None and (now - self._ts) < self._ttl:
            return
        try:
            v = await self._fetch()
        except Exception:  # noqa: BLE001
            log.debug("volatility.vix_fetch_failed")
            return
        if v is not None and float(v) > 0:
            self._value = float(v)
            self._ts = now


def vix_risk_multiplier(
    vix: Optional[float],
    *,
    calm_below: float = 18.0,
    elevated_below: float = 25.0,
    elevated_mult: float = 0.75,
    high_mult: float = 0.5,
) -> float:
    """Map a VIX level to a per-trade risk multiplier.

    Returns 1.0 when `vix` is None (no data → no throttle), 1.0 in a calm
    regime, `elevated_mult` between `calm_below` and `elevated_below`, and
    `high_mult` above that. Research: size down as expected volatility
    rises so the constant dollar-risk assumption holds. This adjusts risk
    sizing only — it does NOT touch the LLM conviction (sizing stays
    decoupled from conviction by design).
    """
    if vix is None:
        return 1.0
    v = float(vix)
    if v < float(calm_below):
        return 1.0
    if v < float(elevated_below):
        return float(elevated_mult)
    return float(high_mult)
