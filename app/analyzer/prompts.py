"""Prompt-template lookup, rendering, and the v1 seed set.

The `seed_default_prompts.py` script calls `seed_defaults(session)` to
insert (or upsert) the DEFAULT + 15 per-event-type prompt rows. The
service uses `load_template(session, event_type)` to pick the right
template at analysis time.

Template selection priority (v1):
  1. Per-event-type row, picked by a simple keyword heuristic on the
     announcement's title + pdf_url (see `detect_event_type`).
  2. DEFAULT row.
  3. If neither exists, the analyzer refuses the analysis and logs a
     clear error.

The heuristic is intentionally simple — a 1-line substring match.
Operators can override it later via a setting or per-monitor override.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analyzer.schemas import DEFAULT_EVENT_TYPE, EVENT_TYPES
from app.db.models import PromptHistory, PromptTemplate
from app.logging_config import get_logger

log = get_logger(__name__)


# -- Keyword heuristic (v1) ----------------------------------------------
#
# Maps each supported EventType to a list of substrings (case-
# insensitive) we'll search for in the announcement title (and, as a
# tie-breaker, the PDF URL). The first match wins; if nothing matches,
# the analyzer falls back to the DEFAULT template.
#
# Multiple keywords per category are allowed (e.g. "ACQUISITION" covers
# "acquisition", "acquires", "stake purchase", "to acquire"). All
# substring matches, no regex magic.

_KEYWORD_MAP: dict[str, tuple[str, ...]] = {
    "ORDER_WIN": ("order win", "order bagged", "wins order", "secures order", "order worth", "new order"),
    "ACQUISITION": ("acquisition", "acquires", "stake purchase", "to acquire", "acquired"),
    "MERGER": ("merger", "scheme of arrangement", "amalgamation", "demerger"),
    "DIVIDEND": ("dividend", "interim dividend", "final dividend", "special dividend"),
    "BUYBACK": ("buyback", "buy-back", "share buyback", "shares buyback", "tender offer"),
    "STOCK_SPLIT": ("stock split", "share split", "split of shares"),
    "BONUS": ("bonus share", "bonus issue"),
    "RIGHTS_ISSUE": ("rights issue", "rights entitlement"),
    "Q1_RESULTS": ("q1", "first quarter", "quarter 1"),
    "Q2_RESULTS": ("q2", "second quarter", "quarter 2", "half year", "h1"),
    "Q3_RESULTS": ("q3", "third quarter", "quarter 3", "nine month"),
    "Q4_RESULTS": ("q4", "fourth quarter", "quarter 4"),
    "ANNUAL_RESULTS": ("annual result", "year ended", "audited", "fy ", "financial year"),
    "BOARD_MEETING": ("board meeting", "intimation of board meeting", "trading window"),
}


def detect_event_type(title: str, pdf_url: Optional[str] = None) -> str:
    """Pick the most likely event type from title (+ optional URL).

    v1: case-insensitive substring match. We pick the FIRST category
    whose keyword appears in `title` (in the order defined above).
    URL is a tie-breaker — only used if title has no hits.

    This is deliberately dumb; the operator can override the template
    in the UI (T5). The goal is "good enough most of the time, never
    crash" for the 60s end-to-end demo.

    Returns one of the 15 EventType values, or "OTHER" if nothing
    matches. The DEFAULT template is selected afterwards by the
    service when this returns "OTHER".
    """
    haystack_title = (title or "").lower()
    haystack_url = (pdf_url or "").lower()
    for event_type, keywords in _KEYWORD_MAP.items():
        for kw in keywords:
            if kw in haystack_title:
                return event_type
    # Tie-breaker on the URL.
    if haystack_url:
        for event_type, keywords in _KEYWORD_MAP.items():
            for kw in keywords:
                if kw in haystack_url:
                    return event_type
    return "OTHER"


# -- DB access -----------------------------------------------------------


def load_template(
    session: Session, event_type: str
) -> Optional[PromptTemplate]:
    """Load the prompt template row for an event_type, or None."""
    stmt = select(PromptTemplate).where(PromptTemplate.event_type == event_type)
    return session.execute(stmt).scalar_one_or_none()


def load_default_template(session: Session) -> Optional[PromptTemplate]:
    """Load the DEFAULT template row, or None."""
    return load_template(session, DEFAULT_EVENT_TYPE)


def upsert_template(
    session: Session,
    *,
    event_type: str,
    system_prompt: str,
    user_template: str,
    model: str = "deepseek-chat",
    temperature: float = 0.2,
    max_tokens: int = 2000,
    updated_by: str = "system",
    change_note: Optional[str] = None,
) -> tuple[PromptTemplate, bool]:
    """Insert a new template, or update the existing one.

    Returns (template, created). When updating, bumps `version`,
    writes a `PromptHistory` row, and returns the updated template.

    Idempotent: re-running with the same args is a no-op for the row,
    but the version is still bumped if any field actually changes.
    """
    existing = load_template(session, event_type)
    if existing is None:
        t = PromptTemplate(
            event_type=event_type,
            system_prompt=system_prompt,
            user_template=user_template,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            version=1,
            updated_by=updated_by,
        )
        session.add(t)
        session.flush()
        session.add(
            PromptHistory(
                template_id=t.id,
                version=1,
                system_prompt=system_prompt,
                user_template=user_template,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                change_note=change_note or "seed",
            )
        )
        session.flush()
        return t, True

    # Bump version if any field changed.
    changed = (
        existing.system_prompt != system_prompt
        or existing.user_template != user_template
        or existing.model != model
        or existing.temperature != temperature
        or existing.max_tokens != max_tokens
    )
    if not changed:
        return existing, False

    existing.system_prompt = system_prompt
    existing.user_template = user_template
    existing.model = model
    existing.temperature = temperature
    existing.max_tokens = max_tokens
    existing.version = existing.version + 1
    existing.updated_by = updated_by
    session.flush()
    session.add(
        PromptHistory(
            template_id=existing.id,
            version=existing.version,
            system_prompt=system_prompt,
            user_template=user_template,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            change_note=change_note or "update",
        )
    )
    session.flush()
    return existing, False


# -- Rendering -----------------------------------------------------------


def render_user_prompt(
    template: PromptTemplate,
    *,
    pdf_url: str,
    extra: Optional[dict[str, str]] = None,
) -> str:
    """Render the user-side prompt.

    Substitutes `{{pdf_url}}` (and any other `{{key}}` placeholders in
    `extra`) inside `template.user_template`. We use a safe regex
    substitution so a malformed `{{...}}` in the template body is left
    alone (we never want to crash the analyzer over a typo in the UI).
    """
    s = template.user_template
    s = _safe_replace(s, "pdf_url", pdf_url)
    if extra:
        for k, v in extra.items():
            s = _safe_replace(s, k, v)
    return s


def append_announcement_context(
    rendered: str,
    *,
    symbol: str,
    exchange: str,
    headline: str,
) -> str:
    """Ground the user prompt with the wire metadata we actually have.

    The DeepSeek chat API cannot fetch URLs, so a template that only
    says "Read the PDF at {{pdf_url}}" makes the model invent filing
    content. Appending the exchange-feed headline gives it real text
    to analyse; the instruction footer stops it hallucinating beyond
    that.
    """
    return (
        f"{rendered}\n\n"
        "Announcement metadata from the exchange feed (authoritative):\n"
        f"- Company/symbol: {symbol} ({exchange})\n"
        f"- Headline: {headline}\n\n"
        "You cannot open URLs. Base your analysis ONLY on the metadata "
        "above. If it is insufficient for a confident call, say so in "
        "`reasoning`, use event_type OTHER, and lower `confidence`. "
        "Never invent facts that are not in the metadata."
    )


def render_system_prompt(
    template: PromptTemplate,
    *,
    event_type: str,
) -> str:
    """Render the system-side prompt.

    Per the spec: "You are a financial analyst. {{system_prompt_specific}}
    Return ONLY valid JSON matching the schema. No prose, no markdown."

    The template's `system_prompt` field IS the system_prompt_specific
    text; we just wrap it.
    """
    return (
        f"You are a financial analyst analysing an NSE/BSE corporate "
        f"announcement (event_type: {event_type}). "
        f"{template.system_prompt} "
        f"Return ONLY valid JSON matching the schema. No prose, no markdown."
    )


_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def _safe_replace(text: str, key: str, value: str) -> str:
    """Replace `{{key}}` (with optional whitespace) with `value`."""
    pattern = r"\{\{\s*" + re.escape(key) + r"\s*\}\}"
    return re.sub(pattern, value, text)


# -- Default seed set ----------------------------------------------------


def _default_system_prompt(event_type: str) -> str:
    """The DEFAULT system_prompt used by `seed_defaults`."""
    return (
        f"You are a senior financial analyst covering Indian equity "
        f"markets. You are reading a corporate announcement that has "
        f"been categorised as {event_type} (or OTHER if you cannot tell). "
        f"Extract the trading-relevant facts and return a structured JSON "
        f"object with these keys: "
        f"event_type, summary, sentiment, sentiment_score, confidence, "
        f"recommendation, reasoning, key_numbers."
    )


def _default_user_template(event_type: str) -> str:
    """The DEFAULT user_template used by `seed_defaults`."""
    return (
        f"Read the PDF at {{pdf_url}} and decide whether the news is "
        f"material for trading. The filing has been categorised as "
        f"{event_type}. If you disagree, pick a more accurate event_type "
        f"from the allowed list. Return a JSON object matching the schema."
    )


def _per_event_system_prompt(event_type: str) -> str:
    """A per-category tweak to the system prompt.

    Most categories just inherit the default; the ones that benefit
    from extra instruction get a short appended line.
    """
    base = _default_system_prompt(event_type)
    tail = _per_event_tail(event_type)
    return f"{base} {tail}".strip()


def _per_event_tail(event_type: str) -> str:
    if event_type == "ORDER_WIN":
        return "Pay close attention to the deal value, the counterparty, and the segment."
    if event_type == "ACQUISITION":
        return "Pay close attention to the stake %, deal value, and whether the deal is friendly."
    if event_type == "DIVIDEND":
        return "Pay close attention to the dividend per share, record date, and total payout."
    if event_type == "BUYBACK":
        return "Pay close attention to the total buyback value, the floor price, and the mode (tender/open market)."
    if event_type in {"Q1_RESULTS", "Q2_RESULTS", "Q3_RESULTS", "Q4_RESULTS", "ANNUAL_RESULTS"}:
        return "Pay close attention to revenue, EBITDA, PAT, and any YoY surprises vs. consensus."
    if event_type == "BOARD_MEETING":
        return "Note the meeting date and the agenda items."
    if event_type in {"STOCK_SPLIT", "BONUS", "RIGHTS_ISSUE"}:
        return "Note the ratio, the record date, and the implied dilution."
    if event_type == "MERGER":
        return "Note the swap ratio, the effective date, and any conditions."
    return ""


def seed_defaults(
    session: Session, *, updated_by: str = "system"
) -> list[PromptTemplate]:
    """Insert (or upsert) DEFAULT + 15 per-event-type templates.

    Idempotent — running twice yields 16 rows total. Returns the list
    of templates touched (created OR updated).
    """
    touched: list[PromptTemplate] = []
    for event_type in EVENT_TYPES:
        if event_type == DEFAULT_EVENT_TYPE:
            system_prompt = _default_system_prompt("OTHER")
            user_template = (
                "Read the PDF at {{pdf_url}} and return a JSON object "
                "matching the schema."
            )
        else:
            system_prompt = _per_event_system_prompt(event_type)
            user_template = _default_user_template(event_type)
        t, _created = upsert_template(
            session,
            event_type=event_type,
            system_prompt=system_prompt,
            user_template=user_template,
            updated_by=updated_by,
            change_note="seed_default_prompts",
        )
        touched.append(t)
    return touched


def list_event_types() -> Iterable[str]:
    """Convenience for tests / scripts."""
    return EVENT_TYPES
