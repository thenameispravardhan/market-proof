"""Signal rules engine.

Evaluates rows from the `signal_rules` table against an analysis
dict (the shape produced by `analysis_to_dict`).

Semantics (T3 spec):
  - Rules are loaded for a given strategy (or a default strategy when
    none is selected), sorted by `priority ASC`.
  - For each rule, evaluate `conditions` against the analysis dict.
  - First matching rule wins.
  - If nothing matches, default to HOLD with a `position_size_pct` of
    0 and a note in the rationale.
  - Supports `all_of` (AND) and `any_of` (OR) in the same `conditions`
    block; the rule matches when `all_of` is fully satisfied AND
    `any_of` has at least one satisfied entry. An empty/missing list
    is treated as "satisfied".
  - Supported operators per the T1 spec:
        ==, !=, in, not_in, >=, <=, >, <
  - Supported fields per T1: event_type, sentiment, sentiment_score,
    confidence, recommendation, deal_value_inr_crore,
    stake_change_pct, dividend_per_share, buyback_value_inr_crore.

On a match we emit a `signal_rules.matched` event on the event bus
and return a `RuleMatch` carrying the rule's id, action, action_params,
and a human-readable summary.

The engine is intentionally a *pure function* over the loaded rules
and the analysis dict. The session-bound loader `load_rules_for_strategy`
sits alongside it so tests can pass an in-memory list of dicts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import SignalRule, Strategy
from app.logging_config import get_logger
from app.services.event_bus import event_bus

log = get_logger(__name__)


# Public channel for observability — emitted on every rule match.
CHANNEL_RULE_MATCHED = "signal_rules.matched"


# Default strategy name — used when an analysis has no strategy
# associated (e.g. an analysis run before the user picks one).
DEFAULT_STRATEGY_NAME = "default"


# Supported operators. Keep in sync with the T1 spec.
SUPPORTED_OPS: frozenset[str] = frozenset(
    {"==", "!=", "in", "not_in", ">=", "<=", ">", "<"}
)

# Fields the engine understands. Anything else in a rule is a
# configuration error and we raise a clear `RuleError`.
SUPPORTED_FIELDS: frozenset[str] = frozenset(
    {
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
)


class RuleError(ValueError):
    """Raised when a rule's conditions are malformed."""


@dataclass(frozen=True)
class RuleMatch:
    """The result of a successful rule match (or the HOLD fallback)."""

    rule_id: Optional[int]      # None for the default HOLD
    action: str                 # BUY | SELL | HOLD | BLOCK
    action_params: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    matched_field: Optional[str] = None  # the field that triggered the match (best-effort)


# -- Loader --------------------------------------------------------------


def get_or_create_default_strategy(session: Session) -> Strategy:
    """Find the default strategy, or create one if missing.

    The T3 spec says "default strategy if none". The Strategy model
    has `name TEXT UNIQUE NOT NULL`, so we just look it up; if it's
    not there (e.g. first run), we create it.
    """
    s = session.execute(
        select(Strategy).where(Strategy.name == DEFAULT_STRATEGY_NAME)
    ).scalar_one_or_none()
    if s is None:
        s = Strategy(
            name=DEFAULT_STRATEGY_NAME,
            description="Default strategy — used when no strategy is selected.",
            enabled=True,
            config={},
        )
        session.add(s)
        session.flush()
    return s


def load_rules_for_strategy(
    session: Session, strategy_id: int
) -> list[SignalRule]:
    """Load enabled rules for one strategy, ordered by priority ASC.

    Disabled rules are skipped. Empty list is fine — the engine will
    return the HOLD fallback.
    """
    stmt = (
        select(SignalRule)
        .where(SignalRule.strategy_id == strategy_id)
        .where(SignalRule.enabled.is_(True))
        .order_by(SignalRule.priority.asc(), SignalRule.id.asc())
    )
    return list(session.execute(stmt).scalars().all())


# -- Pure evaluation -----------------------------------------------------


def evaluate(
    analysis: Mapping[str, Any],
    rules: Sequence[Mapping[str, Any] | SignalRule],
) -> RuleMatch:
    """Evaluate a list of rules against an analysis dict.

    `rules` may be `SignalRule` ORM rows or plain dicts (with the
    same keys: `priority`, `conditions`, `action`, `action_params`).
    The function is pure — no DB writes, no side effects (the caller
    is responsible for emitting the `signal_rules.matched` event).
    """
    # Sort by priority ASC; stable for ties (preserve input order).
    sorted_rules = sorted(
        list(rules),
        key=lambda r: _priority_of(r),
    )
    for r in sorted_rules:
        if not _is_enabled(r):
            continue
        conditions = _conditions_of(r)
        try:
            ok = _match_conditions(conditions, analysis)
        except RuleError as e:
            log.warning(
                "rules_engine.rule_malformed",
                rule_id=_id_of(r),
                error=str(e),
            )
            continue
        if ok:
            return RuleMatch(
                rule_id=_id_of(r),
                action=_action_of(r),
                action_params=dict(_action_params_of(r) or {}),
                rationale=_rationale_of(r, analysis),
            )
    # No match — HOLD fallback.
    return RuleMatch(
        rule_id=None,
        action="HOLD",
        action_params={"reason": "no_matching_rule"},
        rationale="No signal rule matched the analysis; defaulting to HOLD.",
    )


# -- Internals -----------------------------------------------------------


def _priority_of(r: Any) -> int:
    if isinstance(r, SignalRule):
        return int(r.priority)
    return int(r.get("priority", 100) if isinstance(r, dict) else 100)


def _is_enabled(r: Any) -> bool:
    if isinstance(r, SignalRule):
        return bool(r.enabled)
    if isinstance(r, dict):
        return bool(r.get("enabled", True))
    return True


def _id_of(r: Any) -> Optional[int]:
    if isinstance(r, SignalRule):
        return r.id
    if isinstance(r, dict):
        v = r.get("id")
        return int(v) if v is not None else None
    return None


def _conditions_of(r: Any) -> dict[str, Any]:
    if isinstance(r, SignalRule):
        return dict(r.conditions or {})
    if isinstance(r, dict):
        c = r.get("conditions")
        return dict(c) if isinstance(c, dict) else {}
    return {}


def _action_of(r: Any) -> str:
    if isinstance(r, SignalRule):
        return str(r.action)
    if isinstance(r, dict):
        return str(r.get("action") or "HOLD")
    return "HOLD"


def _action_params_of(r: Any) -> Optional[dict[str, Any]]:
    if isinstance(r, SignalRule):
        return r.action_params
    if isinstance(r, dict):
        p = r.get("action_params")
        return p if isinstance(p, dict) else None
    return None


def _name_of(r: Any) -> str:
    if isinstance(r, SignalRule):
        return r.name
    if isinstance(r, dict):
        return str(r.get("name") or "")
    return ""


def _rationale_of(r: Any, analysis: Mapping[str, Any]) -> str:
    name = _name_of(r)
    action = _action_of(r)
    return f"Rule '{name}' matched (action={action})."


def _match_conditions(
    conditions: Mapping[str, Any], analysis: Mapping[str, Any]
) -> bool:
    """Match `conditions` against `analysis`.

    - `all_of` (AND) is satisfied iff every entry matches; missing or
      empty `all_of` is satisfied.
    - `any_of` (OR) is satisfied iff at least one entry matches;
      missing or empty `any_of` is satisfied.
    - A rule matches iff BOTH groups are satisfied.
    """
    all_of = conditions.get("all_of")
    any_of = conditions.get("any_of")

    if all_of is not None:
        if not isinstance(all_of, list) or not all_of:
            raise RuleError("`all_of` must be a non-empty list of conditions")
        for cond in all_of:
            if not _match_condition(cond, analysis):
                return False

    if any_of is not None:
        if not isinstance(any_of, list) or not any_of:
            raise RuleError("`any_of` must be a non-empty list of conditions")
        if not any(_match_condition(c, analysis) for c in any_of):
            return False

    # No conditions at all → match (an empty rule is permissive).
    if all_of is None and any_of is None:
        return True

    return True


def _match_condition(
    cond: Mapping[str, Any], analysis: Mapping[str, Any]
) -> bool:
    """Match a single {field, op, value} condition against the analysis."""
    if not isinstance(cond, dict):
        raise RuleError(f"condition must be a dict, got {type(cond).__name__}")
    field = cond.get("field")
    op = cond.get("op")
    value = cond.get("value")
    if not isinstance(field, str) or not field:
        raise RuleError("condition.field must be a non-empty string")
    if field not in SUPPORTED_FIELDS:
        raise RuleError(f"unsupported field: {field!r}")
    if not isinstance(op, str) or op not in SUPPORTED_OPS:
        raise RuleError(f"unsupported op: {op!r}")

    actual = analysis.get(field)
    return _apply_op(op, actual, value)


def _apply_op(op: str, actual: Any, value: Any) -> bool:
    """Apply one operator to (actual, value).

    - `in` / `not_in`: value must be iterable.
    - `==` / `!=`: value comparison, case-insensitive for strings.
    - `>=`, `<=`, `>`, `<`: numeric comparison. `None` never satisfies
      an ordering (a missing number is not "less than" something).
    """
    if op == "==":
        return _eq(actual, value)
    if op == "!=":
        return not _eq(actual, value)
    if op == "in":
        if value is None or not hasattr(value, "__iter__"):
            raise RuleError("`in` requires value to be a list/tuple")
        return _in(actual, value)
    if op == "not_in":
        if value is None or not hasattr(value, "__iter__"):
            raise RuleError("`not_in` requires value to be a list/tuple")
        return not _in(actual, value)
    # Numeric ordering.
    if actual is None:
        return False
    try:
        a = float(actual)
        v = float(value)
    except (TypeError, ValueError) as e:
        raise RuleError(f"non-numeric value for op {op!r}: {value!r}") from e
    if op == ">=":
        return a >= v
    if op == "<=":
        return a <= v
    if op == ">":
        return a > v
    if op == "<":
        return a < v
    raise RuleError(f"unsupported op: {op!r}")


def _eq(actual: Any, value: Any) -> bool:
    if isinstance(actual, str) and isinstance(value, str):
        return actual.strip().lower() == value.strip().lower()
    return actual == value


def _in(actual: Any, choices: Iterable[Any]) -> bool:
    if isinstance(actual, str):
        a_norm = actual.strip().lower()
        return any(
            (isinstance(c, str) and c.strip().lower() == a_norm) or c == actual
            for c in choices
        )
    return actual in list(choices)


# -- Public helpers used by the service ----------------------------------


async def publish_match(match: RuleMatch, *, analysis: dict[str, Any]) -> int:
    """Emit the `signal_rules.matched` event for observability.

    Returns the number of subscribers the event was delivered to.
    The service should call this on every match.
    """
    return await event_bus.publish(
        CHANNEL_RULE_MATCHED,
        {
            "rule_id": match.rule_id,
            "action": match.action,
            "analysis_event_type": analysis.get("event_type"),
            "symbol": analysis.get("symbol"),
        },
    )
