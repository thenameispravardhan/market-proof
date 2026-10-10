"""Cost-aware event replay: what the news strategy would have earned.

The live system measures a 5-minute directional agreement ("AI correct").
That ignores entry latency, stops, targets, the hold window and Indian
intraday charges, which are the four things that decide whether a signal
makes money. This module replays recorded signals through the SAME trade
management the bot uses, on real 1-minute candles, and reports P&L after
costs with bootstrap confidence intervals.

Per event:

  entry   the first full minute at or after signal time + `entry_delay_s`,
          filled at that minute's OPEN, moved against us by `slippage_bps`.
          Entries outside the bot's entry window are not simulated (the
          bot could not have taken them).
  levels  `event_profiles` + `volatility.stop_distance`, with ATR computed
          from the preceding 5-minute bars, exactly as Manager._derive_levels
          does live (the sentiment-decay target cut included).
  exits   on each subsequent 1-minute bar, in the TradeManager's order:
          time exit (profile hold), breakeven lock, STOP before TARGET
          when one bar touches both (conservative), square-off. Exit
          fills also take `slippage_bps` against us.
  costs   app.algo.engine.charges (brokerage, STT, exchange, SEBI, GST,
          stamp) on a fixed notional per trade.

Not modelled: the consolidation and stall exits (both only cut winners
early) and partial fills. A replay is an estimate with known biases, so
every report carries its parameters.

Blocked signals are replayed too. Comparing what each block reason WOULD
have earned with what got through is the only way to tell whether a gate
saves money or just cuts volume. Signals whose action is HOLD get a
hypothetical side from the analysis (recommendation, then sentiment), and
are labelled as such.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Any, Callable, Iterable, Optional, Protocol, Sequence

from app.algo.engine import charges as engine_charges
from app.risk import event_profiles, volatility
from app.research import stats

IST = timezone(timedelta(hours=5, minutes=30))

Bar = tuple[datetime, float, float, float, float]   # (minute IST naive, o, h, l, c)


class CandleSource(Protocol):
    def bars(self, symbol: str, start: datetime, end: datetime) -> list[Bar]:
        """1-minute bars for `symbol` with start <= minute < end (naive IST)."""


@dataclass
class ReplayParams:
    entry_delay_s: float = 20.0
    slippage_bps: float = 5.0
    notional: float = 100_000.0
    entry_window: tuple[str, str] = ("09:30", "15:00")
    square_off: str = "15:10"
    atr_enabled: bool = True
    atr_period: int = 14
    breakeven_enabled: bool = True
    breakeven_at_pct: float = 2.0
    breakeven_lock_pct: float = 0.2
    sentiment_decay: bool = True
    decay_full_s: float = 2.0
    decay_partial_s: float = 5.0
    decay_partial_mult: float = 0.8
    decay_stale_mult: float = 0.6
    default_sl_min_pct: float = 1.0
    atr_max_stop_pct: float = 8.0
    smallcap_price: float = 200.0
    smallcap_sl_pct: float = 1.5

    @classmethod
    def from_settings(cls, settings: Any, **over: Any) -> "ReplayParams":
        g = lambda k, d: getattr(settings, k, d)  # noqa: E731
        p = cls(
            entry_window=(str(g("ENTRY_WINDOW_START_IST", "09:30")), str(g("ENTRY_WINDOW_END_IST", "15:00"))),
            square_off=str(g("SQUARE_OFF_TIME_IST", "15:10")),
            atr_enabled=bool(g("ATR_ENABLED", True)), atr_period=int(g("ATR_PERIOD", 14)),
            breakeven_enabled=bool(g("BREAKEVEN_ENABLED", True)),
            breakeven_at_pct=float(g("BREAKEVEN_AT_PCT", 2.0)),
            breakeven_lock_pct=float(g("BREAKEVEN_LOCK_PCT", 0.2)),
            sentiment_decay=bool(g("SENTIMENT_DECAY_ENABLED", True)),
            decay_full_s=float(g("SENTIMENT_DECAY_FULL_SECONDS", 2.0)),
            decay_partial_s=float(g("SENTIMENT_DECAY_PARTIAL_SECONDS", 5.0)),
            decay_partial_mult=float(g("SENTIMENT_DECAY_PARTIAL_MULT", 0.8)),
            decay_stale_mult=float(g("SENTIMENT_DECAY_STALE_MULT", 0.6)),
            default_sl_min_pct=float(g("DEFAULT_SL_MIN_PCT", 1.0)),
            atr_max_stop_pct=float(g("ATR_MAX_STOP_PCT", 8.0)),
            smallcap_price=float(g("SMALLCAP_PRICE", 200.0)),
            smallcap_sl_pct=float(g("DEFAULT_SL_SMALLCAP_PCT", 1.5)),
        )
        for k, v in over.items():
            if v is not None:
                setattr(p, k, v)
        return p


@dataclass
class ReplayEvent:
    signal_id: Optional[int]
    symbol: str
    side: int                       # +1 long, -1 short
    signal_at: datetime             # naive UTC (DB convention)
    filed_at: Optional[datetime]
    event_type: Optional[str]
    confidence: Optional[float]
    status: str
    block_reason: Optional[str]
    direction_source: str           # signal | recommendation | sentiment
    model: Optional[str] = None


@dataclass
class ReplayTrade:
    signal_id: Optional[int]
    symbol: str
    side: int
    event_type: Optional[str]
    confidence: Optional[float]
    status: str
    block_reason: Optional[str]
    direction_source: str
    entry_at: str
    entry: float
    stop: float
    target: float
    exit_at: str
    exit: float
    exit_reason: str
    qty: int
    gross_pnl: float
    charges: float
    net_pnl: float
    r_multiple: float
    pre_move_pct: Optional[float]   # signed move from the filing minute to entry
    post_move_pct: float            # signed gross move from entry to exit
    day: str
    model: Optional[str] = None


@dataclass
class ReplayResult:
    params: dict[str, Any]
    trades: list[ReplayTrade] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hhmm(s: str) -> dt_time:
    h, m = s.split(":")
    return dt_time(int(h), int(m))


def to_ist_naive(dt_utc_naive: datetime) -> datetime:
    aware = dt_utc_naive if dt_utc_naive.tzinfo else dt_utc_naive.replace(tzinfo=timezone.utc)
    return aware.astimezone(IST).replace(tzinfo=None)


def _ceil_minute(dt: datetime) -> datetime:
    floor = dt.replace(second=0, microsecond=0)
    return floor if floor == dt else floor + timedelta(minutes=1)


def five_minute_bars(bars: Sequence[Bar]) -> list[tuple[float, float, float, float, float]]:
    """Aggregate 1-minute bars to 5-minute [ts, o, h, l, c] rows (the shape
    volatility.compute_atr reads), matching the live ATR's resolution."""
    out: list[list[float]] = []
    key = None
    for t, o, h, l, c in bars:
        k = (t.date(), t.hour, t.minute // 5)
        if k != key:
            out.append([0.0, o, h, l, c])
            key = k
        else:
            row = out[-1]
            row[2], row[3], row[4] = max(row[2], h), min(row[3], l), c
    return [tuple(r) for r in out]  # type: ignore[misc]


def decay_multiplier(news_age_s: Optional[float], p: ReplayParams) -> float:
    if not p.sentiment_decay or news_age_s is None:
        return 1.0
    if news_age_s < p.decay_full_s:
        return 1.0
    if news_age_s < p.decay_partial_s:
        return p.decay_partial_mult
    return p.decay_stale_mult


_BLOCK_RE = re.compile(r"\|\s*blocked:\s*([A-Za-z0-9_]+)")
_DENY_RE = re.compile(r"\|\s*denied:\s*(.+)$")


def block_reason(status: str, rationale: Optional[str]) -> Optional[str]:
    """Reason code for a blocked signal, parsed from what the analyzer
    (`| denied: ...`) and the execution manager (`| blocked: CODE (...)`)
    append to the rationale."""
    if status != "blocked":
        return None
    text = rationale or ""
    m = _BLOCK_RE.search(text)
    if m:
        return m.group(1).upper()
    m = _DENY_RE.search(text)
    if m:
        d = m.group(1).strip()
        if "rule.action == HOLD" in d:
            return "RULE_HOLD"
        if "rule.action == BLOCK" in d:
            return "RULE_BLOCK"
        if d.startswith("MAX_SIGNALS_PER_DAY"):
            return "MAX_SIGNALS_PER_DAY"
        if d.startswith("confidence"):
            return "ZERO_CONFIDENCE"
        return re.split(r"[\s:(=]", d, maxsplit=1)[0].upper() or "DENIED"
    return "UNSPECIFIED"


# ---------------------------------------------------------------------------
# Core simulation (pure)
# ---------------------------------------------------------------------------


def simulate(event: ReplayEvent, bars: Sequence[Bar], p: ReplayParams) -> tuple[Optional[ReplayTrade], Optional[str]]:
    """Replay one event over its symbol's bars. (trade, None) or (None, why)."""
    sig_ist = to_ist_naive(event.signal_at)
    entry_min = _ceil_minute(sig_ist + timedelta(seconds=p.entry_delay_s))
    ew_start, ew_end = _hhmm(p.entry_window[0]), _hhmm(p.entry_window[1])
    if not (ew_start <= entry_min.time() <= ew_end) or entry_min.weekday() >= 5:
        return None, "outside_entry_window"
    idx = next((i for i, b in enumerate(bars) if b[0] >= entry_min), None)
    if idx is None or bars[idx][0].date() != entry_min.date() or bars[idx][0] - entry_min > timedelta(minutes=5):
        return None, "no_candles"
    side = event.side
    slip = p.slippage_bps / 10_000.0
    entry = bars[idx][1] * (1 + slip * side)
    if entry <= 0:
        return None, "bad_price"

    # Levels — the same calls Manager._derive_levels makes.
    profile = event_profiles.profile_for(event.event_type).resolved(_SettingsView(p))
    atr = None
    if p.atr_enabled:
        prior = [b for b in bars[:idx] if b[0] >= entry_min - timedelta(days=7)]
        atr = volatility.compute_atr(five_minute_bars(prior), p.atr_period)
    dist = volatility.stop_distance(
        entry=entry, atr=atr, mult=profile.sl_atr_mult, default_pct=profile.sl_default_pct,
        min_pct=p.default_sl_min_pct, max_pct=p.atr_max_stop_pct,
        smallcap_price=p.smallcap_price, smallcap_pct=p.smallcap_sl_pct,
    )
    if dist <= 0:
        return None, "bad_price"
    filed = event.filed_at
    news_age = None
    if filed is not None:
        news_age = (event.signal_at - filed).total_seconds() + p.entry_delay_s
    rr = profile.target_rr * decay_multiplier(news_age, p)
    stop = entry - side * dist
    target = entry + side * dist * rr
    hold_until = bars[idx][0] + timedelta(seconds=int(profile.max_hold_seconds))
    sq = datetime.combine(entry_min.date(), _hhmm(p.square_off))
    qty = max(1, int(p.notional // entry))

    exit_px = exit_reason = exit_at = None
    be_armed = False
    for t, o, h, l, c in bars[idx:]:
        if t.date() != entry_min.date():
            break
        if t >= sq:
            exit_px, exit_reason, exit_at = o, "SQUARE_OFF", t
            break
        if t >= hold_until:
            exit_px, exit_reason, exit_at = o, "TIME_EXIT", t
            break
        # Stop first: if one bar spans both levels we assume the worse fill.
        adverse = l if side > 0 else h
        favour = h if side > 0 else l
        if (adverse - stop) * side <= 0:
            # A gap through the stop fills at the open, not the stop.
            exit_px = min(o, stop) if side > 0 else max(o, stop)
            exit_reason, exit_at = ("BREAKEVEN_STOP" if be_armed else "STOP_LOSS"), t
            break
        if (favour - target) * side >= 0:
            exit_px = max(o, target) if side > 0 else min(o, target)
            exit_reason, exit_at = "TARGET", t
            break
        # Breakeven lock applies from the NEXT bar (the live check runs on
        # the tick that crosses the threshold, then the stop moves).
        if p.breakeven_enabled and not be_armed:
            best = (favour - entry) / entry * 100.0 * side
            if best >= p.breakeven_at_pct:
                lock = entry * (1 + side * p.breakeven_lock_pct / 100.0)
                if (lock - stop) * side > 0:
                    stop = lock
                be_armed = True
    if exit_px is None:
        last = [b for b in bars[idx:] if b[0].date() == entry_min.date()]
        if not last:
            return None, "no_candles"
        exit_px, exit_reason, exit_at = last[-1][4], "END_OF_DATA", last[-1][0]
    exit_fill = exit_px * (1 - slip * side)

    gross = (exit_fill - entry) * qty * side
    buy_v, sell_v = (entry * qty, exit_fill * qty) if side > 0 else (exit_fill * qty, entry * qty)
    ch = engine_charges(buy_v, sell_v, "eq")
    net = gross - ch
    risk = dist * qty
    pre = None
    if filed is not None:
        f_ist = to_ist_naive(filed).replace(second=0, microsecond=0)
        ref = next((b[4] for b in reversed(bars[:idx]) if b[0] <= f_ist), None)
        if ref:
            pre = (bars[idx][1] - ref) / ref * 100.0 * side
    return ReplayTrade(
        signal_id=event.signal_id, symbol=event.symbol, side=side, event_type=event.event_type,
        confidence=event.confidence, status=event.status, block_reason=event.block_reason,
        direction_source=event.direction_source, entry_at=bars[idx][0].isoformat(),
        entry=round(entry, 4), stop=round(entry - side * dist, 4), target=round(target, 4),
        exit_at=exit_at.isoformat(), exit=round(exit_fill, 4), exit_reason=exit_reason, qty=qty,
        gross_pnl=round(gross, 2), charges=round(ch, 2), net_pnl=round(net, 2),
        r_multiple=round(net / risk, 4) if risk else 0.0,
        pre_move_pct=None if pre is None else round(pre, 4),
        post_move_pct=round((exit_fill - entry) / entry * 100.0 * side, 4),
        day=entry_min.date().isoformat(), model=event.model,
    ), None


class _SettingsView:
    """The handful of Settings fields event_profiles.resolved() reads,
    sourced from the replay's own parameters so a replay is reproducible
    independent of the live configuration."""

    def __init__(self, p: ReplayParams) -> None:
        from app.config import get_settings

        s = get_settings()
        self.MIN_SENTIMENT_CONFIDENCE = getattr(s, "MIN_SENTIMENT_CONFIDENCE", 0.7)
        self.ATR_STOP_MULT = getattr(s, "ATR_STOP_MULT", 2.0)
        self.DEFAULT_SL_PCT = getattr(s, "DEFAULT_SL_PCT", 6.0)
        self.DEFAULT_TARGET_RR = getattr(s, "DEFAULT_TARGET_RR", 3.0)
        self.MAX_HOLD_SECONDS = getattr(s, "MAX_HOLD_SECONDS", 1080)


def run_replay(events: Iterable[ReplayEvent], source: CandleSource, p: ReplayParams) -> ReplayResult:
    """Replay every event, loading each symbol's candles once."""
    result = ReplayResult(params=asdict(p))
    by_symbol: dict[str, list[ReplayEvent]] = {}
    for e in events:
        by_symbol.setdefault(e.symbol.upper(), []).append(e)
    for sym, evs in by_symbol.items():
        first = min(to_ist_naive(e.signal_at) for e in evs) - timedelta(days=7)
        last = max(to_ist_naive(e.signal_at) for e in evs) + timedelta(days=1)
        try:
            bars = source.bars(sym, first, last)
        except Exception:  # noqa: BLE001 — one symbol's broken file must not end the run
            bars = []
        for e in evs:
            trade, why = simulate(e, bars, p) if bars else (None, "no_candles")
            if trade is not None:
                result.trades.append(trade)
            else:
                result.skipped[why or "unknown"] = result.skipped.get(why or "unknown", 0) + 1
    result.trades.sort(key=lambda t: t.entry_at)
    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

CONFIDENCE_BUCKETS = ((0.0, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1.01))


def _group(trades: Sequence[ReplayTrade], key: Callable[[ReplayTrade], Any]) -> dict[Any, list[ReplayTrade]]:
    out: dict[Any, list[ReplayTrade]] = {}
    for t in trades:
        out.setdefault(key(t), []).append(t)
    return out


def _block(ts: Sequence[ReplayTrade]) -> dict[str, Any]:
    return stats.summarize_r([t.r_multiple for t in ts], [t.net_pnl for t in ts])


def daily_sharpe(trades: Sequence[ReplayTrade], capital: float) -> Optional[float]:
    by_day: dict[str, float] = {}
    for t in trades:
        by_day[t.day] = by_day.get(t.day, 0.0) + t.net_pnl
    rets = [v / capital for v in by_day.values()]
    return stats.sharpe(rets) if len(rets) >= 5 else None


def report(result: ReplayResult, *, capital: float = 1_000_000.0) -> dict[str, Any]:
    trades = result.trades
    taken = [t for t in trades if t.status != "blocked"]
    blocked = [t for t in trades if t.status == "blocked"]
    out: dict[str, Any] = {
        "params": result.params,
        "skipped": result.skipped,
        "all_directional": {**_block(trades), "daily_sharpe": _r(daily_sharpe(trades, capital))},
        "taken": {**_block(taken), "daily_sharpe": _r(daily_sharpe(taken, capital))},
        "blocked": _block(blocked),
        "by_block_reason": {k or "NONE": _block(v) for k, v in
                            sorted(_group(blocked, lambda t: t.block_reason).items(), key=lambda kv: -len(kv[1]))},
        "by_event_type": {k or "UNKNOWN": _block(v) for k, v in
                          sorted(_group(trades, lambda t: t.event_type).items(), key=lambda kv: -len(kv[1]))},
        "by_exit_reason": {k: len(v) for k, v in _group(trades, lambda t: t.exit_reason).items()},
        "by_direction_source": {k: _block(v) for k, v in _group(trades, lambda t: t.direction_source).items()},
        "confidence_buckets": confidence_table(trades),
        "breakeven_pct_at_notional": round(
            engine_charges(result.params["notional"], result.params["notional"]) / result.params["notional"] * 100, 4),
    }
    return out


def confidence_table(trades: Sequence[ReplayTrade]) -> list[dict[str, Any]]:
    """The inverted-confidence test: if high-confidence filings are the
    obvious ones, the move should already be realised by entry (high
    pre_move) and little should be left after it (low post_move)."""
    rows = []
    for lo, hi in CONFIDENCE_BUCKETS:
        ts = [t for t in trades if t.confidence is not None and lo <= t.confidence < hi]
        pre = [t.pre_move_pct for t in ts if t.pre_move_pct is not None]
        post = [t.post_move_pct for t in ts]
        lo_ci, hi_ci = stats.bootstrap_ci(post)
        rows.append({
            "bucket": f"{lo:.1f}-{min(hi, 1.0):.1f}", "n": len(ts),
            "mean_pre_move_pct": _r(stats.mean(pre)),
            "mean_post_move_pct": _r(stats.mean(post)),
            "post_move_ci95": [_r(lo_ci), _r(hi_ci)],
            "expectancy_r": _r(stats.mean([t.r_multiple for t in ts])),
        })
    return rows


def _r(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(float(x), 4)


# ---------------------------------------------------------------------------
# Loading events from the database
# ---------------------------------------------------------------------------


def side_of(action: Optional[str], recommendation: Optional[str], sentiment: Optional[str]) -> tuple[int, str]:
    a = (action or "").upper()
    if a in ("BUY", "SELL"):
        return (1 if a == "BUY" else -1), "signal"
    r = (recommendation or "").lower()
    if r in ("buy", "sell"):
        return (1 if r == "buy" else -1), "recommendation"
    s = (sentiment or "").lower()
    if s in ("positive", "negative"):
        return (1 if s == "positive" else -1), "sentiment"
    return 0, "none"


def load_events(db: Any, *, since: Optional[datetime] = None, until: Optional[datetime] = None,
                limit: Optional[int] = None, include_hypothetical: bool = True) -> list[ReplayEvent]:
    """Signals joined to their analysis and announcement, newest first up to
    `limit`. HOLD signals become hypothetical events (side from the
    analysis) when `include_hypothetical`."""
    from sqlalchemy import select

    from app.db.models import Analysis, Announcement, Signal

    stmt = (
        select(Signal, Analysis, Announcement)
        .outerjoin(Analysis, Signal.analysis_id == Analysis.id)
        .outerjoin(Announcement, Analysis.announcement_id == Announcement.id)
        .order_by(Signal.id.desc())
    )
    if since is not None:
        stmt = stmt.where(Signal.created_at >= since)
    if until is not None:
        stmt = stmt.where(Signal.created_at < until)
    if limit:
        stmt = stmt.limit(limit)
    out: list[ReplayEvent] = []
    for sig, an, ann in db.execute(stmt).all():
        side, src = side_of(sig.action, getattr(an, "recommendation", None), getattr(an, "sentiment", None))
        if side == 0 or (src != "signal" and not include_hypothetical):
            continue
        out.append(ReplayEvent(
            signal_id=sig.id, symbol=str(sig.symbol or "").upper(), side=side,
            signal_at=sig.created_at, filed_at=getattr(ann, "filed_at", None),
            event_type=getattr(ann, "event_type", None),
            confidence=sig.confidence if sig.confidence is not None else getattr(an, "confidence", None),
            status=str(sig.status or ""), block_reason=block_reason(str(sig.status or ""), sig.rationale),
            direction_source=src, model=getattr(an, "model", None),
        ))
    return out


class ParquetCandleSource:
    """The bot's own 1-minute candle store (AIdataset/stockdata base export +
    monthly increments written by candle_sync), read with DuckDB."""

    def bars(self, symbol: str, start: datetime, end: datetime) -> list[Bar]:
        import duckdb

        from app.services.warehouse_prices import candle_source

        src = candle_source(symbol)
        if src is None:
            return []
        con = duckdb.connect()
        try:
            rows = con.execute(
                f"SELECT CAST(datetime AS TIMESTAMP) AS t, open, high, low, close FROM {src} "
                "WHERE CAST(datetime AS TIMESTAMP) >= ? AND CAST(datetime AS TIMESTAMP) < ? "
                "ORDER BY t", [start, end]).fetchall()
        finally:
            con.close()
        seen: set[datetime] = set()
        out: list[Bar] = []
        for t, o, h, l, c in rows:      # increments can overlap the base export
            if t in seen or o is None:
                continue
            seen.add(t)
            out.append((t, float(o), float(h), float(l), float(c)))
        return out
