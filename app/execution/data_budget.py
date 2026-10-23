"""One budget for Fyers' Data APIs: quotes, depth, history, option chain.

Fyers caps them per user (fyers.in/api-docs/v3, Plans & Limits):

    plan       per second   per minute   per day     data-socket symbols
    standard   5            50           5,000       100
    prime      10           500          5,00,000    5,000

Past a cap Fyers answers 429, and past the day's it answers nothing until
midnight — the Trade page's quotes and an exit's price included. So every
data call takes a slot here first (FyersLiveBackend._request), in one of two
lanes (`bulk()` picks it for the calls inside):

  * "ui" (default): the Trade page, quotes for orders, exits. Waits only for
    the per-second / per-minute window; never refused for the day.
  * "bulk": the automation's screening and candle fetches, backtest
    downloads, the nightly sync. Kept to (100 - FYERS_DATA_RESERVE_PCT)% of
    each minute and of the day, so a big universe can never take the quotes
    an order needs; past its share of the day it is refused
    (DataBudgetExhausted) rather than queued into tomorrow.

The day's count is shared with the tick collector (its own process) through
data/fyers_data_budget.json under a file lock, folded in every few seconds.
The per-second / per-minute windows are per process — the collector's few
planning calls are spaced well inside them.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections import deque
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator, Optional

from app.logging_config import get_logger

log = get_logger(__name__)

PLANS: dict[str, dict[str, int]] = {
    "standard": {"per_sec": 5, "per_min": 50, "per_day": 5_000, "ws_symbols": 100},
    "prime": {"per_sec": 10, "per_min": 500, "per_day": 500_000, "ws_symbols": 5_000},
}
STATE = (Path(__import__("tempfile").gettempdir()) / f"fyers_data_budget_test_{os.getpid()}.json"
         if os.environ.get("TESTING") == "1" else
         Path(__file__).resolve().parents[2] / "data" / "fyers_data_budget.json")
IST = 19800
SYNC_S = 5.0                 # fold this process's calls into the shared day count this often
PENALTY_S = 15.0             # after a 429 the bulk lane waits this long

_lane: ContextVar[str] = ContextVar("fyers_data_lane", default="ui")


class DataBudgetExhausted(RuntimeError):
    """The bulk lane has used its share of today's Fyers data calls."""


@contextlib.contextmanager
def bulk() -> Iterator[None]:
    """Data calls made inside go in the bulk lane (automation / sync / backtests)."""
    tok = _lane.set("bulk")
    try:
        yield
    finally:
        _lane.reset(tok)


def limits() -> dict[str, Any]:
    """The plan's caps, each overridable in Settings (0 = the plan's figure)."""
    from app.config import get_settings

    st = get_settings()
    plan = str(getattr(st, "FYERS_PLAN", "standard") or "standard").lower()
    base = PLANS.get(plan, PLANS["standard"])
    out: dict[str, Any] = {"plan": plan}
    for k, name in (("per_sec", "FYERS_DATA_PER_SEC"), ("per_min", "FYERS_DATA_PER_MIN"),
                    ("per_day", "FYERS_DATA_PER_DAY"), ("ws_symbols", "FYERS_WS_SYMBOLS")):
        v = int(getattr(st, name, 0) or 0)
        out[k] = v if v > 0 else base[k]
    out["reserve_pct"] = max(0.0, min(80.0, float(getattr(st, "FYERS_DATA_RESERVE_PCT", 20.0) or 0.0)))
    return out


def ws_split() -> tuple[int, int]:
    """(the app's own price feed, the order-flow collector) of the plan's
    data-socket symbols. The app's feed — open positions' exits, the Trade
    page, the index ticker — comes first: a tenth of the cap, at least 60.
    Standard (100): 60 / 40, under the collector's minimum, so it records
    nothing; Prime (5,000): 500 / 4,500."""
    total = int(limits()["ws_symbols"])
    app = min(total, max(60, total // 10))
    return app, total - app


def _day(now: float) -> int:
    return int((now + IST) // 86400)


class DataBudget:
    def __init__(self, state: Optional[Path] = None) -> None:
        self.state = state or STATE
        self._sec: deque[float] = deque()
        self._min: deque[tuple[float, str]] = deque()
        self._day = -1
        self._shared = 0            # today's count in the shared file at the last fold
        self._mine = 0              # this process's calls since then
        self._synced = 0.0
        self._penalty_until = 0.0
        self.refused = 0            # bulk calls refused today (day budget)
        self.waited_s = 0.0

    # -- the shared day count ---------------------------------------------

    def _fold(self, now: float) -> None:
        """Add this process's calls to the shared file and read everyone's total."""
        day = _day(now)
        try:
            self.state.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.state, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                try:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX)
                except ImportError:      # pragma: no cover — Windows dev boxes: unlocked, best effort
                    pass
                raw = os.read(fd, 4096)
                try:
                    st = json.loads(raw.decode() or "{}")
                except ValueError:
                    st = {}
                used = int(st.get("used", 0)) if int(st.get("day", -1)) == day else 0
                if self._day == day:
                    used += self._mine
                os.lseek(fd, 0, os.SEEK_SET)
                os.ftruncate(fd, 0)
                os.write(fd, json.dumps({"day": day, "used": used}).encode())
            finally:
                os.close(fd)
        except OSError as e:
            log.warning("fyers.data_budget.state_failed", error=str(e)[:200])
            used = (self._shared + self._mine) if self._day == day else 0
        if self._day != day:
            self.refused = 0
        self._day, self._shared, self._mine, self._synced = day, used, 0, now

    def used_today(self, now: Optional[float] = None) -> int:
        now = now or time.time()
        if _day(now) != self._day:
            self._fold(now)
        return self._shared + self._mine

    # -- slots -------------------------------------------------------------

    def _wait_s(self, now: float, lane: str, lim: dict[str, Any]) -> float:
        while self._sec and now - self._sec[0] >= 1.0:
            self._sec.popleft()
        while self._min and now - self._min[0][0] >= 60.0:
            self._min.popleft()
        per_min = lim["per_min"]
        if lane == "bulk":
            if now < self._penalty_until:
                return self._penalty_until - now
            per_min = max(1, int(per_min * (100 - lim["reserve_pct"]) / 100))
            in_min = sum(1 for _, ln in self._min if ln == "bulk")
            if in_min >= per_min:
                return 60.0 - (now - next(t for t, ln in self._min if ln == "bulk")) + 0.01
        if len(self._min) >= lim["per_min"]:
            return 60.0 - (now - self._min[0][0]) + 0.01
        if len(self._sec) >= lim["per_sec"]:
            return 1.0 - (now - self._sec[0]) + 0.01
        return 0.0

    async def acquire(self) -> None:
        """Wait for a data-call slot in the caller's lane; the bulk lane is
        refused (DataBudgetExhausted) once its share of the day is used."""
        lane = _lane.get()
        lim = limits()
        while True:
            now = time.time()
            if _day(now) != self._day or now - self._synced >= SYNC_S:
                self._fold(now)
            if lane == "bulk":
                cap = int(lim["per_day"] * (100 - lim["reserve_pct"]) / 100)
                if self._shared + self._mine >= cap:
                    self.refused += 1
                    raise DataBudgetExhausted(
                        f"Fyers data budget for automations used up today ({cap:,} of {lim['per_day']:,} calls; "
                        f"{lim['reserve_pct']:.0f}% kept for the Trade page and orders)")
            wait = self._wait_s(now, lane, lim)
            if wait <= 0:
                self._sec.append(now)
                self._min.append((now, lane))
                self._mine += 1
                return
            self.waited_s += wait
            await asyncio.sleep(min(wait, 5.0))

    def rate_limited(self) -> None:
        """Fyers said 429: hold the bulk lane back for a while."""
        self._penalty_until = time.time() + PENALTY_S

    # -- planning ------------------------------------------------------------

    def bulk_left_today(self, now: Optional[float] = None) -> int:
        lim = limits()
        cap = int(lim["per_day"] * (100 - lim["reserve_pct"]) / 100)
        return max(0, cap - self.used_today(now))

    def stats(self, now: Optional[float] = None) -> dict[str, Any]:
        now = now or time.time()
        lim = limits()
        used = self.used_today(now)
        in_min = sum(1 for t, _ in self._min if now - t < 60)
        return {**lim, "used_today": used, "bulk_left_today": self.bulk_left_today(now),
                "last_minute": in_min, "refused_today": self.refused, "waited_s": round(self.waited_s, 1)}


_budget: Optional[DataBudget] = None


def budget() -> DataBudget:
    global _budget
    if _budget is None:
        _budget = DataBudget()
    return _budget
