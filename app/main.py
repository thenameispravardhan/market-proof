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
from app.api import (
    audit_log as audit_log_api,
    backtest,
    broker_accounts as broker_accounts_api,
    core as core_api,
    fyers_callback,
    health,
    notifications as notifications_api,
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

    monitor_manager = MonitorManager()
    execution_manager = ExecutionManager()
    notification_manager = NotificationManager()
    webhook_dispatcher = WebhookDispatcher()
    if not settings.TESTING:
        # Don't start network monitors in test mode — they would
        # hit real exchanges. Tests that need the loop drive
        # `MonitorManager` with stubbed fetchers instead.
        await monitor_manager.start()
        # T4: start the execution manager (paper backend + signal
        # loop). Tests that need a managed lifecycle drive
        # `ExecutionManager` directly.
        execution_manager.start()
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
            execution_manager.stop()
            notification_manager.stop()
            webhook_dispatcher.stop()
            await execution_manager.wait_until_stopped()
            await notification_manager.wait_until_stopped()
            await webhook_dispatcher.wait_until_stopped()
            await webhook_dispatcher.aclose()
            await monitor_manager.stop()
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
app.include_router(core_api.router)


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
