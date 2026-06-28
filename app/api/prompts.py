"""`/api/prompts` — Prompt template CRUD + version history + restore + preview.

Endpoints:
  GET    /api/prompts                          list all templates
  GET    /api/prompts/{event_type}             get current version
  GET    /api/prompts/{event_type}/history     full version history
  POST   /api/prompts/{event_type}             create or update (bumps version)
  POST   /api/prompts/{event_type}/restore/{version}   restore from history
  POST   /api/prompts/{event_type}/preview     render template with a fake pdf_url (no API call)
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analyzer.prompts import EVENT_TYPES, default_template_fields
from app.db.init import init_db
from app.db.models import AuditLog, PromptHistory, PromptTemplate
from app.db.session import get_db
from app.logging_config import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/prompts", tags=["prompts"])
init_db()

_VALID_MODELS = {
    "deepseek-v4-flash",
    "deepseek-v4-pro",
}

_VALID_REASONING_EFFORT = {"low", "medium", "high"}


# -------------------------------------------------------------------------
# Pydantic schemas
# -------------------------------------------------------------------------


class PromptUpdate(BaseModel):
    system_prompt: str = Field(..., min_length=50, max_length=4000)
    user_template: str = Field(...)
    model: str = Field(default="deepseek-v4-flash")
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=2000, ge=100, le=8000)
    # DeepSeek v4 inference controls. reasoning_effort tunes the thinking
    # depth (only used when thinking is on); thinking_enabled=False sends
    # extra_body={"thinking": {"type": "disabled"}}; stream toggles SSE.
    reasoning_effort: str = Field(default="medium")
    thinking_enabled: bool = Field(default=False)
    stream: bool = Field(default=False)
    change_note: Optional[str] = None

    @field_validator("user_template")
    @classmethod
    def _must_have_placeholder(cls, v: str) -> str:
        if "{{pdf_url}}" not in v:
            raise ValueError("user_template must contain {{pdf_url}} placeholder")
        return v

    @field_validator("model")
    @classmethod
    def _model_check(cls, v: str) -> str:
        if v not in _VALID_MODELS:
            # Be lenient — allow any non-empty string so operators can try new models
            if not v.strip():
                raise ValueError("model must be a non-empty string")
        return v

    @field_validator("reasoning_effort")
    @classmethod
    def _reasoning_check(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in _VALID_REASONING_EFFORT:
            raise ValueError(
                f"reasoning_effort must be one of {sorted(_VALID_REASONING_EFFORT)}"
            )
        return v


class PromptPreviewRequest(BaseModel):
    pdf_url: str = Field(default="https://example.com/filing.pdf")


# -------------------------------------------------------------------------
# Serialisers
# -------------------------------------------------------------------------


def _ser_template(t: PromptTemplate) -> dict[str, Any]:
    return {
        "id": t.id,
        "event_type": t.event_type,
        "system_prompt": t.system_prompt,
        "user_template": t.user_template,
        "model": t.model,
        "temperature": t.temperature,
        "max_tokens": t.max_tokens,
        "reasoning_effort": t.reasoning_effort,
        "thinking_enabled": t.thinking_enabled,
        "stream": t.stream,
        "version": t.version,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        "updated_by": t.updated_by,
    }


def _ser_history(h: PromptHistory) -> dict[str, Any]:
    return {
        "id": h.id,
        "template_id": h.template_id,
        "version": h.version,
        "system_prompt": h.system_prompt,
        "user_template": h.user_template,
        "model": h.model,
        "temperature": h.temperature,
        "max_tokens": h.max_tokens,
        "reasoning_effort": h.reasoning_effort,
        "thinking_enabled": h.thinking_enabled,
        "stream": h.stream,
        "changed_at": h.changed_at.isoformat() if h.changed_at else None,
        "change_note": h.change_note,
    }


def _get_template_or_404(event_type: str, db: Session) -> PromptTemplate:
    t = db.execute(
        select(PromptTemplate).where(PromptTemplate.event_type == event_type)
    ).scalar_one_or_none()
    if t is None:
        raise HTTPException(404, detail=f"prompt template for '{event_type}' not found")
    return t


def _save_history(db: Session, t: PromptTemplate) -> None:
    """Snapshot the current template state into prompt_history."""
    db.add(
        PromptHistory(
            template_id=t.id,
            version=t.version,
            system_prompt=t.system_prompt,
            user_template=t.user_template,
            model=t.model,
            temperature=t.temperature,
            max_tokens=t.max_tokens,
            reasoning_effort=t.reasoning_effort,
            thinking_enabled=t.thinking_enabled,
            stream=t.stream,
            change_note=None,
        )
    )


def _write_audit(
    db: Session,
    *,
    action: str,
    target: str,
    before: Optional[dict],
    after: Optional[dict],
) -> None:
    db.add(
        AuditLog(
            actor="api",
            action=action,
            target=target,
            before=before,
            after=after,
        )
    )


# -------------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------------


@router.get("")
def list_prompts(db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.execute(
        select(PromptTemplate).order_by(PromptTemplate.event_type.asc())
    ).scalars().all()
    return {"prompts": [_ser_template(t) for t in rows]}


@router.get("/{event_type}")
def get_prompt(event_type: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    return _ser_template(_get_template_or_404(event_type, db))


@router.get("/{event_type}/history")
def get_prompt_history(event_type: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    t = _get_template_or_404(event_type, db)
    rows = db.execute(
        select(PromptHistory)
        .where(PromptHistory.template_id == t.id)
        .order_by(PromptHistory.version.desc())
    ).scalars().all()
    return {"event_type": event_type, "history": [_ser_history(h) for h in rows]}


@router.get("/{event_type}/default")
def get_prompt_default(event_type: str) -> dict[str, Any]:
    """Factory-default field values for a template — NOT persisted.

    Backs the editor's "Reset to default" button: the UI fills the form
    from this, and the operator must Save to actually change the active
    version. Until they do, the last-saved prompt keeps driving the
    analyzer.
    """
    if event_type not in EVENT_TYPES:
        raise HTTPException(404, detail=f"unknown event_type '{event_type}'")
    return {"event_type": event_type, **default_template_fields(event_type)}


@router.post("/{event_type}", status_code=status.HTTP_200_OK)
def upsert_prompt(
    event_type: str,
    body: PromptUpdate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Create or update a prompt template. Always bumps version + writes history."""
    t = db.execute(
        select(PromptTemplate).where(PromptTemplate.event_type == event_type)
    ).scalar_one_or_none()

    if t is None:
        # Create new template at version 1
        t = PromptTemplate(
            event_type=event_type,
            system_prompt=body.system_prompt,
            user_template=body.user_template,
            model=body.model,
            temperature=body.temperature,
            max_tokens=body.max_tokens,
            reasoning_effort=body.reasoning_effort,
            thinking_enabled=body.thinking_enabled,
            stream=body.stream,
            version=1,
            updated_by="api",
        )
        db.add(t)
        db.flush()
        # Write initial history
        db.add(
            PromptHistory(
                template_id=t.id,
                version=1,
                system_prompt=body.system_prompt,
                user_template=body.user_template,
                model=body.model,
                temperature=body.temperature,
                max_tokens=body.max_tokens,
                reasoning_effort=body.reasoning_effort,
                thinking_enabled=body.thinking_enabled,
                stream=body.stream,
                change_note=body.change_note,
            )
        )
        _write_audit(db, action="prompt.create", target=event_type, before=None, after=_ser_template(t))
        db.commit()
        db.refresh(t)
        return _ser_template(t)

    # Snapshot before state
    before = _ser_template(t)
    old_version = t.version

    # Bump version
    t.version = old_version + 1
    t.system_prompt = body.system_prompt
    t.user_template = body.user_template
    t.model = body.model
    t.temperature = body.temperature
    t.max_tokens = body.max_tokens
    t.reasoning_effort = body.reasoning_effort
    t.thinking_enabled = body.thinking_enabled
    t.stream = body.stream
    t.updated_by = "api"

    db.flush()

    # Write history for the NEW version
    db.add(
        PromptHistory(
            template_id=t.id,
            version=t.version,
            system_prompt=body.system_prompt,
            user_template=body.user_template,
            model=body.model,
            temperature=body.temperature,
            max_tokens=body.max_tokens,
            reasoning_effort=body.reasoning_effort,
            thinking_enabled=body.thinking_enabled,
            stream=body.stream,
            change_note=body.change_note,
        )
    )
    _write_audit(db, action="prompt.update", target=event_type, before=before, after=_ser_template(t))
    db.commit()
    db.refresh(t)
    return _ser_template(t)


@router.post("/{event_type}/restore/{version}")
def restore_prompt(
    event_type: str,
    version: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Restore a template to a historical version. Bumps version number."""
    t = _get_template_or_404(event_type, db)
    hist = db.execute(
        select(PromptHistory)
        .where(PromptHistory.template_id == t.id)
        .where(PromptHistory.version == version)
    ).scalar_one_or_none()
    if hist is None:
        raise HTTPException(404, detail=f"version {version} not found for '{event_type}'")

    before = _ser_template(t)
    t.version += 1
    t.system_prompt = hist.system_prompt
    t.user_template = hist.user_template
    t.model = hist.model
    t.temperature = hist.temperature
    t.max_tokens = hist.max_tokens
    t.reasoning_effort = hist.reasoning_effort
    t.thinking_enabled = hist.thinking_enabled
    t.stream = hist.stream
    t.updated_by = "api"

    db.flush()
    db.add(
        PromptHistory(
            template_id=t.id,
            version=t.version,
            system_prompt=hist.system_prompt,
            user_template=hist.user_template,
            model=hist.model,
            temperature=hist.temperature,
            max_tokens=hist.max_tokens,
            reasoning_effort=hist.reasoning_effort,
            thinking_enabled=hist.thinking_enabled,
            stream=hist.stream,
            change_note=f"restored from v{version}",
        )
    )
    _write_audit(db, action="prompt.restore", target=event_type, before=before, after=_ser_template(t))
    db.commit()
    db.refresh(t)
    return _ser_template(t)


@router.post("/{event_type}/preview")
def preview_prompt(
    event_type: str,
    body: PromptPreviewRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return the rendered prompt text — no API call made."""
    t = _get_template_or_404(event_type, db)
    rendered_user = t.user_template.replace("{{pdf_url}}", body.pdf_url)
    return {
        "event_type": event_type,
        "version": t.version,
        "model": t.model,
        "temperature": t.temperature,
        "max_tokens": t.max_tokens,
        "reasoning_effort": t.reasoning_effort,
        "thinking_enabled": t.thinking_enabled,
        "stream": t.stream,
        "system_prompt": t.system_prompt,
        "rendered_user_template": rendered_user,
        "pdf_url": body.pdf_url,
    }
