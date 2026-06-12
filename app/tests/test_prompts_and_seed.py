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
    EVENT_TYPES,
    detect_event_type,
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
