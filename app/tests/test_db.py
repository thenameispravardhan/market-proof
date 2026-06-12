"""Schema and CRUD tests for all 14 tables."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect

from app.db.models import (
    Analysis,
    Announcement,
    AuditLog,
    BacktestRun,
    BrokerAccount,
    NotificationChannel,
    NotificationLog,
    Position,
    PromptHistory,
    PromptTemplate,
    RiskEvent,
    Signal,
    SignalRule,
    Strategy,
    Trade,
    Webhook,
    WebhookDelivery,
)
from app.db.session import Base
from app.db import session as db_session_mod


EXPECTED_TABLES = {
    # core
    "announcements",
    "analyses",
    "signals",
    "trades",
    "positions",
    "risk_events",
    # advanced
    "strategies",
    "broker_accounts",
    "signal_rules",
    "prompt_templates",
    "prompt_history",
    "webhooks",
    "webhook_deliveries",
    "notification_channels",
    "notification_log",
    "backtest_runs",
    "audit_log",
    # infra
    "app_settings",
}


def test_all_tables_created() -> None:
    insp = inspect(db_session_mod.engine)
    actual = set(insp.get_table_names())
    missing = EXPECTED_TABLES - actual
    assert not missing, f"missing tables: {missing}; got: {sorted(actual)}"
    # We expect at least 14 user-facing tables; app_settings is the 15th
    # infra table used by /api/settings.
    assert len(actual & EXPECTED_TABLES) >= 14


def test_base_metadata_covers_all_models() -> None:
    """ORM-level coverage — every model class maps to a real table."""
    tables = set(Base.metadata.tables.keys())
    for cls in (
        Announcement,
        Analysis,
        Signal,
        Trade,
        Position,
        RiskEvent,
        Strategy,
        BrokerAccount,
        SignalRule,
        PromptTemplate,
        PromptHistory,
        Webhook,
        WebhookDelivery,
        NotificationChannel,
        NotificationLog,
        BacktestRun,
        AuditLog,
    ):
        assert cls.__tablename__ in tables, f"model {cls.__name__} not registered"


# -------------------------------------------------------------------------
# CRUD round-trips
# -------------------------------------------------------------------------


def _make_announcement(db_session) -> Announcement:
    a = Announcement(
        symbol="RELIANCE",
        exchange="BSE",
        event_type="ORDER_WIN",
        headline="Reliance wins Rs 5,000 cr defence order",
    )
    db_session.add(a)
    db_session.flush()
    return a


def test_crud_announcement(db_session) -> None:
    a = _make_announcement(db_session)
    assert a.id is not None
    fetched = db_session.get(Announcement, a.id)
    assert fetched is not None
    assert fetched.symbol == "RELIANCE"


def test_announcement_content_hash_optional(db_session) -> None:
    """content_hash is nullable — older rows / migrations may not have it."""
    a = Announcement(
        symbol="TCS",
        exchange="NSE",
        event_type="ORDER_WIN",
        headline="TCS wins contract",
        content_hash=None,
    )
    db_session.add(a)
    db_session.flush()
    assert a.id is not None
    assert a.content_hash is None


def test_announcement_content_hash_unique(db_session) -> None:
    """UNIQUE(content_hash) on announcements — required by the verify spec."""
    from sqlalchemy.exc import IntegrityError

    from app.db.models import compute_content_hash

    h = compute_content_hash(
        exchange="BSE", symbol="RELIANCE",
        filed_at=datetime(2026, 1, 15, 9, 30, tzinfo=timezone.utc),
        pdf_url="https://bseindia.com/x.pdf",
    )
    db_session.add(
        Announcement(
            content_hash=h,
            symbol="RELIANCE",
            exchange="BSE",
            event_type="ORDER_WIN",
            headline="Reliance wins Rs 5,000 cr defence order",
        )
    )
    db_session.flush()
    # Same content_hash on a different row -> unique violation.
    db_session.add(
        Announcement(
            content_hash=h,
            symbol="INFY",
            exchange="BSE",
            event_type="ORDER_WIN",
            headline="Different headline, same dedupe key",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_announcement_content_hash_unique_at_table_level() -> None:
    """The UNIQUE constraint is named and discoverable via SQLAlchemy
    metadata — the verifier looks for it programmatically."""
    from sqlalchemy import inspect

    from app.db import session as db_session_mod

    insp = inspect(db_session_mod.engine)
    uqs = insp.get_unique_constraints("announcements")
    # SQLAlchemy normalises constraint columns to a list; we look for
    # content_hash in any unique constraint on the table.
    matched = [uq for uq in uqs if "content_hash" in uq.get("column_names", [])]
    assert matched, f"no UNIQUE constraint on content_hash; got: {uqs}"


def test_compute_content_hash_deterministic() -> None:
    """The helper must be stable across calls and order-independent of
    keyword arguments."""
    from app.db.models import compute_content_hash

    a = compute_content_hash(
        exchange="BSE",
        symbol="RELIANCE",
        filed_at=datetime(2026, 1, 15, 9, 30, tzinfo=timezone.utc),
        pdf_url="https://bseindia.com/x.pdf",
    )
    b = compute_content_hash(
        exchange="bse",                       # lower-case -> normalised
        symbol=" reliance ",                  # whitespace -> stripped
        filed_at=datetime(2026, 1, 15, 9, 30),# naive -> assumed UTC
        pdf_url="https://bseindia.com/x.pdf",
    )
    assert a == b
    assert len(a) == 64
    assert all(c in "0123456789abcdef" for c in a)


def test_crud_analysis(db_session) -> None:
    a = _make_announcement(db_session)
    analysis = Analysis(
        announcement_id=a.id,
        sentiment="positive",
        sentiment_score=72.5,
        confidence=0.85,
        recommendation="buy",
        rationale="Strong order win, large deal value.",
        deal_value_inr_crore=5000.0,
    )
    db_session.add(analysis)
    db_session.flush()
    assert analysis.id is not None
    assert analysis.sentiment_score == 72.5


def test_crud_signal_trade_position(db_session) -> None:
    a = _make_announcement(db_session)
    analysis = Analysis(
        announcement_id=a.id, sentiment="positive", recommendation="buy", confidence=0.9
    )
    db_session.add(analysis)
    db_session.flush()
    sig = Signal(analysis_id=analysis.id, symbol="RELIANCE", action="BUY", confidence=0.9)
    db_session.add(sig)
    db_session.flush()

    trade = Trade(signal_id=sig.id, symbol="RELIANCE", side="BUY", quantity=10, price=2500.0)
    db_session.add(trade)
    db_session.flush()
    assert trade.id is not None

    pos = Position(symbol="RELIANCE", quantity=10, average_price=2500.0)
    db_session.add(pos)
    db_session.flush()
    assert pos.id is not None


def test_crud_risk_event(db_session) -> None:
    re = RiskEvent(event_type="DAILY_LOSS_BREACH", severity="critical", message="halt trading", halted=True)
    db_session.add(re)
    db_session.flush()
    assert re.id is not None


def test_crud_strategy_and_signal_rule(db_session) -> None:
    s = Strategy(name="Aggressive Earnings", description="high beta", config={"MAX_CAPITAL_RISK_PCT": 1.5})
    db_session.add(s)
    db_session.flush()
    rule = SignalRule(
        strategy_id=s.id,
        name="Big deal buy",
        priority=10,
        conditions={"all_of": [{"field": "deal_value_inr_crore", "op": ">=", "value": 100}]},
        action="BUY",
    )
    db_session.add(rule)
    db_session.flush()
    assert rule.id is not None


def test_strategy_unique_name(db_session) -> None:
    from sqlalchemy.exc import IntegrityError

    db_session.add(Strategy(name="Dup"))
    db_session.flush()
    db_session.add(Strategy(name="Dup"))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_cascade_delete_strategy_removes_rules(db_session) -> None:
    s = Strategy(name="Cascade")
    db_session.add(s)
    db_session.flush()
    db_session.add(
        SignalRule(
            strategy_id=s.id,
            name="r1",
            conditions={"all_of": []},
            action="HOLD",
        )
    )
    db_session.flush()
    rule_id = db_session.query(SignalRule).filter_by(strategy_id=s.id).one().id
    db_session.delete(s)
    db_session.flush()
    assert db_session.get(SignalRule, rule_id) is None


def test_crud_broker_account(db_session) -> None:
    ba = BrokerAccount(
        name="Primary",
        broker="fyers",
        app_id="APP123",
        secret_key="***",
        access_token="***",
    )
    db_session.add(ba)
    db_session.flush()
    assert ba.id is not None


def test_crud_prompt_template_and_history(db_session) -> None:
    t = PromptTemplate(
        event_type="ORDER_WIN",
        system_prompt="You are a financial analyst.",
        user_template="Analyse {{pdf_url}}",
        model="deepseek-chat",
        temperature=0.2,
        max_tokens=2000,
    )
    db_session.add(t)
    db_session.flush()
    h = PromptHistory(
        template_id=t.id,
        version=1,
        system_prompt="You are a financial analyst.",
        user_template="Analyse {{pdf_url}}",
        model="deepseek-chat",
        temperature=0.2,
        max_tokens=2000,
        change_note="initial",
    )
    db_session.add(h)
    db_session.flush()
    assert h.id is not None


def test_prompt_template_unique_event_type(db_session) -> None:
    from sqlalchemy.exc import IntegrityError

    db_session.add(
        PromptTemplate(
            event_type="DEFAULT",
            system_prompt="x",
            user_template="y",
        )
    )
    db_session.flush()
    db_session.add(
        PromptTemplate(
            event_type="DEFAULT",
            system_prompt="x",
            user_template="y",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_crud_webhook_and_delivery(db_session) -> None:
    w = Webhook(name="Slack", direction="out", event_filter="signal,trade", url="https://x")
    db_session.add(w)
    db_session.flush()
    d = WebhookDelivery(webhook_id=w.id, direction="out", event_type="signal", status_code=200)
    db_session.add(d)
    db_session.flush()
    assert d.id is not None


def test_crud_notification_channel_and_log(db_session) -> None:
    ch = NotificationChannel(
        name="TG-Alerts",
        kind="telegram",
        config={"chat_id": "123"},
        events_filter="signal,trade",
    )
    db_session.add(ch)
    db_session.flush()
    log = NotificationLog(channel_id=ch.id, event_type="signal", status="sent")
    db_session.add(log)
    db_session.flush()
    assert log.id is not None


def test_crud_backtest_run(db_session) -> None:
    from datetime import date

    s = Strategy(name="BT")
    db_session.add(s)
    db_session.flush()
    run = BacktestRun(
        name="trial",
        strategy_id=s.id,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        initial_capital=1_000_000.0,
        status="pending",
    )
    db_session.add(run)
    db_session.flush()
    assert run.id is not None


def test_crud_audit_log(db_session) -> None:
    a = AuditLog(actor="ui", action="settings.update", target="global", before={"x": 1}, after={"x": 2})
    db_session.add(a)
    db_session.flush()
    assert a.id is not None
