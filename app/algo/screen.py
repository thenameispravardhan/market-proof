"""Large universes on a small Fyers data budget: screen every symbol on batch
quotes, fetch exact candles only for the ones that may act.

Fyers' Standard plan allows 50 data calls a minute and 5,000 a day, and the
history endpoint serves one symbol a call — so the runner's usual "fetch every
symbol's candles at each close" stops at a few dozen symbols. A strategy with
more symbols than fit in the minute after a close (`exact_capacity`) runs
through here:

  * warm-up — before the open (from 08:30) and for anything added later, each
    symbol's exact candles once a day: the runner's own data.live_bundle, kept
    as typed arrays;
  * at each candle close — one batch-quote poll covers every symbol (50 a call:
    20 calls for 1,000) and appends an approximate candle: open = the previous
    close (the day's open for the first), close = the LTP just after the close,
    high / low = the day's high / low when it moved since the last candle (else
    the open / close), volume = the change in the day's volume. Intraday higher
    timeframes the conditions read are extended from it;
  * the strategy is evaluated on that with the screening tolerance
    (engine.screening): every symbol whose entry may have fired, every symbol
    holding a position (its exits) and every symbol the screen can't vouch for
    (no warm-up, a missed poll) is a candidate;
  * candidates take the runner's exact path — live_bundle + _decide, unchanged
    — so no order is decided on an approximate candle; their exact candles then
    replace the approximate ones.

Daily / weekly strategies need no screen: the warm-up (fetched once the candle
they act on has closed) IS their exact data, so every symbol is decided from it
at the open without a call.

Not screened (they need each minute's data): volume / turnover candles and
order-flow indicators — such a strategy gets the exact path for as many
symbols as the budget serves.
"""
from __future__ import annotations

import asyncio
import time
from array import array
from typing import Any, Iterable, Optional

from app.algo import data, engine
from app.execution import data_budget
from app.logging_config import get_logger

log = get_logger(__name__)

IST = 19800
DAY = 86400
OPEN_S, CLOSE_S = 33300, 55800
WARM_FROM_MIN = 8 * 60 + 30          # warm-ups start this early: the day's budget refills at midnight
QUOTE_BATCH = 50                     # symbols per /data/quotes call (Fyers' cap)
SNAP_FRESH_S = 20                    # a snapshot this young at a close is reused across strategies
_ARR = ("t", "tc", "o", "h", "l", "c", "v")


def slack() -> float:
    from app.config import get_settings

    return max(0.0, float(getattr(get_settings(), "ALGO_SCREEN_SLACK_PCT", 0.5) or 0.0)) / 100


def screenable(spec: dict) -> bool:
    """Time candles without per-minute order-flow: what a quote snapshot can approximate."""
    return spec["bars"]["type"] == "time" and not engine.needs_minutes(spec)


def calls_per_symbol(spec: dict) -> int:
    """History calls data.live_bundle makes for one symbol (other symbols' candles are shared)."""
    return 1 + len(engine.cond_timeframes(spec))


def exact_capacity(spec: dict) -> int:
    """Symbols whose exact candles fit in the minute after a close, in the bulk lane."""
    lim = data_budget.limits()
    per_min = int(lim["per_min"] * (100 - lim["reserve_pct"]) / 100)
    return max(1, per_min // calls_per_symbol(spec))


def daily_calls(spec: dict, confirm_rate: float = 0.05) -> dict[str, int]:
    """What one strategy costs a day in the bulk lane, screened or exact."""
    n = len(spec["symbols"])
    tf = spec["timeframe"]
    bars = 1 if engine.multi_day(tf) else max(1, round(engine.per_session(tf)))
    warm = n * calls_per_symbol(spec)
    if engine.multi_day(tf):
        return {"warm": warm, "polls": 0, "confirm": 0, "total": warm}
    if n <= exact_capacity(spec) or not screenable(spec):
        return {"warm": 0, "polls": 0, "confirm": n * bars * calls_per_symbol(spec),
                "total": n * bars * calls_per_symbol(spec)}
    polls = bars * -(-n // QUOTE_BATCH)
    confirm = int(bars * max(1.0, n * confirm_rate)) * calls_per_symbol(spec)
    return {"warm": warm, "polls": polls, "confirm": confirm, "total": warm + polls + confirm}


def _compact(d: dict) -> dict:
    """A bundle's candle lists as typed arrays (8 bytes a value): ~1,000 kept all day."""
    for k in _ARR:
        v = d.get(k)
        if isinstance(v, list) and v and all(isinstance(x, (int, float)) for x in v[:1]):
            try:
                d[k] = array("q" if k in ("t", "tc") else "d", v)
            except (TypeError, OverflowError):
                pass
    for h in (d.get("htf") or {}).values():
        if isinstance(h, dict):
            _compact(h)
    return d


class Screener:
    def __init__(self) -> None:
        self.day: Optional[int] = None
        self.warm: dict[tuple[int, str], dict] = {}        # (strategy, symbol) -> exact candles (+ approx since)
        self.spec_of: dict[int, str] = {}                   # strategy -> the spec its warm-ups were for
        self.snaps: dict[str, tuple] = {}                   # symbol -> (ts, ltp, open, high, low, volume)
        self.unsure: set[tuple[int, str]] = set()           # missed a poll: candidates until confirmed
        self.passes: dict[int, tuple[int, set[str]]] = {}   # strategy -> (bar, candidates) of its last screen
        self.stats: dict[int, dict[str, Any]] = {}
        self.warming: Optional[asyncio.Task] = None
        self.warm_errors = 0

    # -- which strategies -------------------------------------------------

    @staticmethod
    def large(spec: dict) -> bool:
        return len(spec["symbols"]) > exact_capacity(spec)

    def uses(self, spec: dict) -> bool:
        """Runs through the screen: more symbols than the exact path serves, and
        either a daily / weekly timeframe or approximable intraday candles."""
        return self.large(spec) and (engine.multi_day(spec["timeframe"]) or screenable(spec))

    # -- warm-up ------------------------------------------------------------

    def _new_day(self, day: int) -> None:
        if self.day != day:
            self.day = day
            self.warm.clear()
            self.snaps.clear()
            self.unsure.clear()
            self.passes.clear()
            self.warm_errors = 0

    def maintain(self, strategies: list[dict], now: float, held: Iterable[tuple[int, str]] = ()) -> None:
        """Start (or keep going) the warm-up of every symbol of every screened,
        switched-on strategy — held positions' symbols first."""
        day = int((now + IST) // DAY)
        self._new_day(day)
        mins = int((now + IST) % DAY) // 60
        if mins < WARM_FROM_MIN or (self.warming is not None and not self.warming.done()):
            return
        held = set(held)
        todo: list[tuple[dict, str]] = []
        for s in strategies:
            if not s["enabled"] or not self.uses(s["spec"]):
                continue
            key = repr(s["spec"])
            if self.spec_of.get(s["id"]) != key:           # edited: warm again
                self.spec_of[s["id"]] = key
                for k in [k for k in self.warm if k[0] == s["id"]]:
                    del self.warm[k]
            todo += [(s, x) for x in s["spec"]["symbols"] if (s["id"], x) not in self.warm]
        if todo:
            todo.sort(key=lambda it: (it[0]["id"], it[1]) not in held)
            self.warming = asyncio.create_task(self._warm(todo), name="algo-screen-warmup")

    async def _warm(self, todo: list[tuple[dict, str]]) -> None:
        for s, sym in todo:
            try:
                with data_budget.bulk():
                    d = await data.live_bundle(s["spec"], sym, time.time())
            except data_budget.DataBudgetExhausted:
                log.warning("algo.screen.warmup_budget", left=len(todo))
                return
            except Exception as e:  # noqa: BLE001 — that symbol stays unwarmed: a candidate for the exact path
                self.warm_errors += 1
                log.warning("algo.screen.warmup_failed", symbol=sym, error=str(e)[:200])
                continue
            self.warm[(s["id"], sym)] = _compact(d)

    def bundle(self, sid: int, sym: str) -> Optional[dict]:
        return self.warm.get((sid, sym))

    def absorb(self, sid: int, sym: str, d: dict) -> None:
        """A candidate's exact candles (just fetched): they replace the screen's."""
        if (sid, sym) in self.warm or sid in self.spec_of:
            self.warm[(sid, sym)] = _compact(d)
            self.unsure.discard((sid, sym))

    # -- the close ----------------------------------------------------------

    async def _poll(self, symbols: list[str], close_t: int) -> int:
        """Batch quotes for the symbols without a snapshot since `close_t`."""
        from app.api.market import _fyers_backend

        need = [x for x in symbols if (self.snaps.get(x) or (0,))[0] < close_t]
        if not need:
            return 0
        be = _fyers_backend()
        if be is None:
            return 0
        calls = 0
        for i in range(0, len(need), QUOTE_BATCH):
            part = need[i:i + QUOTE_BATCH]
            try:
                with data_budget.bulk():
                    qs = await be.get_quote(part)
            except data_budget.DataBudgetExhausted:
                break
            except Exception as e:  # noqa: BLE001 — those symbols are unsure this candle
                log.warning("algo.screen.poll_failed", error=str(e)[:200])
                continue
            calls += 1
            now = time.time()
            got = {q.symbol.upper(): q for q in qs}
            for x in part:
                q = got.get(x.upper())
                if q is None or not q.last_price:
                    continue
                ex = q.extra or {}
                self.snaps[x] = (now, float(q.last_price), ex.get("open"), ex.get("high"), ex.get("low"),
                                 q.volume)
        return calls

    def _append(self, d: dict, bar: int, tf_s: int, snap: Optional[tuple]) -> bool:
        """The approximate candle for `bar`. False when the screen can't vouch
        for this symbol's candles: no snapshot for it, or closes missed before
        it (flat stand-ins fill them; its exact candles will replace them)."""
        if not d["t"] or d["t"][-1] >= bar:
            return True
        if snap is None:
            return False
        _, ltp, d_open, d_hi, d_lo, vol = snap
        day0 = bar - (bar + IST) % DAY
        seen = d.get("_seen")
        if seen is None or seen[0] != day0:          # what today's candles so far already hold
            idx = [i for i in range(len(d["t"])) if d["t"][i] >= day0]
            seen = (day0, max((d["h"][i] for i in idx), default=None), min((d["l"][i] for i in idx), default=None),
                    sum(d["v"][i] for i in idx), bool(idx))
        today = seen[4]
        prev = d["c"][-1] if today else (d_open or ltp)
        t = d["t"][-1] + tf_s if today else day0 + OPEN_S
        gap = t < bar
        while t < bar:                                # a missed close: a flat stand-in, the symbol stays unsure
            self._push(d, t, prev, prev, prev, prev, 0)
            t += tf_s
        o = prev if today else (d_open or prev)
        h, l = max(o, ltp), min(o, ltp)
        if d_hi and (seen[1] is None or d_hi > seen[1]):
            h = max(h, d_hi)
        if d_lo and (seen[2] is None or d_lo < seen[2]):
            l = min(l, d_lo)
        v = max(0, int(vol or 0) - int(seen[3] or 0)) if vol is not None else 0
        self._push(d, bar, o, h, l, ltp, v)
        d["_seen"] = (day0, max(x for x in (seen[1], h, d_hi) if x is not None),
                      min(x for x in (seen[2], l, d_lo) if x is not None),
                      int(vol) if vol is not None else int(seen[3] or 0) + v, True)
        return not gap

    @staticmethod
    def _push(d: dict, t: int, o: float, h: float, l: float, c: float, v: int) -> None:
        for k, x in (("t", t), ("o", o), ("h", h), ("l", l), ("c", c), ("v", v)):
            d[k].append(x)
        for tf, b in (d.get("htf") or {}).items():    # intraday higher timeframes: extend their forming candle
            if not b or tf >= 1440 or not b.get("t"):
                continue
            n = tf * 60
            o_s = t - (t + IST) % DAY + OPEN_S
            key = o_s + (t - o_s) // n * n
            if b["t"][-1] == key:
                b["h"][-1], b["l"][-1], b["c"][-1] = max(b["h"][-1], h), min(b["l"][-1], l), c
                b["v"][-1] += v
            elif b["t"][-1] < key:
                for k2, x in (("t", key), ("o", o), ("h", h), ("l", l), ("c", c), ("v", v)):
                    b[k2].append(x)

    async def candidates(self, s: dict, bar: int, tf_s: int, now: float, held: dict[str, str]) -> set[str]:
        """The symbols of `s` that take the exact path for the candle starting at
        `bar` (computed once per candle; the runner asks every second). `held`:
        symbol -> side of this strategy's open positions."""
        sid, spec = s["id"], s["spec"]
        last = self.passes.get(sid)
        if last and last[0] == bar:
            return last[1]
        syms = spec["symbols"]
        close_t = min(bar + tf_s, bar - (bar + IST) % DAY + CLOSE_S)
        t0 = time.time()
        calls = await self._poll([x for x in syms if (sid, x) in self.warm], close_t)
        shared = await self._shared(spec, now)
        ready, unsure = [], set()
        for x in syms:
            d = self.warm.get((sid, x))
            if d is None:
                continue
            if shared:
                d.update(shared)
            if not self._append(d, bar, tf_s, self.snaps.get(x)) or (sid, x) in self.unsure:
                unsure.add(x)
                self.unsure.add((sid, x))
            else:
                ready.append(x)
        mine = {x: side for x, side in held.items() if x in set(syms)}
        fired = await asyncio.to_thread(self._screen, spec, [(x, self.warm[(sid, x)]) for x in ready], mine)
        cold = {x for x in syms if (sid, x) not in self.warm}
        # Positions whose exit may need more than their candle (option Greeks / premium
        # rules, re-entries / rolls / hedges) are checked every candle.
        always = set(mine) if (spec["instrument"]["type"] == "option" and (
            (spec.get("position_rules") or {}).get("exit") or engine.adjusts(spec))) else set()
        exits = {x for x in fired if x in mine} | always
        # Confirmations are paced by the data budget across the candle (the bulk
        # lane's per-minute window spaces them): what fits before the next close,
        # after the polls — exits first, then entries, then the symbols to re-sync,
        # then the unwarmed. The rest wait for a later close (unsure / cold stay so).
        lim = data_budget.limits()
        per_min = int(lim["per_min"] * (100 - lim["reserve_pct"]) / 100)
        minutes = max(1, tf_s // 60 - 1)
        cap = max(len(exits), 1, (per_min * minutes - calls * minutes) // calls_per_symbol(spec))
        order = list(dict.fromkeys([*sorted(exits), *sorted(fired - exits), *sorted(unsure), *sorted(cold)]))
        out = set(order[:cap])
        self.passes[sid] = (bar, out)
        self.stats[sid] = {"bar": bar, "symbols": len(syms), "warm": len(syms) - len(cold), "screened": len(ready),
                           "fired": len(fired), "unsure": len(unsure), "held": len(mine), "candidates": len(out),
                           "skipped": len(fired - out), "deferred": len(order) - len(out) - len(fired - out),
                           "quote_calls": calls, "ms": int((time.time() - t0) * 1000)}
        return out

    async def _shared(self, spec: dict, now: float) -> dict[str, Any]:
        """Other symbols' candles and India VIX the conditions read, fresh for this close."""
        out: dict[str, Any] = {}
        keep = lambda tf, extra: engine.history_bars(spec, tf) + extra   # noqa: E731
        try:
            with data_budget.bulk():
                xs = {}
                for ref, tfs in engine.ref_frames(spec).items():
                    b = data.as_base(await data.shared_bars(ref, spec["timeframe"], keep(spec["timeframe"], 60), now),
                                     spec["timeframe"])
                    b["tf_min"] = spec["timeframe"]
                    b["htf"] = {tf: await data.shared_bars(ref, tf, keep(tf, 5), now) for tf in tfs - {spec["timeframe"]}}
                    b["meta"] = {"symbol": ref}
                    xs[ref] = b
                if xs:
                    out["xs"] = xs
                if spec["timeframe"] in engine.vix_timeframes(spec):
                    out["vix"] = await data.shared_bars(data.VIX, spec["timeframe"],
                                                        engine.warmup_bars(spec) + 60, now)
        except Exception as e:  # noqa: BLE001 — the screen then reads the warm-up's copies
            log.warning("algo.screen.shared_failed", error=str(e)[:200])
        return out

    @staticmethod
    def _screen(spec: dict, items: list[tuple[str, dict]], held: Optional[dict[str, str]] = None) -> set[str]:
        """Symbols whose entry — or, holding a position, whose exit — may have
        fired on their newest candle, with the tolerance."""
        held = held or {}
        out: set[str] = set()
        with engine.screening(slack()):
            for sym, d in items:
                try:
                    if spec["instrument"]["type"] != "equity" or any(
                            o["ind"] == "DTE" for o in engine._walk_operands(spec)):
                        engine.prepare(d, spec)
                    sig = engine.signals(d, spec, {})
                except Exception:  # noqa: BLE001 — can't evaluate: let the exact path decide
                    out.add(sym)
                    continue
                side = held.get(sym)
                if side is not None:
                    if sig["exit_long" if side == "BUY" else "exit_short"][-1]:
                        out.add(sym)
                elif sig["entry_long"][-1] or sig["entry_short"][-1]:
                    out.add(sym)
        return out

    def status(self) -> dict[str, Any]:
        return {"warm": len(self.warm), "warming": self.warming is not None and not self.warming.done(),
                "warm_errors": self.warm_errors, "strategies": self.stats,
                "budget": data_budget.budget().stats()}
