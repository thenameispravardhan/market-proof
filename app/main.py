"""FastAPI application entrypoint.

Run locally:
    uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

Run via this module:
    python -m app.main
"""
from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from typing import Awaitable, Callable

import uvicorn
from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app import __version__
from app.analyzer.service import Service as AnalyzerService
from app.api import (
    audit_log as audit_log_api,
    broker_accounts as broker_accounts_api,
    core as core_api,
    dataset as dataset_api,
    fyers_callback,
    health,
    market as market_api,
    metrics as metrics_api,
    model as model_api,
    notifications as notifications_api,
    options as options_api,
    orders as orders_api,
    outcomes as outcomes_api,
    positions as positions_api,
    prompts as prompts_api,
    risk as risk_api,
    rules as rules_api,
    search as search_api,
    settings_api,
    strategies as strategies_api,
    system as system_api,
    trading_mode,
    warehouse as warehouse_api,
    ws,
)
from app.config import get_settings
from app.db.init import init_db
from app.execution.manager import Manager as ExecutionManager
from app.execution.fyers_stream import FyersStreamManager
from app.execution.quote_feed import QuoteFeed
from app.execution.trade_manager import TradeManager
from app.logging_config import configure_logging, correlation_id_var, get_logger
from app.monitors.manager import MonitorManager
from app.notifications.manager import NotificationManager


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    log = get_logger("app.lifespan")

    log.info(
        "app.startup",
        version=__version__,
        trading_mode=settings.TRADING_MODE,
        bind_host=settings.BIND_HOST,
        bind_port=settings.BIND_PORT,
        testing=bool(settings.TESTING),
    )
    if settings.bind_public:
        log.warning(
            "app.public_bind",
            host=settings.BIND_HOST,
            msg=(
                "App is bound to a non-loopback interface with NO authentication. "
                "Any caller on the network can change TRADING_MODE and place trades. "
                "Bind to 127.0.0.1 unless you are behind a reverse proxy with auth."
            ),
        )

    init_db()

    # Apply persisted UI/API setting overrides into the process env and
    # rebuild the Settings cache so saved tweaks (poll interval, risk
    # params, portfolio value, trading mode) survive a restart and are
    # effective before any service reads them.
    try:
        from app.api.settings_api import apply_overrides_to_env
        from app.db.session import SessionLocal

        with SessionLocal() as _s:
            apply_overrides_to_env(_s)
        settings = get_settings()
    except Exception:  # noqa: BLE001
        log.exception("app.settings_override_apply_failed")

    # Seed the analyzer prompt templates + the default strategy/paper
    # account so a fresh install analyzes filings and can route paper
    # trades without manual setup. All seeders are idempotent.
    #
    # NOTE: we intentionally do NOT auto-seed the example signal rules
    # anymore. The operator builds their own rules in the Rules UI, and a
    # restart must never resurrect deleted rules. (Re-seed on demand with
    # `python -m scripts.seed_default_rules` if the starter set is wanted.)
    #
    # Prompts seed with overwrite=False: on a restart we only INSERT
    # templates that are missing and never touch existing rows, so an
    # operator's saved prompt edits survive a reboot. Resetting a prompt
    # to factory default is an explicit, per-template action (the Prompts
    # page "Reset to default" button), never something a restart does.
    try:
        from app.analyzer.default_rules import seed_default_paper_account
        from app.analyzer.prompts import seed_defaults
        from app.db.session import SessionLocal

        with SessionLocal() as _s:
            n_prompts = len(seed_defaults(_s, overwrite=False))
            acct_seeded = seed_default_paper_account(_s)
            _s.commit()
        log.info(
            "app.seed_defaults",
            prompts=n_prompts, paper_account=acct_seeded,
        )
    except Exception:  # noqa: BLE001
        log.exception("app.seed_defaults_failed")

    monitor_manager = MonitorManager()
    analyzer_service = AnalyzerService()
    execution_manager = ExecutionManager()
    notification_manager = NotificationManager()
    # Phase 4: passive signals.new → signal_outcomes recorder (price at
    # signal, +5m, +30m). Pure telemetry — no trading influence.
    from app.services.outcome_logger import OutcomeLogger
    outcome_logger = OutcomeLogger()
    # Dataset builder: enriches each signal_outcomes row with 1-minute
    # candle reaction features + horizon targets once the reaction
    # window has elapsed. Passive telemetry; exposed on app.state so
    # POST /api/dataset/backfill reuses the same instance (its run lock
    # keeps the periodic loop and manual backfills from overlapping).
    from app.services.dataset_builder import DatasetBuilder
    dataset_builder = DatasetBuilder()
    app.state.dataset_builder = dataset_builder
    # Quote feed + trade manager share the execution manager's market
    # data bus and paper backend so entries, exits and P&L all see the
    # same prices.
    async def _real_quote(symbol: str):
        """REAL market price for the quote feed (used in BOTH paper and
        live mode so paper-order fills are realistic, not synthetic).

        WebSocket-FIRST: ask the realtime stream for the price (it serves
        from its live cache if the symbol is already streaming, else
        subscribes on demand and waits briefly for the first tick). Only
        when the socket can't deliver — not connected, or no tick in time —
        do we fall back to a one-shot REST quote. This is what keeps the
        per-symbol /data/quotes calls from coming back for symbols the
        socket can serve. Returns None on any miss so the feed keeps the
        last price / simulates."""
        # WS-first.
        if fyers_stream is not None:
            try:
                price = await fyers_stream.get_live_price(symbol)
                if price is not None and price > 0:
                    return price
            except Exception:  # noqa: BLE001
                pass
        # REST fallback. Resolve bare short-name -> full Fyers symbol
        # (shared with the streaming feed so both resolve identically).
        from app.execution.symbols import resolve_fyers_symbol

        full = resolve_fyers_symbol(symbol) or symbol
        try:
            from app.api.market import fetch_quote

            q = await fetch_quote(full)
            if q and q.get("last_price"):
                return float(q["last_price"])
        except Exception:  # noqa: BLE001
            pass
        return None

    quote_feed = QuoteFeed(
        market_data=execution_manager.market_data,
        live_quote_fn=_real_quote,
    )
    trade_manager = TradeManager(
        market_data=execution_manager.market_data,
        quote_feed=quote_feed,
        paper_backend=execution_manager.paper_backend,
    )
    execution_manager.attach_quote_feed(quote_feed)
    # Exit routing: LIVE positions flatten at the broker (marketable
    # limit → market fallback) before settling locally; paper settles
    # directly. The reconciliation loop compares the Fyers book with the
    # managed book so manual closes in the Fyers app / margin-call
    # square-offs surface as CLOSED_EXTERNAL instead of ghost positions.
    trade_manager.attach_exit_router(execution_manager.exit_backend_for_account)

    async def _live_positions():
        from app.api.market import _fyers_backend

        b = _fyers_backend()
        if b is None:
            return None
        try:
            return await b.get_positions()
        except Exception:  # noqa: BLE001
            return None

    trade_manager.attach_live_positions_provider(_live_positions)
    # Live volatility feeds (Phase 5): ATR from Fyers history candles and
    # India VIX from the index quote, both fail-safe. Until a Fyers account
    # is connected the fetches return empty/None and the risk layer falls
    # back to the % stop / no VIX gate. The bare symbol is resolved to the
    # full Fyers symbol for the candle fetch (same resolver the quote feed
    # uses); Fyers stays the only price source (no public-feed fallback).
    from app.api.market import fetch_history as _fetch_history
    from app.api.market import fetch_quote as _fetch_quote
    from app.execution.symbols import resolve_fyers_symbol as _resolve_sym
    from app.risk import volatility as _volatility

    async def _candle_fetch(symbol: str, resolution: str, days: int):
        full = _resolve_sym(symbol) or symbol
        return await _fetch_history(full, resolution, days)

    async def _vix_fetch():
        q = await _fetch_quote("NSE:INDIAVIX-INDEX")
        return float(q["last_price"]) if q and q.get("last_price") else None

    execution_manager.attach_volatility_providers(
        vol_provider=_volatility.FyersCandleVolatilityProvider(
            _candle_fetch, period=settings.ATR_PERIOD,
        ),
        vol_regime=_volatility.FyersVolatilityRegime(_vix_fetch),
    )
    # Live funds feed: in LIVE mode position sizing anchors to the REAL
    # Fyers account balance instead of the static PORTFOLIO_VALUE.
    # Fail-safe — no connected account (or a failing call) → None → the
    # risk layer falls back to the PORTFOLIO_VALUE ledger.
    from app.api.market import fetch_funds as _fetch_funds
    from app.risk import position_sizer as _position_sizer

    execution_manager.attach_funds_provider(
        _position_sizer.FyersFundsProvider(_fetch_funds)
    )
    # Realtime Fyers feed: data socket streams live prices into the same
    # market-data bus (so QuoteFeed can skip REST polling for streamed
    # symbols → no more /quotes 429s), order socket tracks fills in real
    # time. No-op until a live Fyers account is connected.
    from app.api.market import INDICES as _INDICES

    fyers_stream = FyersStreamManager(
        market_data=execution_manager.market_data,
        quote_feed=quote_feed,
        # Keep the index symbols streaming all session so the status-bar
        # ticker is sub-second, not REST-polled.
        always_subscribe=[idx["symbol"] for idx in _INDICES],
    )

    # Bridge every market-data tick onto the /ws event bus so the dashboard
    # gets sub-second price pushes instead of polling REST. Publishing to a
    # channel with no subscribers is a cheap no-op, so this stays idle until
    # a browser subscribes to the "quotes" channel.
    from app.services.event_bus import event_bus as _event_bus

    async def _quote_sink(q) -> None:
        await _event_bus.publish(
            "quotes",
            {
                "symbol": q.symbol,
                "last_price": q.last_price,
                "bid": q.bid,
                "ask": q.ask,
                "volume": q.volume,
                "change": q.change,
                "change_pct": q.change_pct,
                "prev_close": q.prev_close,
                "source": (q.extra or {}).get("source"),
                "simulated": bool((q.extra or {}).get("simulated")),
                "ts": q.timestamp.isoformat(),
            },
        )

    execution_manager.market_data.set_quote_sink(_quote_sink)
    # Expose the trade manager so the positions API can close trades,
    # AND the execution manager itself so the manual-trade orders API
    # (and tests) can grab it via `app.state.execution_manager`.
    app.state.trade_manager = trade_manager
    app.state.quote_feed = quote_feed
    app.state.fyers_stream = fyers_stream
    app.state.execution_manager = execution_manager
    # Only created when not TESTING; the shutdown path guards on None so
    # it never depends on re-evaluating settings.TESTING to match startup.
    risk_monitor_task: asyncio.Task[None] | None = None
    dataset_eod_task: asyncio.Task[None] | None = None
    if not settings.TESTING:
        # T3: start the analyzer before the monitors so its event-bus
        # subscription is live before the first `announcements.new`
        # event fires. Without it the pipeline dead-ends at the
        # announcements table.
        analyzer_service.start()
        await analyzer_service.wait_until_ready()
        # Don't start network monitors in test mode — they would
        # hit real exchanges. Tests that need the loop drive
        # `MonitorManager` with stubbed fetchers instead.
        await monitor_manager.start()
        # T4: start the execution manager (paper backend + signal
        # loop). Tests that need a managed lifecycle drive
        # `ExecutionManager` directly.
        execution_manager.start()
        # Trade management: the quote feed keeps prices flowing; the
        # trade manager exits positions on SL / target and computes
        # realised P&L.
        quote_feed.start()
        trade_manager.start()
        await trade_manager.wait_until_ready()
        # Realtime Fyers WebSocket feed (prices + order fills). Lazily
        # connects once a live account is present; toggled off via
        # FYERS_STREAMING_ENABLED to fall back to pure REST polling.
        if settings.FYERS_STREAMING_ENABLED:
            fyers_stream.start()
        # T6: start the notification manager — it subscribes to the
        # event bus and fans events out to operator-curated channels
        # (Telegram, etc.).
        notification_manager.start()
        # Phase 4 outcome logger — subscribe before signals start flowing.
        outcome_logger.start()
        # Dataset builder — periodic 1-min-candle enrichment batches.
        dataset_builder.start()
        # Daily health report — fires once per IST day at
        # HEALTH_REPORT_TIME_IST (default 15:45) on the `system.report`
        # channel; operators subscribe a notification channel with the
        # "report" event type. No-op under TESTING.
        from app.services.health_report import HealthReportService

        health_report_service = HealthReportService(
            market_data=execution_manager.market_data
        )
        health_report_service.start()
        app.state.health_report_service = health_report_service

        # Portfolio circuit-breaker monitor (RISK.md §4): periodically
        # rolls the day/week/month equity anchors, trips the daily /
        # weekly / monthly breakers, and flattens everything on a halt.
        # The EOD square-off itself runs inside the TradeManager sweep.
        async def _risk_monitor() -> None:
            from app.db.session import SessionLocal
            from app.risk import circuit_breakers
            from app.services.event_bus import event_bus as _bus

            while True:
                try:
                    await asyncio.sleep(10.0)
                    with SessionLocal() as _s:
                        summary = circuit_breakers.evaluate_breakers(
                            _s, execution_manager.market_data
                        )
                    if summary.get("newly_tripped"):
                        await _bus.publish("breaker.tripped", summary)
                    if summary.get("flatten_required"):
                        log.warning("risk_monitor.flatten", reason=summary.get("disabled_reason"))
                        await trade_manager.close_all(reason="CIRCUIT_BREAKER")
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    log.exception("risk_monitor.tick_failed")

        risk_monitor_task = asyncio.create_task(_risk_monitor(), name="risk-monitor")

        # Dataset completion (once per trading day, after the close).
        # Announcements land with only the columns known at arrival; the
        # price/label/metadata columns the model trains on need the day's
        # candles, which do not exist until the session ends. Off by default
        # — it is new behaviour and costs one Fyers history call per symbol.
        async def _dataset_eod() -> None:
            from app.risk.market_clock import _is_trading_day, _parse_hhmm, to_ist
            from app.services.candle_sync import run_eod, run_incremental

            done_for: Optional[str] = None
            last_incr = 0.0
            while True:
                try:
                    await asyncio.sleep(60.0)
                    s = get_settings()
                    if not s.DATASET_EOD_ENABLED:
                        continue
                    ist = to_ist(None)
                    day = ist.date().isoformat()

                    # Real-time pass. A filing cannot be fully priced until 60
                    # minutes of trading have passed, so this is as live as the
                    # data allows: the moment px_60m exists, the row is filled.
                    now = asyncio.get_running_loop().time()
                    if now - last_incr >= s.DATASET_AUTOFILL_MINUTES * 60:
                        last_incr = now
                        log.info("dataset_autofill.done", **{
                            k: str(v)[:120]
                            for k, v in (await run_incremental()).items()})

                    # Daily sweep: the wider window, plus the AI labels and
                    # metadata that the light pass does not chase.
                    if done_for == day or not _is_trading_day(ist):
                        continue
                    if ist.time() < _parse_hhmm(s.DATASET_EOD_TIME_IST):
                        continue
                    done_for = day          # set before the run: a failure
                    log.info("dataset_eod.start", day=day)   # must not retry-loop
                    log.info("dataset_eod.done", **{
                        k: str(v)[:120] for k, v in (await run_eod()).items()})
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — never kill the loop
                    log.exception("dataset_eod.failed")

        dataset_eod_task = asyncio.create_task(_dataset_eod(), name="dataset-eod")

    try:
        yield
    finally:
        if not settings.TESTING:
            # Stop the breaker monitor first.
            for _t in (risk_monitor_task, dataset_eod_task):
                if _t is not None:
                    _t.cancel()
                    try:
                        await _t
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
            # Stop the producers first so consumers drain cleanly.
            await monitor_manager.stop()
            analyzer_service.stop()
            # Stop the realtime socket feed before the quote feed so it
            # stops publishing into the bus first.
            fyers_stream.stop()
            trade_manager.stop()
            quote_feed.stop()
            execution_manager.stop()
            notification_manager.stop()
            outcome_logger.stop()
            dataset_builder.stop()
            health_report_service.stop()
            await dataset_builder.wait_until_stopped()
            await health_report_service.wait_until_stopped()
            await analyzer_service.wait_until_stopped()
            await fyers_stream.wait_until_stopped()
            await trade_manager.wait_until_stopped()
            await quote_feed.wait_until_stopped()
            await execution_manager.wait_until_stopped()
            await notification_manager.wait_until_stopped()
            await outcome_logger.wait_until_stopped()
            await analyzer_service.aclose()
        log.info("app.shutdown")


app = FastAPI(
    title="AI News Trading Bot",
    version=__version__,
    description="Local-first AI-powered news trading bot (T1 infrastructure).",
    lifespan=lifespan,
    # OpenAPI is fine; we explicitly do not add auth.
)


# -------------------------------------------------------------------------
# Middleware: correlation IDs + access log
# -------------------------------------------------------------------------


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    HEADER = "x-request-id"

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        cid = request.headers.get(self.HEADER) or uuid.uuid4().hex
        token = correlation_id_var.set(cid)
        start = time.perf_counter()
        log = get_logger("app.http")
        try:
            response = await call_next(request)
        except Exception:
            log.exception(
                "http.unhandled",
                method=request.method,
                path=request.url.path,
            )
            raise
        finally:
            correlation_id_var.reset(token)
        elapsed_ms = (time.perf_counter() - start) * 1000
        response.headers[self.HEADER] = cid
        log.info(
            "http.request",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            elapsed_ms=round(elapsed_ms, 2),
        )
        return response


app.add_middleware(CorrelationIdMiddleware)


# -------------------------------------------------------------------------
# Routers
# -------------------------------------------------------------------------


app.include_router(health.router)
app.include_router(settings_api.router)
app.include_router(trading_mode.router)
app.include_router(fyers_callback.router)
app.include_router(ws.router)
app.include_router(notifications_api.router)
app.include_router(prompts_api.router)
app.include_router(rules_api.router)
app.include_router(strategies_api.router)
app.include_router(broker_accounts_api.router)
app.include_router(audit_log_api.router)
app.include_router(positions_api.router)
app.include_router(risk_api.router)
app.include_router(market_api.router)
app.include_router(core_api.router)
app.include_router(orders_api.router)
app.include_router(search_api.router)
app.include_router(options_api.router)
app.include_router(metrics_api.router)
app.include_router(outcomes_api.router)
app.include_router(dataset_api.router)
app.include_router(warehouse_api.router)
app.include_router(model_api.router)
app.include_router(system_api.router)


# -------------------------------------------------------------------------
# Static frontend (built by `npm run build` in ./frontend).
# If the dist/ directory exists, serve it at /; otherwise the API still
# works and /docs is still useful, but the dashboard is missing.
# -------------------------------------------------------------------------


from pathlib import Path  # noqa: E402

from fastapi.staticfiles import StaticFiles  # noqa: E402

_DIST_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _DIST_DIR.is_dir() and (_DIST_DIR / "index.html").is_file():
    # Serve assets (JS/CSS/images) at /assets/*.
    app.mount(
        "/assets",
        StaticFiles(directory=str(_DIST_DIR / "assets")),
        name="frontend-assets",
    )

    # Catch-all: anything that isn't an API route returns the SPA shell.
    # The API routers (mounted above) take precedence because they're
    # matched before this catch-all in Starlette's routing order.
    @app.get("/", include_in_schema=False)
    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_shell(full_path: str = "") -> object:  # noqa: ARG001
        index = _DIST_DIR / "index.html"
        if not index.is_file():
            return {"detail": "frontend not built"}
        from fastapi.responses import FileResponse
        return FileResponse(str(index))
else:
    log_path_msg = f"no static frontend at {_DIST_DIR} (run scripts/run.sh to build)"
    print(f"[startup] {log_path_msg}")


# -------------------------------------------------------------------------
# Entrypoint
# -------------------------------------------------------------------------


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.BIND_HOST,
        port=settings.BIND_PORT,
        log_level=settings.LOG_LEVEL.lower(),
        # We do our own JSON logging via structlog, so disable uvicorn's
        # default access log to avoid double-logging.
        access_log=False,
        reload=False,
    )


if __name__ == "__main__":
    main()
