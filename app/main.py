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
    backtest,
    broker_accounts as broker_accounts_api,
    core as core_api,
    fyers_callback,
    health,
    notifications as notifications_api,
    positions as positions_api,
    prompts as prompts_api,
    rules as rules_api,
    settings_api,
    strategies as strategies_api,
    trading_mode,
    webhooks as webhooks_api,
    ws,
)
from app.config import get_settings
from app.db.init import init_db
from app.execution.manager import Manager as ExecutionManager
from app.execution.quote_feed import QuoteFeed
from app.execution.trade_manager import TradeManager
from app.logging_config import configure_logging, correlation_id_var, get_logger
from app.monitors.manager import MonitorManager
from app.notifications.manager import NotificationManager
from app.webhooks.dispatcher import WebhookDispatcher


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

    # Seed the analyzer prompt templates and the default signal rules so
    # a fresh install analyzes filings and can actually trade in paper
    # mode without any manual setup. Both seeders are idempotent.
    try:
        from app.analyzer.default_rules import (
            seed_default_paper_account,
            seed_default_rules,
        )
        from app.analyzer.prompts import seed_defaults
        from app.db.session import SessionLocal

        with SessionLocal() as _s:
            n_prompts = len(seed_defaults(_s))
            n_rules = seed_default_rules(_s)
            acct_seeded = seed_default_paper_account(_s)
            _s.commit()
        log.info(
            "app.seed_defaults",
            prompts=n_prompts, rules=n_rules, paper_account=acct_seeded,
        )
    except Exception:  # noqa: BLE001
        log.exception("app.seed_defaults_failed")

    monitor_manager = MonitorManager()
    analyzer_service = AnalyzerService()
    execution_manager = ExecutionManager()
    notification_manager = NotificationManager()
    webhook_dispatcher = WebhookDispatcher()
    # Quote feed + trade manager share the execution manager's market
    # data bus and paper backend so entries, exits and P&L all see the
    # same prices.
    quote_feed = QuoteFeed(market_data=execution_manager.market_data)
    trade_manager = TradeManager(
        market_data=execution_manager.market_data,
        quote_feed=quote_feed,
        paper_backend=execution_manager.paper_backend,
    )
    execution_manager.attach_quote_feed(quote_feed)
    # Expose the trade manager so the positions API can close trades.
    app.state.trade_manager = trade_manager
    app.state.quote_feed = quote_feed
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
        # T6: start the notification + outbound webhook managers.
        # Both subscribe to the event bus; the dispatcher POSTs to
        # registered webhook URLs, the notification manager fans
        # events out to operator-curated channels (Telegram, etc.).
        notification_manager.start()
        webhook_dispatcher.start()

    try:
        yield
    finally:
        if not settings.TESTING:
            # Stop the producers first so consumers drain cleanly.
            await monitor_manager.stop()
            analyzer_service.stop()
            trade_manager.stop()
            quote_feed.stop()
            execution_manager.stop()
            notification_manager.stop()
            webhook_dispatcher.stop()
            await analyzer_service.wait_until_stopped()
            await trade_manager.wait_until_stopped()
            await quote_feed.wait_until_stopped()
            await execution_manager.wait_until_stopped()
            await notification_manager.wait_until_stopped()
            await webhook_dispatcher.wait_until_stopped()
            await analyzer_service.aclose()
            await webhook_dispatcher.aclose()
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
app.include_router(backtest.router)
app.include_router(notifications_api.router)
app.include_router(webhooks_api.router)
app.include_router(prompts_api.router)
app.include_router(rules_api.router)
app.include_router(strategies_api.router)
app.include_router(broker_accounts_api.router)
app.include_router(audit_log_api.router)
app.include_router(positions_api.router)
app.include_router(core_api.router)


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
