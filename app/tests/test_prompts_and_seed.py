"""Prompt-template + seed tests.

Coverage:
  - detect_event_type: keyword heuristic across all 15 categories.
  - detect_event_type: returns OTHER for unrelated titles.
  - render_system_prompt + render_user_prompt substitutions.
  - upsert_template: inserts new, updates existing (bumps version).
  - upsert_template: idempotent — no version bump when nothing changed.
  - load_template + load_default_template.
  - seed_defaults: inserts 16 rows; running twice still 16 (idempotency).
  - seed_defaults: every row has version=1 on first seed.
  - The seed script main() is importable (no side effects on import).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from app.analyzer.prompts import (
    DEFAULT_EVENT_TYPE,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_REASONING_EFFORT,
    DEFAULT_TEMPERATURE,
    EVENT_TYPES,
    append_announcement_context,
    default_template_fields,
    detect_event_type,
    is_non_material,
    load_default_template,
    load_template,
    render_system_prompt,
    render_user_prompt,
    seed_defaults,
    upsert_template,
)
from app.analyzer.schemas import DEFAULT_EVENT_TYPE as _DEFAULT
from app.db.models import PromptHistory, PromptTemplate

# Wipe rows between tests so test order doesn't leak PromptTemplate /
# PromptHistory data from one test into another.
pytestmark = pytest.mark.usefixtures("isolated_db")


# -- is_non_material (pre-LLM noise filter) -----------------------------


def test_non_material_filters_obvious_admin_notices():
    assert is_non_material("Closure of Trading Window") is True
    assert is_non_material("Compliance Certificate under Regulation 7(3)") is True
    assert is_non_material("Newspaper Publication of audited accounts") is True


def test_non_material_lets_real_events_through():
    assert is_non_material("Company wins order worth Rs 1500 cr") is False
    assert is_non_material("Interim Dividend Announcement") is False
    assert is_non_material("") is False


def test_non_material_does_not_drop_material_event_bundled_with_admin_notice():
    """Regression: a material catalyst in the same headline as an
    administrative phrase must NOT be filtered — the substring denylist is
    too blunt to safely drop it, so the material-override veto wins."""
    assert (
        is_non_material(
            "Intimation of Board Meeting to consider fund raising "
            "and closure of Trading Window"
        )
        is False
    )
    assert (
        is_non_material("Newspaper publication of notice of buyback of equity shares")
        is False
    )


# -- detect_event_type --------------------------------------------------


def test_detect_buyback():
    assert detect_event_type("BUYBACK OF SHARES") == "BUYBACK"
    assert detect_event_type("Board approves share buyback via tender") == "BUYBACK"


def test_detect_dividend():
    assert detect_event_type("Interim Dividend Announcement") == "DIVIDEND"
    assert detect_event_type("Final Dividend Declared") == "DIVIDEND"


def test_detect_order_win():
    assert detect_event_type("Company wins order worth Rs 1500 cr") == "ORDER_WIN"
    assert detect_event_type("New order bagged from defence sector") == "ORDER_WIN"


def test_detect_acquisition():
    assert detect_event_type("Reliance acquires stake in xyz") == "ACQUISITION"
    assert detect_event_type("Approval for acquisition of ABC Ltd") == "ACQUISITION"


def test_detect_merger():
    assert detect_event_type("Scheme of Arrangement — Merger") == "MERGER"
    assert detect_event_type("Amalgamation with subsidiary") == "MERGER"


def test_detect_split_bonus_rights():
    assert detect_event_type("Stock Split Announcement") == "STOCK_SPLIT"
    assert detect_event_type("Bonus Issue of Shares") == "BONUS"
    assert detect_event_type("Rights Issue Opening") == "RIGHTS_ISSUE"


def test_detect_quarterly_results():
    # Order matters: the first match wins. Q1 → Q1_RESULTS, etc.
    # If the title contains "Q1 results" we want Q1_RESULTS, not
    # "results" hitting Q4 or ANNUAL first.
    assert detect_event_type("Q1 Results Announcement") == "Q1_RESULTS"
    assert detect_event_type("Q2 Results — PAT up 30%") == "Q2_RESULTS"
    assert detect_event_type("Q3 Results — strong quarter") == "Q3_RESULTS"
    assert detect_event_type("Q4 Results") == "Q4_RESULTS"
    # Annual: "audited" / "year ended" — must not collide with quarters.
    assert detect_event_type("Audited Financial Results for the year ended March 2026") == "ANNUAL_RESULTS"


def test_detect_board_meeting():
    assert detect_event_type("Intimation of Board Meeting") == "BOARD_MEETING"


def test_detect_other_for_garbage():
    assert detect_event_type("Random update about something") == "OTHER"
    assert detect_event_type("") == "OTHER"
    # No URL at all and empty title -> OTHER.
    assert detect_event_type("") == "OTHER"


def test_detect_uses_url_as_tiebreaker():
    """If the title has no match but the URL does, we still get a hit."""
    assert detect_event_type("Filing", pdf_url="https://x.com/q1-results.pdf") == "Q1_RESULTS"


# -- Rendering ----------------------------------------------------------


def test_render_user_prompt_substitutes_pdf_url():
    t = PromptTemplate(
        event_type="DEFAULT",
        system_prompt="x",
        user_template="Read {{pdf_url}} please.",
    )
    out = render_user_prompt(t, pdf_url="https://x/y.pdf")
    assert "https://x/y.pdf" in out
    assert "{{pdf_url}}" not in out


def test_render_user_prompt_handles_whitespace_in_placeholder():
    t = PromptTemplate(
        event_type="DEFAULT",
        system_prompt="x",
        user_template="Read {{ pdf_url }} please.",
    )
    out = render_user_prompt(t, pdf_url="https://x/y.pdf")
    assert "https://x/y.pdf" in out


def test_render_user_prompt_keeps_unknown_placeholder():
    """An unreplaced {{foo}} stays as-is so the operator notices the
    typo in the UI rather than silently losing it."""
    t = PromptTemplate(
        event_type="DEFAULT",
        system_prompt="x",
        user_template="Read {{pdf_url}} and {{nope}}.",
    )
    out = render_user_prompt(t, pdf_url="https://x/y.pdf")
    assert "https://x/y.pdf" in out
    assert "{{nope}}" in out


def test_render_user_prompt_substitutes_headline_via_extra():
    t = PromptTemplate(
        event_type="DEFAULT",
        system_prompt="x",
        user_template="Analyse: {{headline}} ({{symbol}}/{{exchange}})",
    )
    out = render_user_prompt(
        t,
        pdf_url="https://x/y.pdf",
        extra={"headline": "Order win Rs 500 cr", "symbol": "ABC", "exchange": "NSE"},
    )
    assert "Order win Rs 500 cr" in out
    assert "ABC" in out
    assert "NSE" in out


def test_append_announcement_context_grounds_the_prompt():
    """The LLM cannot fetch the PDF URL; the grounding block must carry
    the real headline and forbid invention."""
    out = append_announcement_context(
        "Read the PDF at https://x/y.pdf.",
        symbol="RELIANCE",
        exchange="NSE",
        headline="Reliance wins Rs 5000 crore order from IOCL",
    )
    assert "Reliance wins Rs 5000 crore order from IOCL" in out
    assert "RELIANCE" in out
    assert "cannot open URLs" in out
    assert "Never invent facts" in out


def test_render_system_prompt_wraps():
    t = PromptTemplate(
        event_type="ORDER_WIN",
        system_prompt="Be concise.",
        user_template="x",
    )
    out = render_system_prompt(t, event_type="ORDER_WIN")
    assert "financial analyst" in out.lower()
    assert "Be concise." in out
    assert "ORDER_WIN" in out
    assert "valid JSON" in out
    assert "no markdown" in out.lower()


# -- upsert_template / load_template -----------------------------------


def test_upsert_inserts_new(db_session):
    t, created = upsert_template(
        db_session,
        event_type="ORDER_WIN",
        system_prompt="s1",
        user_template="u1",
    )
    db_session.commit()
    assert created is True
    assert t.id is not None
    assert t.version == 1
    # History row written too.
    h = db_session.execute(select(PromptHistory).where(PromptHistory.template_id == t.id)).scalar_one()
    assert h.version == 1


def test_upsert_updates_existing_and_bumps_version(db_session):
    t1, _ = upsert_template(
        db_session,
        event_type="ORDER_WIN",
        system_prompt="s1",
        user_template="u1",
    )
    db_session.commit()
    t2, created = upsert_template(
        db_session,
        event_type="ORDER_WIN",
        system_prompt="s2",  # changed
        user_template="u1",
    )
    db_session.commit()
    assert created is False
    assert t2.id == t1.id
    assert t2.version == 2
    assert t2.system_prompt == "s2"
    # Two history rows now.
    h_rows = db_session.execute(
        select(PromptHistory).where(PromptHistory.template_id == t1.id).order_by(PromptHistory.version)
    ).scalars().all()
    assert [h.version for h in h_rows] == [1, 2]


def test_upsert_persists_v4_inference_controls(db_session):
    """reasoning_effort / thinking_enabled / stream round-trip onto the
    template and its history snapshot."""
    t, _ = upsert_template(
        db_session,
        event_type="ORDER_WIN",
        system_prompt="s1",
        user_template="u1",
        reasoning_effort="high",
        thinking_enabled=True,
        stream=True,
    )
    db_session.commit()
    assert t.reasoning_effort == "high"
    assert t.thinking_enabled is True
    assert t.stream is True
    h = db_session.execute(
        select(PromptHistory).where(PromptHistory.template_id == t.id)
    ).scalar_one()
    assert h.reasoning_effort == "high"
    assert h.thinking_enabled is True
    assert h.stream is True


def test_upsert_defaults_v4_controls(db_session):
    """Unspecified v4 controls fall back to medium / thinking-off /
    stream-off."""
    t, _ = upsert_template(
        db_session, event_type="ORDER_WIN", system_prompt="s", user_template="u"
    )
    db_session.commit()
    assert t.reasoning_effort == "medium"
    assert t.thinking_enabled is False
    assert t.stream is False


def test_upsert_bumps_version_when_only_v4_control_changes(db_session):
    """Flipping just the streaming toggle is a real change → version bump."""
    t1, _ = upsert_template(
        db_session, event_type="ORDER_WIN", system_prompt="s1", user_template="u1"
    )
    db_session.commit()
    t2, created = upsert_template(
        db_session,
        event_type="ORDER_WIN",
        system_prompt="s1",
        user_template="u1",
        stream=True,  # only this changed
    )
    db_session.commit()
    assert created is False
    assert t2.version == 2
    assert t2.stream is True


def test_upsert_no_change_does_not_bump_version(db_session):
    t1, _ = upsert_template(
        db_session,
        event_type="ORDER_WIN",
        system_prompt="s1",
        user_template="u1",
    )
    db_session.commit()
    t2, created = upsert_template(
        db_session,
        event_type="ORDER_WIN",
        system_prompt="s1",
        user_template="u1",
    )
    db_session.commit()
    assert created is False
    assert t2.version == 1  # no bump


def test_load_template_returns_row(db_session):
    upsert_template(db_session, event_type="ORDER_WIN", system_prompt="s", user_template="u")
    db_session.commit()
    t = load_template(db_session, "ORDER_WIN")
    assert t is not None
    assert t.event_type == "ORDER_WIN"


def test_load_template_missing_returns_none(db_session):
    assert load_template(db_session, "BOGUS") is None


def test_load_default_template(db_session):
    upsert_template(db_session, event_type=DEFAULT_EVENT_TYPE, system_prompt="d", user_template="d")
    db_session.commit()
    t = load_default_template(db_session)
    assert t is not None
    assert t.event_type == DEFAULT_EVENT_TYPE


# -- seed_defaults -----------------------------------------------------


def test_seed_defaults_inserts_16_rows(db_session):
    seed_defaults(db_session)
    db_session.commit()
    rows = db_session.execute(select(PromptTemplate)).scalars().all()
    assert len(rows) == 16
    types = {r.event_type for r in rows}
    assert DEFAULT_EVENT_TYPE in types
    # Every EventType enum value is present.
    for v in EVENT_TYPES:
        assert v in types


def test_seed_defaults_first_run_all_version_1(db_session):
    seed_defaults(db_session)
    db_session.commit()
    rows = db_session.execute(select(PromptTemplate)).scalars().all()
    assert all(r.version == 1 for r in rows)


def test_seed_defaults_is_idempotent(db_session):
    """Running twice yields 16 rows (no duplicates, no version bump)."""
    seed_defaults(db_session)
    db_session.commit()
    seed_defaults(db_session)
    db_session.commit()
    rows = db_session.execute(select(PromptTemplate)).scalars().all()
    assert len(rows) == 16
    assert all(r.version == 1 for r in rows)


def test_seed_defaults_writes_history(db_session):
    seed_defaults(db_session)
    db_session.commit()
    h_rows = db_session.execute(select(PromptHistory)).scalars().all()
    # One history row per template (16).
    assert len(h_rows) == 16
    assert all(h.version == 1 for h in h_rows)


def test_seed_defaults_overwrite_false_preserves_operator_edits(db_session):
    """Regression: app startup must NOT reset an operator's saved prompt.

    Seeds, then edits the DEFAULT template (as the UI would), then re-seeds
    with overwrite=False (the startup path). The edit must survive — same
    content, same version, no new history row."""
    seed_defaults(db_session)
    db_session.commit()
    # Operator changes the DEFAULT prompt and saves (bumps to v2).
    upsert_template(
        db_session,
        event_type=DEFAULT_EVENT_TYPE,
        system_prompt="OPERATOR base prompt that must survive a restart.",
        user_template="EDITED {{pdf_url}}",
        change_note="base",
    )
    db_session.commit()
    edited = load_default_template(db_session)
    assert edited.version == 2
    edited_hist_count = len(
        db_session.execute(
            select(PromptHistory).where(PromptHistory.template_id == edited.id)
        ).scalars().all()
    )

    # Simulate a restart: insert-missing-only.
    seed_defaults(db_session, overwrite=False)
    db_session.commit()

    after = load_default_template(db_session)
    assert after.system_prompt == "OPERATOR base prompt that must survive a restart."
    assert after.user_template == "EDITED {{pdf_url}}"
    assert after.version == 2  # NOT bumped back to default
    # No extra history row written for the untouched template.
    after_hist_count = len(
        db_session.execute(
            select(PromptHistory).where(PromptHistory.template_id == after.id)
        ).scalars().all()
    )
    assert after_hist_count == edited_hist_count


def test_seed_defaults_overwrite_false_inserts_missing_on_fresh_db(db_session):
    """On a fresh DB, overwrite=False still seeds all 16 templates."""
    touched = seed_defaults(db_session, overwrite=False)
    db_session.commit()
    assert len(touched) == 16
    rows = db_session.execute(select(PromptTemplate)).scalars().all()
    assert len(rows) == 16
    assert all(r.version == 1 for r in rows)


# -- default_template_fields / reset-to-default ------------------------


def test_default_template_fields_returns_factory_defaults():
    f = default_template_fields(DEFAULT_EVENT_TYPE)
    assert f["model"] == DEFAULT_MODEL
    assert f["temperature"] == DEFAULT_TEMPERATURE
    assert f["max_tokens"] == DEFAULT_MAX_TOKENS
    assert f["reasoning_effort"] == DEFAULT_REASONING_EFFORT
    assert f["thinking_enabled"] is False
    assert f["stream"] is False
    # The DEFAULT user template must keep the placeholder the editor needs.
    assert "{{pdf_url}}" in f["user_template"]
    assert f["system_prompt"]


def test_default_template_fields_matches_a_fresh_seed(db_session):
    """The Reset button (default_template_fields) and a fresh seed agree —
    they share one source of truth, so resetting yields exactly what a
    brand-new seed would have produced."""
    seed_defaults(db_session)
    db_session.commit()
    t = load_default_template(db_session)
    f = default_template_fields(DEFAULT_EVENT_TYPE)
    assert f["system_prompt"] == t.system_prompt
    assert f["user_template"] == t.user_template
    assert f["model"] == t.model
    assert f["reasoning_effort"] == t.reasoning_effort
    assert f["thinking_enabled"] == t.thinking_enabled
    assert f["stream"] == t.stream


def test_get_prompt_default_endpoint_returns_defaults(client):
    r = client.get(f"/api/prompts/{DEFAULT_EVENT_TYPE}/default")
    assert r.status_code == 200
    body = r.json()
    assert body["event_type"] == DEFAULT_EVENT_TYPE
    assert body["model"] == DEFAULT_MODEL
    assert body["reasoning_effort"] == DEFAULT_REASONING_EFFORT
    assert body["thinking_enabled"] is False
    assert body["stream"] is False
    assert "{{pdf_url}}" in body["user_template"]


def test_get_prompt_default_unknown_event_type_404(client):
    assert client.get("/api/prompts/BOGUS/default").status_code == 404


def test_get_prompt_default_does_not_persist(client, db_session):
    """Reset-to-default is read-only — hitting it must not create or
    mutate any template row (the last-saved version stays active)."""
    before = len(db_session.execute(select(PromptTemplate)).scalars().all())
    client.get(f"/api/prompts/{DEFAULT_EVENT_TYPE}/default")
    after = len(db_session.execute(select(PromptTemplate)).scalars().all())
    assert after == before


# -- Script invocation -------------------------------------------------


def test_seed_script_runs_twice_yields_16_rows(tmp_path, monkeypatch):
    """Run the script twice against an isolated sqlite file."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("TESTING", "0")
    # Need to clear the cached settings + rebuild engine so the
    # script sees the new DATABASE_URL.
    from app import config as app_config
    from app.db import session as db_session_mod
    from app.db import init as db_init

    app_config.reset_settings_cache()
    db_session_mod.rebuild_engine_for_testing()
    db_init.init_db()

    script = Path(__file__).resolve().parent.parent.parent / "scripts" / "seed_default_prompts.py"
    assert script.exists()

    r1 = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        env={
            **__import__("os").environ,
            "DATABASE_URL": f"sqlite:///{db_path}",
            "TESTING": "0",
            "DEEPSEEK_API_KEY": "",
        },
    )
    assert r1.returncode == 0, f"first run failed:\nstdout={r1.stdout}\nstderr={r1.stderr}"

    r2 = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        env={
            **__import__("os").environ,
            "DATABASE_URL": f"sqlite:///{db_path}",
            "TESTING": "0",
            "DEEPSEEK_API_KEY": "",
        },
    )
    assert r2.returncode == 0, f"second run failed:\nstdout={r2.stdout}\nstderr={r2.stderr}"

    # Verify final state.
    from app.db.models import PromptTemplate as PT
    from app.db.session import SessionLocal
    with SessionLocal() as s:
        n = s.execute(select(PT)).scalars().all()
        assert len(n) == 16
        # All version=1 still — re-seed with identical content is a no-op.
        assert all(r.version == 1 for r in n)


# -- extracted-text mode helpers ------------------------------------------


def test_append_extracted_document_grounds_with_filing_text():
    from app.analyzer.prompts import append_extracted_document

    out = append_extracted_document(
        "Analyse this filing.",
        symbol="TCS",
        exchange="NSE",
        headline="Order win announcement",
        document_text="The company received an order worth Rs 500 crore.",
        pages_used=[1, 2, 7],
        pages_total=42,
        truncated=True,
    )
    assert "TCS (NSE)" in out
    assert "Rs 500 crore" in out
    assert "pages 1, 2, 7 of 42" in out
    assert "remaining pages omitted" in out
    # No-trade default stance is stated.
    assert "Default to recommendation HOLD" in out
    # The legacy "cannot open URLs" footer must NOT be in this path.
    assert "cannot open URLs" not in out


def test_safe_replace_is_literal_for_backslashes():
    """PDF text contains backslashes / regex group refs; substitution must
    insert them literally instead of letting re.sub interpret them."""
    from app.analyzer.prompts import _safe_replace

    out = _safe_replace(
        "Text: {{extracted_text}}",
        "extracted_text",
        r"C:\path\g<0> and \1 refs",
    )
    assert out == r"Text: C:\path\g<0> and \1 refs"
