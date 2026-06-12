"""`/api/strategies` — CRUD for trading strategies.

DELETE cascades to signal_rules via the DB foreign key.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.init import init_db
from app.db.models import AuditLog, Strategy
from app.db.session import get_db
from app.logging_config import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/strategies", tags=["strategies"])
init_db()


# -------------------------------------------------------------------------
# Pydantic schemas
# -------------------------------------------------------------------------


class StrategyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: Optional[str] = None
    enabled: bool = True
    config: Optional[dict[str, Any]] = None


class StrategyUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None
    config: Optional[dict[str, Any]] = None


# -------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------


def _ser(s: Strategy) -> dict[str, Any]:
    return {
        "id": s.id,
        "name": s.name,
        "description": s.description,
        "enabled": s.enabled,
        "config": s.config,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


def _get_or_404(strategy_id: int, db: Session) -> Strategy:
    s = db.get(Strategy, strategy_id)
    if s is None:
        raise HTTPException(404, detail=f"strategy {strategy_id} not found")
    return s


def _audit(
    db: Session,
    *,
    action: str,
    target: str,
    before: Optional[dict],
    after: Optional[dict],
) -> None:
    db.add(
        AuditLog(actor="api", action=action, target=target, before=before, after=after)
    )


# -------------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------------


@router.get("")
def list_strategies(db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.execute(select(Strategy).order_by(Strategy.id.asc())).scalars().all()
    return {"strategies": [_ser(s) for s in rows]}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_strategy(body: StrategyCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    # Unique name check
    existing = db.execute(
        select(Strategy).where(Strategy.name == body.name)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(409, detail=f"strategy with name '{body.name}' already exists")

    s = Strategy(
        name=body.name,
        description=body.description,
        enabled=body.enabled,
        config=body.config,
    )
    db.add(s)
    db.flush()
    _audit(db, action="strategy.create", target=f"strategy:{s.id}", before=None, after=_ser(s))
    db.commit()
    db.refresh(s)
    return _ser(s)


@router.get("/{strategy_id}")
def get_strategy(strategy_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    return _ser(_get_or_404(strategy_id, db))


@router.put("/{strategy_id}")
def update_strategy(
    strategy_id: int,
    body: StrategyUpdate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    s = _get_or_404(strategy_id, db)
    before = _ser(s)
    if body.name is not None:
        # Check uniqueness if renaming
        existing = db.execute(
            select(Strategy).where(Strategy.name == body.name).where(Strategy.id != strategy_id)
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(409, detail=f"strategy with name '{body.name}' already exists")
        s.name = body.name
    if body.description is not None:
        s.description = body.description
    if body.enabled is not None:
        s.enabled = body.enabled
    if body.config is not None:
        s.config = body.config
    _audit(db, action="strategy.update", target=f"strategy:{s.id}", before=before, after=_ser(s))
    db.commit()
    db.refresh(s)
    return _ser(s)


@router.delete("/{strategy_id}")
def delete_strategy(strategy_id: int, db: Session = Depends(get_db)) -> Response:
    s = _get_or_404(strategy_id, db)
    before = _ser(s)
    db.delete(s)
    _audit(db, action="strategy.delete", target=f"strategy:{strategy_id}", before=before, after=None)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
