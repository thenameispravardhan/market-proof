"""`/api/rules` — Signal rules CRUD + dry-run + reorder.

Endpoints:
  GET    /api/rules                        list (filterable by strategy_id)
  POST   /api/rules                        create
  GET    /api/rules/{id}
  PUT    /api/rules/{id}                   update
  DELETE /api/rules/{id}
  POST   /api/rules/reorder                {strategy_id, ordered_ids: [...]}
  POST   /api/rules/dry-run               {analysis: {...}} → {matched_rule, action, params}
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.init import init_db
from app.db.models import AuditLog, SignalRule, Strategy
from app.db.session import get_db
from app.logging_config import get_logger
from app.services.event_bus import event_bus

log = get_logger(__name__)

router = APIRouter(prefix="/api/rules", tags=["rules"])
init_db()

_VALID_FIELDS = {
    "event_type",
    "sentiment",
    "sentiment_score",
    "confidence",
    "recommendation",
    "deal_value_inr_crore",
    "stake_change_pct",
    "dividend_per_share",
    "buyback_value_inr_crore",
}
_VALID_OPS = {"==", "!=", "in", "not_in", ">=", "<=", ">", "<"}
_VALID_ACTIONS = {"BUY", "SELL", "HOLD", "BLOCK"}


# -------------------------------------------------------------------------
# Pydantic schemas
# -------------------------------------------------------------------------


def _validate_conditions(conds: dict) -> dict:
    """Validate the conditions dict structure."""
    all_of = conds.get("all_of", [])
    any_of = conds.get("any_of", [])
    if not all_of and not any_of:
        raise ValueError("conditions must have at least one condition in all_of or any_of")
    for clause in [all_of, any_of]:
        for c in clause:
            if not isinstance(c, dict):
                raise ValueError(f"each condition must be a dict, got {type(c)}")
            field = c.get("field")
            op = c.get("op")
            if field not in _VALID_FIELDS:
                raise ValueError(f"unknown field '{field}', must be one of {sorted(_VALID_FIELDS)}")
            if op not in _VALID_OPS:
                raise ValueError(f"unknown op '{op}', must be one of {sorted(_VALID_OPS)}")
            if "value" not in c:
                raise ValueError(f"condition missing 'value': {c}")
    return conds


class RuleCreate(BaseModel):
    strategy_id: int
    name: str = Field(..., min_length=1, max_length=128)
    priority: int = Field(default=100, ge=0)
    conditions: dict[str, Any]
    action: str
    action_params: Optional[dict[str, Any]] = None
    enabled: bool = True

    @field_validator("action")
    @classmethod
    def _action_check(cls, v: str) -> str:
        v = v.upper()
        if v not in _VALID_ACTIONS:
            raise ValueError(f"action must be one of {sorted(_VALID_ACTIONS)}")
        return v

    @field_validator("conditions")
    @classmethod
    def _conds_check(cls, v: dict) -> dict:
        return _validate_conditions(v)


class RuleUpdate(BaseModel):
    name: Optional[str] = None
    priority: Optional[int] = None
    conditions: Optional[dict[str, Any]] = None
    action: Optional[str] = None
    action_params: Optional[dict[str, Any]] = None
    enabled: Optional[bool] = None

    @field_validator("action")
    @classmethod
    def _action_check(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            v = v.upper()
            if v not in _VALID_ACTIONS:
                raise ValueError(f"action must be one of {sorted(_VALID_ACTIONS)}")
        return v

    @field_validator("conditions")
    @classmethod
    def _conds_check(cls, v: Optional[dict]) -> Optional[dict]:
        if v is not None:
            return _validate_conditions(v)
        return v


class ReorderRequest(BaseModel):
    strategy_id: int
    ordered_ids: list[int] = Field(..., min_length=1)


class DryRunRequest(BaseModel):
    analysis: dict[str, Any]
    strategy_id: Optional[int] = None


# -------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------


def _ser_rule(r: SignalRule) -> dict[str, Any]:
    return {
        "id": r.id,
        "strategy_id": r.strategy_id,
        "name": r.name,
        "priority": r.priority,
        "conditions": r.conditions,
        "action": r.action,
        "action_params": r.action_params,
        "enabled": r.enabled,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _get_rule_or_404(rule_id: int, db: Session) -> SignalRule:
    r = db.get(SignalRule, rule_id)
    if r is None:
        raise HTTPException(404, detail=f"rule {rule_id} not found")
    return r


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


def _eval_condition(cond: dict, analysis: dict) -> bool:
    """Evaluate a single condition dict against an analysis dict."""
    field = cond["field"]
    op = cond["op"]
    expected = cond["value"]
    actual = analysis.get(field)

    if op == "==":
        return actual == expected
    if op == "!=":
        return actual != expected
    if op == "in":
        return actual in (expected if isinstance(expected, list) else [expected])
    if op == "not_in":
        return actual not in (expected if isinstance(expected, list) else [expected])
    if actual is None:
        return False
    try:
        actual_num = float(actual)
        exp_num = float(expected)
    except (TypeError, ValueError):
        return False
    if op == ">=":
        return actual_num >= exp_num
    if op == "<=":
        return actual_num <= exp_num
    if op == ">":
        return actual_num > exp_num
    if op == "<":
        return actual_num < exp_num
    return False


def _eval_rule(rule: SignalRule, analysis: dict) -> bool:
    """Return True if this rule matches the given analysis."""
    conds = rule.conditions or {}
    all_of = conds.get("all_of", [])
    any_of = conds.get("any_of", [])

    if all_of and not all(_eval_condition(c, analysis) for c in all_of):
        return False
    if any_of and not any(_eval_condition(c, analysis) for c in any_of):
        return False
    # If any_of is present but no condition matched → fail
    if not all_of and not any_of:
        return False
    return True


# -------------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------------


@router.get("")
def list_rules(
    strategy_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    stmt = select(SignalRule).order_by(SignalRule.strategy_id, SignalRule.priority)
    if strategy_id is not None:
        stmt = stmt.where(SignalRule.strategy_id == strategy_id)
    rows = db.execute(stmt).scalars().all()
    return {"rules": [_ser_rule(r) for r in rows]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_rule(body: RuleCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    # Verify strategy exists
    s = db.get(Strategy, body.strategy_id)
    if s is None:
        raise HTTPException(404, detail=f"strategy {body.strategy_id} not found")

    r = SignalRule(
        strategy_id=body.strategy_id,
        name=body.name,
        priority=body.priority,
        conditions=body.conditions,
        action=body.action,
        action_params=body.action_params,
        enabled=body.enabled,
    )
    db.add(r)
    db.flush()
    _audit(db, action="rule.create", target=f"signal_rule:{r.id}", before=None, after=_ser_rule(r))
    db.commit()
    db.refresh(r)
    await event_bus.publish("rules.updated", {"rule_id": r.id, "strategy_id": r.strategy_id})
    return _ser_rule(r)


@router.get("/dry-run")
def dry_run_get() -> dict[str, Any]:
    """Redirect reminder — use POST."""
    raise HTTPException(405, detail="Use POST /api/rules/dry-run")


@router.post("/dry-run")
def dry_run(body: DryRunRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Evaluate all enabled rules against a synthetic analysis dict."""
    stmt = select(SignalRule).where(SignalRule.enabled.is_(True))
    if body.strategy_id is not None:
        stmt = stmt.where(SignalRule.strategy_id == body.strategy_id)
    stmt = stmt.order_by(SignalRule.priority.asc())
    rules = db.execute(stmt).scalars().all()

    for rule in rules:
        if _eval_rule(rule, body.analysis):
            return {
                "matched": True,
                "matched_rule": _ser_rule(rule),
                "action": rule.action,
                "action_params": rule.action_params,
            }
    return {"matched": False, "matched_rule": None, "action": "HOLD", "action_params": None}


@router.post("/reorder")
async def reorder_rules(body: ReorderRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Set priority of each rule to its position in ordered_ids (0-based * 10)."""
    for i, rule_id in enumerate(body.ordered_ids):
        r = db.get(SignalRule, rule_id)
        if r is None:
            raise HTTPException(404, detail=f"rule {rule_id} not found")
        if r.strategy_id != body.strategy_id:
            raise HTTPException(
                400,
                detail=f"rule {rule_id} belongs to strategy {r.strategy_id}, not {body.strategy_id}",
            )
        r.priority = i * 10
    db.commit()
    await event_bus.publish("rules.updated", {"strategy_id": body.strategy_id, "reordered": True})
    return {"ok": True, "strategy_id": body.strategy_id, "ordered_ids": body.ordered_ids}


@router.get("/{rule_id}")
def get_rule(rule_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    return _ser_rule(_get_rule_or_404(rule_id, db))


@router.put("/{rule_id}")
async def update_rule(rule_id: int, body: RuleUpdate, db: Session = Depends(get_db)) -> dict[str, Any]:
    r = _get_rule_or_404(rule_id, db)
    before = _ser_rule(r)
    if body.name is not None:
        r.name = body.name
    if body.priority is not None:
        r.priority = body.priority
    if body.conditions is not None:
        r.conditions = body.conditions
    if body.action is not None:
        r.action = body.action
    if body.action_params is not None:
        r.action_params = body.action_params
    if body.enabled is not None:
        r.enabled = body.enabled
    _audit(db, action="rule.update", target=f"signal_rule:{r.id}", before=before, after=_ser_rule(r))
    db.commit()
    db.refresh(r)
    await event_bus.publish("rules.updated", {"rule_id": r.id, "strategy_id": r.strategy_id})
    return _ser_rule(r)


@router.delete("/{rule_id}")
async def delete_rule(rule_id: int, db: Session = Depends(get_db)) -> Response:
    r = _get_rule_or_404(rule_id, db)
    before = _ser_rule(r)
    strategy_id = r.strategy_id
    db.delete(r)
    _audit(db, action="rule.delete", target=f"signal_rule:{rule_id}", before=before, after=None)
    db.commit()
    await event_bus.publish("rules.updated", {"rule_id": rule_id, "strategy_id": strategy_id, "deleted": True})
    return Response(status_code=status.HTTP_204_NO_CONTENT)
