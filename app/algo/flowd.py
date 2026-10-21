"""The order-flow collector: its own process, its own Fyers data socket.

Recording thousands of symbols costs about a CPU core (the Fyers SDK decodes
every frame in Python), so it runs beside the app rather than inside it: a
slow moment or a crash here can't stall order handling, the live price feed
or the automations' exits, and it runs at a lower CPU priority. The app
starts and watches it (`Supervisor`); the two share only files under
data/ticks/ — config.json in, plan.json / status.json / stats.json and the
recordings out.

What it does, all day:
  * plans the universe (flow_universe.build) at start, every 5 minutes in
    the session (ATM strikes re-centre, new expiries / rolls are picked up)
    and when the config changes;
  * subscribes the plan on its socket — frames are classified on the socket
    thread (ticks.TickRecorder) and appended to today's files every 5 s;
  * watches its own load: when it can't keep up (CPU pinned or frames
    arriving late) it sheds the lowest-priority symbols, and adds them back
    once it has been healthy for half an hour;
  * after 16:10 compacts the day into days/<YYYYMMDD>/ and drops the oldest
    collected day beyond `retention_days` (90 by default).
  * guards the disk: below `min_free_gb` free it drops the oldest days early
    (keeping the newest 5), and pauses recording below half of it;
  * if Fyers refuses symbols for the plan's symbol cap, it cuts its own budget.

    python -m app.algo.flowd      # what the app starts; runs until disabled or stopped
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Any, Optional

from app.algo import flow_universe as fu
from app.algo import ticks
from app.logging_config import get_logger

log = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SESSION_PLAN_S = 300            # re-plan cadence in the session (ATM drift)
IDLE_PLAN_S = 1800              # ... outside it
FLUSH_S = 5
MIN_BUDGET = 50
DISK_CHECK_S = 60
DISK_KEEP_DAYS = 5              # the disk guard never drops the newest days


class _Token:
    """The minimal 'backend' FyersStreamManager reads: the socket token."""

    def __init__(self, token: str) -> None:
        self.ws_access_token = token


def _account() -> Optional[tuple[str, str, int, str]]:
    """(app_id, access_token, account id, name) of the connected live Fyers account."""
    try:
        from sqlalchemy import select

        from app.config import get_settings
        from app.db.models import BrokerAccount
        from app.db.session import SessionLocal

        with SessionLocal() as db:
            acc = db.execute(select(BrokerAccount).where(
                BrokerAccount.broker == "fyers", BrokerAccount.paper_mode == False,  # noqa: E712
                BrokerAccount.access_token.is_not(None)).order_by(BrokerAccount.id.asc())).scalars().first()
            if acc is None:
                return None
            app_id = (get_settings().FYERS_APP_ID or "").strip() or (acc.app_id or "")
            if not app_id or not acc.access_token:
                return None
            return app_id, acc.access_token, int(acc.id), acc.name
    except Exception as e:  # noqa: BLE001 — retried on the next pass
        log.warning("flowd.account_lookup_failed", error=str(e)[:200])
        return None


class Collector:
    def __init__(self, root: Path = ticks.ROOT) -> None:
        self.root = root
        self.cfg = ticks.load_config(root)
        self.cfg_mtime = self._cfg_mtime()
        self.rec = ticks.TickRecorder(root)
        self.symbols: list[str] = []
        self.plan_summary: dict[str, Any] = {}
        self.plan_at = 0.0
        self.prev_sel: dict[str, list[float]] = {}
        self.sel_day: Optional[int] = None
        self.lists: dict[str, tuple[list[str], str]] = {}
        self.lists_at = 0.0
        self.quotes: dict[str, tuple[float, float]] = {}       # full -> (price, at): REST spot fallback
        self.active_budget = int(self.cfg.get("budget") or ticks.DEFAULT_BUDGET)
        self.budget_note: Optional[str] = None
        self.started = time.time()
        self.errors: list[str] = []
        self.stream: Any = None
        self._acct: tuple[float, Optional[tuple]] = (0.0, None)
        self._rest: tuple[Optional[str], Any] = (None, None)          # (token, FyersLiveBackend) for REST quotes
        self._cpu = (time.process_time(), time.monotonic())
        self._frames = (0, time.monotonic())
        self.cpu_pct = 0.0
        # one core's worth on a multi-core box; half the only core on a single-core one
        self.cpu_cap = 90.0 if (os.cpu_count() or 1) >= 2 else 50.0
        self.fps = 0.0
        self.lag = (0.0, 0.0)
        self._strikes = 0
        self._healthy_since = time.time()
        self._compacted: Optional[int] = None
        self._day = ticks.ist_day(time.time())
        self.cap: Optional[int] = None              # Fyers refused more than this many: no regrowing past it today
        self._limit_hits = 0
        self.free_gb: Optional[float] = None
        self.disk_note: Optional[str] = None
        self.paused = False                         # the disk guard stopped recording
        self._disk_at = 0.0
        self._stop = asyncio.Event()

    # -- plumbing ------------------------------------------------------------

    def _cfg_mtime(self) -> float:
        try:
            return (self.root / "config.json").stat().st_mtime
        except OSError:
            return 0.0

    def account(self) -> Optional[tuple]:
        if time.time() - self._acct[0] > 30:
            self._acct = (time.time(), _account())
        return self._acct[1]

    def backend(self) -> Optional[_Token]:
        a = self.account()
        return _Token(f"{a[0]}:{a[1]}") if a else None

    def spot(self, full: str) -> Optional[float]:
        px = self.rec.ltp.get(full.upper())
        if px:
            return px
        q = self.quotes.get(full.upper())
        return q[0] if q and time.time() - q[1] < 900 else None

    def note_error(self, text: str) -> None:
        self.errors = (self.errors + [f"{time.strftime('%H:%M:%S')} {text}"[:300]])[-20:]

    async def refresh_quotes(self, fulls: list[str]) -> None:
        """Last prices over REST for underlyings the socket hasn't priced yet
        (before the open, or just subscribed): 50 a call."""
        need = [f for f in dict.fromkeys(fulls) if not self.spot(f)]
        a = self.account()
        if not need or not a:
            return
        try:
            from app.execution.fyers_live import FyersLiveBackend

            if self._rest[0] != a[1]:
                if self._rest[1] is not None:
                    await self._rest[1].aclose()
                self._rest = (a[1], FyersLiveBackend(app_id=a[0], access_token=a[1], broker_account_id=a[2],
                                                     account_name=a[3]))
            be = self._rest[1]
            for i in range(0, len(need), 50):
                for q in await be.get_quote(need[i:i + 50]):
                    if q.last_price:
                        self.quotes[q.symbol.upper()] = (float(q.last_price), time.time())
                await asyncio.sleep(0.25)               # well inside Fyers' 10 requests / s
        except Exception as e:  # noqa: BLE001
            self.note_error(f"quotes: {e}")
            log.warning("flowd.quotes_failed", error=str(e)[:200])

    # -- the plan ----------------------------------------------------------------

    async def replan(self, now: float) -> None:
        cfg = self.cfg
        day = ticks.ist_day(now)
        if self.paused:                            # the disk guard: the socket lets everything go
            self.symbols = []
            self.plan_summary = {**self.plan_summary, "used": 0}
            self.plan_at = now
            return
        if self.sel_day != day:
            self.prev_sel, self.sel_day = {}, day
        if time.time() - self.lists_at > 6 * 3600 or not self.lists:
            names = [n for n in cfg["universe"].get("lists") or [] if n in fu.LISTS]
            self.lists = {n: await asyncio.to_thread(fu.index_list, n, self.root / "universe") for n in names}
            self.lists_at = time.time()
        fo = await asyncio.to_thread(fu.contracts)
        stocks = {s for n in self.lists.values() for s in n[0]}
        under = [fu.INDEX_SYMBOL[n] for n in fu.INDEX_SYMBOL if n in fo]
        under += [f"NSE:{n}-EQ" for n in stocks if n in fo and fo[n]["fut"]]
        await self.refresh_quotes(under)
        focus = list(cfg.get("symbols") or []) + ticks.strategy_symbols(self.root)
        plan = await asyncio.to_thread(fu.build, cfg, focus, self.spot, now, self.prev_sel, fo, self.lists, None,
                                       self.active_budget)
        self.prev_sel = plan.opt_sel
        self.rec.key_of = {**self.rec.key_of, **plan.keys}
        self.rec.raw_keys = {plan.keys[s] for s in plan.symbols if plan.meta[plan.keys[s]]["cls"] == "focus"}
        self.rec.write_meta(plan.meta, day)
        self.symbols = plan.symbols
        self.plan_summary = plan.summary()
        self.plan_at = now
        ticks.write_json(self.root / "plan.json", {"at": now, **self.plan_summary,
                                                   "symbols": [[s, plan.keys[s], plan.meta[plan.keys[s]]["cls"]]
                                                               for s in plan.symbols]})
        log.info("flowd.planned", used=len(plan.symbols), budget=plan.budget, classes=plan.classes)

    def plan_due(self, now: float) -> bool:
        tod = (now + ticks.IST) % 86400
        weekday = ((ticks.ist_day(now) + 3) % 7) < 5
        session = weekday and 8 * 3600 + 50 * 60 <= tod <= ticks.CLOSE_S
        return now - self.plan_at >= (SESSION_PLAN_S if session else IDLE_PLAN_S)

    # -- load ------------------------------------------------------------------------

    def check_load(self, now: float) -> bool:
        """Once a minute: CPU share and frame delay; True when the plan must shrink / may grow."""
        pt, mono = time.process_time(), time.monotonic()
        dt = mono - self._cpu[1]
        if dt < 60:
            return False
        self.cpu_pct = round((pt - self._cpu[0]) / dt * 100, 1)
        self._cpu = (pt, mono)
        frames = self.rec.frames
        self.fps = round((frames - self._frames[0]) / max(1e-6, mono - self._frames[1]), 1)
        self._frames = (frames, mono)
        lags = sorted(self.rec.lag)
        self.rec.lag = []
        if lags:
            self.lag = (round(lags[len(lags) // 2], 2), round(lags[int(len(lags) * 0.95)], 2))
        tod = (now + ticks.IST) % 86400
        session = ((ticks.ist_day(now) + 3) % 7) < 5 and ticks.OPEN_S <= tod < ticks.CLOSE_S
        over = session and (self.cpu_pct > self.cpu_cap or (lags and self.lag[1] > 10))
        self._strikes = self._strikes + 1 if over else max(0, self._strikes - 1)
        configured = int(self.cfg.get("budget") or ticks.DEFAULT_BUDGET)
        if self._strikes >= 3 and self.active_budget > MIN_BUDGET:
            self.active_budget = max(MIN_BUDGET, int(min(self.active_budget, len(self.symbols) or self.active_budget) * 0.8))
            self.budget_note = (f"cut to {self.active_budget} at {time.strftime('%H:%M')}: CPU {self.cpu_pct}%, "
                                f"frames {self.lag[1]}s late")
            self._strikes = 0
            self._healthy_since = now
            log.warning("flowd.overloaded_shedding", budget=self.active_budget, cpu=self.cpu_pct, lag_p95=self.lag[1])
            return True
        if over:
            self._healthy_since = now
        ceiling = min(configured, self.cap) if self.cap else configured
        if self.active_budget < ceiling and now - self._healthy_since > 1800:
            self.active_budget = min(ceiling, int(self.active_budget * 1.1) + 1)
            self.budget_note = f"raised back to {self.active_budget} at {time.strftime('%H:%M')}" \
                if self.active_budget < configured else None
            self._healthy_since = now
            return True
        return False

    def check_limits(self, now: float) -> bool:
        """Fyers refused a subscribe for the symbol cap: shrink to fit and stay there today."""
        hits = int(getattr(self.stream, "limit_hits", 0) or 0) if self.stream is not None else 0
        if hits <= self._limit_hits:
            return False
        self._limit_hits = hits
        held = len(getattr(self.stream, "_subscribed", {}) or {})
        self.active_budget = max(MIN_BUDGET, int(min(self.active_budget, len(self.symbols) or self.active_budget) * 0.8))
        if held:
            self.active_budget = max(MIN_BUDGET, min(self.active_budget, held))
        self.cap = self.active_budget
        self.budget_note = f"cut to {self.active_budget} at {time.strftime('%H:%M')}: Fyers refused more symbols"
        self._healthy_since = now
        log.warning("flowd.symbol_cap_shrinking", budget=self.active_budget, held=held,
                    error=getattr(self.stream, "limit_error", None))
        return True

    def check_disk(self, now: float) -> bool:
        """Once a minute: below `min_free_gb` free, the oldest collected days go
        early (the newest few stay); below half of it recording pauses until the
        space is back. True when the plan must change (pause / resume)."""
        if now - self._disk_at < DISK_CHECK_S:
            return False
        self._disk_at = now
        try:
            free = shutil.disk_usage(self.root).free / 1e9
        except OSError:
            return False
        floor = float(self.cfg.get("min_free_gb") or ticks.MIN_FREE_GB)
        self.free_gb = round(free, 1)
        if free >= floor:
            if self.paused:
                self.paused = False
                self.disk_note = None
                log.info("flowd.disk_ok_resuming", free_gb=self.free_gb)
                return True
            if self.disk_note and not self.disk_note.startswith("paused"):
                self.disk_note = None
            return False
        gone = self.rec.drop_oldest(DISK_KEEP_DAYS)
        if gone:
            self.disk_note = f"disk low ({free:.1f} GB free): dropped {gone} early"
        if not self.paused and free < floor / 2 and not gone:
            self.paused = True
            self.disk_note = f"paused: only {free:.1f} GB free (keeps {floor:g} GB free)"
            log.warning("flowd.disk_low_paused", free_gb=self.free_gb, floor=floor)
            return True
        return False

    # -- status ----------------------------------------------------------------------

    def write_status(self, state: str = "running") -> None:
        st = self.rec.stats
        totals = [0.0] * 5
        for v in list(st.values()):
            for i in range(5):
                totals[i] += v[i]
        ticks.write_json(self.root / "status.json", {
            "pid": os.getpid(), "state": state, "started": self.started, "heartbeat": time.time(),
            "stream": self.stream.status() if self.stream is not None else None,
            "frames": self.rec.frames, "fps": self.fps, "cpu_pct": self.cpu_pct,
            "lag_p50": self.lag[0], "lag_p95": self.lag[1],
            "budget": {"configured": int(self.cfg.get("budget") or ticks.DEFAULT_BUDGET), "active": self.active_budget,
                       "note": self.budget_note},
            "disk": {"free_gb": self.free_gb, "min_free_gb": float(self.cfg.get("min_free_gb") or ticks.MIN_FREE_GB),
                     "note": self.disk_note, "paused": self.paused},
            "plan": {"at": self.plan_at, **self.plan_summary},
            "today": {"keys": len(st), "ticks": totals[0], "trades": totals[1], "buy": totals[2], "sell": totals[3],
                      "unk": totals[4]},
            "errors": self.errors[-10:],
        })

    def write_stats(self) -> None:
        ticks.write_json(self.root / "stats.json", {k: [round(x, 2) if isinstance(x, float) else x for x in v]
                                                    for k, v in list(self.rec.stats.items())})

    # -- the day -----------------------------------------------------------------------

    async def end_of_day(self, now: float) -> None:
        day = ticks.ist_day(now)
        if day != self._day:                       # midnight passed: sweep whatever yesterday left
            self._day = day
            self.cap = None
            await asyncio.to_thread(self.rec.sweep, now)
            await asyncio.to_thread(self.rec.prune, int(self.cfg["retention_days"]), int(self.cfg["raw_days"]))
        if (now + ticks.IST) % 86400 // 60 >= ticks.COMPACT_AFTER_MIN and self._compacted != day:
            self._compacted = day
            await asyncio.to_thread(self.rec.flush)
            if await asyncio.to_thread(self.rec.compact, day):
                await asyncio.to_thread(self.rec.prune, int(self.cfg["retention_days"]), int(self.cfg["raw_days"]))

    async def run(self) -> None:
        from app.execution.fyers_stream import FyersStreamManager
        from app.execution.market_data import MarketDataBus

        try:
            os.nice(10)                            # the app's own work comes first
        except OSError:
            pass
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        log.info("flowd.start", pid=os.getpid(), budget=self.active_budget)
        try:
            await asyncio.to_thread(self.rec.migrate_legacy)
        except Exception as e:  # noqa: BLE001 — the old files stay; recording goes on
            self.note_error(f"migration: {e}")
            log.exception("flowd.migration_failed")
        await asyncio.to_thread(self.rec.sweep)
        await asyncio.to_thread(self.rec.prune, int(self.cfg["retention_days"]), int(self.cfg["raw_days"]))
        self.rec.resume()
        try:
            await self.replan(time.time())
        except Exception as e:  # noqa: BLE001
            self.note_error(f"plan: {e}")
            log.exception("flowd.plan_failed")
        self.stream = FyersStreamManager(market_data=MarketDataBus(), quote_feed=None, backend_provider=self.backend,
                                         record_symbols=lambda: self.symbols, orders=False)
        self.stream.tick_listeners.append(self.rec.on_tick)
        self.stream.connect_listeners.append(self.rec.mark_resync)
        self.stream.start()
        last_flush = last_status = last_stats = 0.0
        try:
            while not self._stop.is_set():
                now = time.time()
                try:
                    if self._cfg_mtime() != self.cfg_mtime:
                        self.cfg_mtime = self._cfg_mtime()
                        self.cfg = ticks.load_config(self.root)
                        if not self.cfg.get("enabled"):
                            break
                        self.active_budget = min(self.active_budget, int(self.cfg.get("budget") or ticks.DEFAULT_BUDGET)) \
                            if self.budget_note or self.cap else int(self.cfg.get("budget") or ticks.DEFAULT_BUDGET)
                        self._disk_at = 0.0
                        self.lists_at = 0.0
                        await self.replan(now)
                    if now - last_flush >= FLUSH_S:
                        last_flush = now
                        await asyncio.to_thread(self.rec.flush)
                    changed = self.check_load(now)
                    changed = self.check_limits(now) or changed
                    changed = self.check_disk(now) or changed
                    if changed or self.plan_due(now):
                        await self.replan(now)
                    await self.end_of_day(now)
                    if now - last_status >= 5:
                        last_status = now
                        self.write_status()
                    if now - last_stats >= 30:
                        last_stats = now
                        self.write_stats()
                except Exception as e:  # noqa: BLE001 — never die on one bad pass
                    self.note_error(str(e))
                    log.exception("flowd.iteration_failed")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.stream.stop()
            try:
                await asyncio.wait_for(self.stream.wait_until_stopped(), timeout=20)
            except asyncio.TimeoutError:
                pass
            await asyncio.to_thread(self.rec.flush)
            if self._rest[1] is not None:
                await self._rest[1].aclose()
            self.write_status("stopped")
            log.info("flowd.stop")


# ---- the app's side: start / watch / stop the process ---------------------------------

class Supervisor:
    """Runs `python -m app.algo.flowd` while the recorder is switched on;
    restarts it (backing off) if it exits, stops it when switched off."""

    def __init__(self, root: Path = ticks.ROOT, cmd: Optional[list[str]] = None, poll_s: float = 5.0) -> None:
        self.root = root
        self.cmd = cmd or [sys.executable, "-m", "app.algo.flowd"]
        self.poll_s = poll_s
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.backoff = 5.0
        self.next_start = 0.0
        self.started_at = 0.0
        self.restarts = 0
        self.last_exit: Optional[dict[str, Any]] = None

    def info(self) -> dict[str, Any]:
        return {"running": self.proc is not None and self.proc.returncode is None,
                "pid": self.proc.pid if self.proc is not None else None, "restarts": self.restarts,
                "last_exit": self.last_exit}

    async def _start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        logf = self.root / "collector.log"
        try:
            if logf.exists() and logf.stat().st_size > 50e6:
                logf.replace(self.root / "collector.log.1")
        except OSError:
            pass
        out = logf.open("ab")
        try:
            self.proc = await asyncio.create_subprocess_exec(*self.cmd, cwd=str(PROJECT_ROOT), stdout=out,
                                                             stderr=asyncio.subprocess.STDOUT)
        finally:
            out.close()
        self.started_at = time.time()
        log.info("flowd.supervisor_started", pid=self.proc.pid)

    async def stop(self) -> None:
        p = self.proc
        if p is None or p.returncode is not None:
            return
        try:
            p.terminate()
            await asyncio.wait_for(p.wait(), timeout=25)
        except asyncio.TimeoutError:
            p.kill()
            await p.wait()
        except ProcessLookupError:
            pass
        log.info("flowd.supervisor_stopped", rc=p.returncode)

    async def run(self) -> None:
        while True:
            try:
                enabled = bool(ticks.load_config(self.root).get("enabled"))
                p = self.proc
                if p is not None and p.returncode is not None:
                    self.last_exit = {"at": time.time(), "rc": p.returncode}
                    self.proc = None
                    if enabled:
                        self.restarts += 1
                        self.next_start = time.time() + self.backoff
                        self.backoff = min(300.0, self.backoff * 2)
                        log.warning("flowd.exited_restarting", rc=p.returncode, in_s=self.backoff)
                running = self.proc is not None
                if enabled and not running and time.time() >= self.next_start:
                    await self._start()
                elif not enabled and running:
                    await self.stop()
                elif running and time.time() - self.started_at > 600:
                    self.backoff = 5.0
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("flowd.supervisor_failed")
            await asyncio.sleep(self.poll_s)


def main() -> None:
    from app.config import get_settings
    from app.logging_config import configure_logging

    configure_logging(getattr(get_settings(), "LOG_LEVEL", "INFO"))
    cfg = ticks.load_config()
    if not cfg.get("enabled"):
        print(json.dumps({"event": "flowd.disabled"}), flush=True)
        return
    asyncio.run(Collector().run())


if __name__ == "__main__":
    main()
