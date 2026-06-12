"""Backtest API — /api/backtest.

Endpoints (T7 spec):

  GET    /api/backtest/runs
  POST   /api/backtest/runs
  GET    /api/backtest/runs/{id}
  DELETE /api/backtest/runs/{id}
  GET    /api/backtest/runs/{id}/trades
  GET    /api/backtest/runs/{id}/equity-curve

`POST` creates a `backtest_runs` row with status=pending, kicks
the engine off in a background task, and returns the run id
immediately. The client polls `GET /api/backtest/runs/{id}` to
discover when status flips to `done` (or `failed`).

The engine itself is `app.backtest.engine.BacktestEngine` —
this router is the thinnest possible HTTP wrapper around it.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.backtest.engine import BacktestConfig, BacktestEngine, run_backtest
from app.db.init import init_db
from app.db.models import (
    BacktestRun,
    BrokerAccount,
    Strategy,
    Trade as TradeRow,
)
from app.db.session import get_db
from app.logging_config import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/backtest", tags=["backtest"])

# Make sure the backtest_runs table exists.
init_db()


# -------------------------------------------------------------------------
# Request / response schemas
# -------------------------------------------------------------------------


class BacktestCreate(BaseModel):
    name: Optional[str] = Field(None, description="Free-form label")
    strategy_id: Optional[int] = Field(None)
    broker_account_id: Optional[int] = Field(None)
    start_date: date
    end_date: date
    initial_capital: float = Field(100_000.0, gt=0)

    @field_validator("end_date")
    @classmethod
    def _dates_valid(cls, v: date, info) -> date:
        start = info.data.get("start_date")
        if start is not None and v < start:
            raise ValueError("end_date must be >= start_date")
        return v


class BacktestSummary(BaseModel):
    id: int
    name: Optional[str]
    strategy_id: Optional[int]
    broker_account_id: Optional[int]
    start_date: date
    end_date: date
    initial_capital: float
    status: str
    created_at: datetime
    finished_at: Optional[datetime]

    @classmethod
    def from_row(cls, row: BacktestRun) -> "BacktestSummary":
        return cls(
            id=int(row.id),
            name=row.name,
            strategy_id=row.strategy_id,
            broker_account_id=row.broker_account_id,
            start_date=row.start_date,
            end_date=row.end_date,
            initial_capital=float(row.initial_capital),
            status=str(row.status),
            created_at=row.created_at,
            finished_at=row.finished_at,
        )


class BacktestDetail(BacktestSummary):
    config: Optional[dict[str, Any]] = None
    metrics: Optional[dict[str, Any]] = None
    announcements_processed: Optional[int] = None
    signals_generated: Optional[int] = None
    blocked_trades: Optional[int] = None

    @classmethod
    def from_row(cls, row: BacktestRun) -> "BacktestDetail":
        base = BacktestSummary.from_row(row).model_dump()
        results = row.results or {}
        return cls(
            **base,
            config=row.config,
            metrics=results.get("metrics"),
            announcements_processed=results.get("announcements_processed"),
            signals_generated=results.get("signals_generated"),
            blocked_trades=results.get("blocked_trades"),
        )


# -------------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------------


@router.get("/runs", response_model=list[BacktestSummary])
def list_runs(
    limit: int = Query(50, ge=1, le=200),
    strategy_id: Optional[int] = Query(None, description="filter by strategy_id"),
    status_filter: Optional[str] = Query(
        None,
        alias="status",
        description="filter by status (pending|running|done|failed)",
    ),
    db: Session = Depends(get_db),
) -> list[BacktestSummary]:
    stmt = select(BacktestRun).order_by(BacktestRun.created_at.desc())
    if strategy_id is not None:
        stmt = stmt.where(BacktestRun.strategy_id == int(strategy_id))
    if status_filter is not None:
        stmt = stmt.where(BacktestRun.status == status_filter)
    stmt = stmt.limit(int(limit))
    rows = db.execute(stmt).scalars().all()
    return [BacktestSummary.from_row(r) for r in rows]


@router.post(
    "/runs",
    response_model=BacktestSummary,
    status_code=status.HTTP_201_CREATED,
)
async def create_run(
    body: BacktestCreate,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> BacktestSummary:
    # Validate that strategy + broker account exist (if provided).
    if body.strategy_id is not None:
        s = db.get(Strategy, int(body.strategy_id))
        if s is None:
            raise HTTPException(404, detail=f"strategy_id {body.strategy_id} not found")
    if body.broker_account_id is not None:
        a = db.get(BrokerAccount, int(body.broker_account_id))
        if a is None:
            raise HTTPException(
                404, detail=f"broker_account_id {body.broker_account_id} not found"
            )
    # Insert the run row in `pending` state. The actual backtest
    # runs as a background task; the API returns immediately so
    # the UI can poll.
    row = BacktestRun(
        name=body.name,
        strategy_id=body.strategy_id,
        broker_account_id=body.broker_account_id,
        start_date=body.start_date,
        end_date=body.end_date,
        initial_capital=float(body.initial_capital),
        status="pending",
        config={
            "name": body.name,
            "strategy_id": body.strategy_id,
            "broker_account_id": body.broker_account_id,
            "start_date": body.start_date.isoformat(),
            "end_date": body.end_date.isoformat(),
            "initial_capital": float(body.initial_capital),
        },
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    # Kick off the run in a background task. We capture a fresh
    # session factory so the task isn't bound to the request
    # thread's session.
    from app.db.session import SessionLocal

    background.add_task(
        _run_in_background,
        run_id=int(row.id),
        cfg=BacktestConfig(
            name=body.name or f"run-{row.id}",
            strategy_id=body.strategy_id,
            broker_account_id=body.broker_account_id,
            start_date=body.start_date,
            end_date=body.end_date,
            initial_capital=float(body.initial_capital),
            run_id=int(row.id),
        ),
        session_factory=SessionLocal,
    )
    return BacktestSummary.from_row(row)


@router.get("/runs/{run_id}", response_model=BacktestDetail)
def get_run(run_id: int, db: Session = Depends(get_db)) -> BacktestDetail:
    row = db.get(BacktestRun, int(run_id))
    if row is None:
        raise HTTPException(404, detail=f"backtest run {run_id} not found")
    return BacktestDetail.from_row(row)


@router.delete(
    "/runs/{run_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=None,
)
def delete_run(run_id: int, db: Session = Depends(get_db)) -> Response:
    row = db.get(BacktestRun, int(run_id))
    if row is None:
        raise HTTPException(404, detail=f"backtest run {run_id} not found")
    # Hard-delete (no soft-delete in v1).
    db.delete(row)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/runs/{run_id}/trades")
def get_trades(
    run_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return the list of trades for the run.

    Reads from `trades` table when the run has a `results`
    blob (preferred) — that's the recorded in-process list. As
    a fallback, we filter by `signal_id in (signals created
    during the run)`, but T7 doesn't currently persist signals
    separately per-run, so the blob is the source of truth.
    """
    row = db.get(BacktestRun, int(run_id))
    if row is None:
        raise HTTPException(404, detail=f"backtest run {run_id} not found")
    results = row.results or {}
    trades = results.get("trades") or []
    return {
        "run_id": int(run_id),
        "count": len(trades),
        "trades": trades,
    }


@router.get("/runs/{run_id}/equity-curve")
def get_equity_curve(
    run_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = db.get(BacktestRun, int(run_id))
    if row is None:
        raise HTTPException(404, detail=f"backtest run {run_id} not found")
    results = row.results or {}
    curve = results.get("equity_curve") or []
    return {
        "run_id": int(run_id),
        "initial_capital": float(row.initial_capital),
        "count": len(curve),
        "curve": curve,
    }


# -------------------------------------------------------------------------
# Background task — the actual backtest
# -------------------------------------------------------------------------


def _run_in_background(
    *,
    run_id: int,
    cfg: BacktestConfig,
    session_factory,
) -> None:
    """Run the backtest and update the row.

    Marked `status="running"` at start, `status="done"` or
    `status="failed"` at the end. Errors are logged; the row
    is left in `failed` state with the error message in
    `config["error"]`.
    """
    from app.db.models import BacktestRun
    from sqlalchemy.orm import Session

    # Mark running.
    with session_factory() as session:
        row = session.get(BacktestRun, run_id)
        if row is None:
            return
        row.status = "running"
        session.add(row)
        session.commit()

    try:
        # The engine is async (uses asyncio for the paper backend
        # + risk engine); we run it in a fresh event loop.
        result = asyncio.run(run_backtest(cfg))
        log.info(
            "backtest.background_done",
            run_id=run_id,
            announcements=result.announcements_processed,
            signals=result.signals_generated,
            trades=len(result.trades),
        )
    except Exception as e:  # noqa: BLE001
        log.exception("backtest.background_failed", run_id=run_id)
        with session_factory() as session:
            row = session.get(BacktestRun, run_id)
            if row is None:
                return
            row.status = "failed"
            cfg_dict = dict(row.config or {})
            cfg_dict["error"] = repr(e)
            row.config = cfg_dict
            session.add(row)
            session.commit()
