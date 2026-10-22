"""Live desk (person A): guards that switch a strategy off, book-level risk, execution."""
from __future__ import annotations

import asyncio

import pytest

from app.algo import engine
from app.tests.test_algo import DAY0, _cross_spec


# ---- A1: guards ----------------------------------------------------------------

def test_guard_is_validated_and_absent_by_default():
    assert "guard" not in _cross_spec()
    s = _cross_spec(guard={"max_drawdown": 5000, "max_rejects": 3})
    assert s["guard"] == {"max_drawdown": 5000, "max_rejects": 3, "on_breach": "pause"}
    assert "guard" not in _cross_spec(guard={"max_drawdown": None})            # nothing set = no guard
    with pytest.raises(ValueError, match="on_breach"):
        _cross_spec(guard={"max_drawdown": 5000, "on_breach": "panic"})
    with pytest.raises(ValueError, match="rejected orders"):
        _cross_spec(guard={"max_rejects": 0})


def test_guard_breach_reads_drawdown_rejects_and_slippage():
    from app.algo.runner import guard_breach

    st = {"closed": 500.0, "peak": 2000.0, "rejects": 0, "slippage": []}
    assert guard_breach({"max_drawdown": 2000}, st, 0.0) is None              # 1,500 down
    assert "drawdown ₹2,000" in guard_breach({"max_drawdown": 2000}, st, -500.0)   # open MTM counts
    assert "3 entries rejected" in guard_breach({"max_rejects": 3}, {**st, "rejects": 3}, 0.0)
    few = {**st, "slippage": [0.5] * 9}
    assert guard_breach({"max_slippage_pct": 0.1}, few, 0.0) is None           # too few fills to judge
    assert "median slippage 0.500%" in guard_breach({"max_slippage_pct": 0.1}, {**st, "slippage": [0.5] * 10}, 0.0)
    assert engine.fill_slippage({"act": 1, "qty": 10, "entry_quote": 100.0, "entry": 101.0,
                                 "exit_quote": 110.0, "exit": 109.0}) == [(pytest.approx(1.0), pytest.approx(10.0)),
                                                                          (pytest.approx(10 / 11), pytest.approx(10.0))]


def test_runner_switches_a_breached_strategy_off_and_flattens(monkeypatch, isolated_db):
    from app.algo import runner as rn
    from app.db import session as dbs
    from app.db.models import AlgoStrategy, AlgoTrade, AuditLog

    spec = _cross_spec(guard={"max_drawdown": 1000, "on_breach": "flatten"})
    at = lambda s: rn._utc(DAY0 + s).replace(tzinfo=None)  # noqa: E731
    with dbs.SessionLocal() as db:
        s = AlgoStrategy(name="g", spec=spec, enabled=True, mode="paper")
        db.add(s)
        db.flush()
        for k, net in enumerate((500.0, -1300.0)):                              # peak 500, now -800
            db.add(AlgoTrade(strategy_id=s.id, symbol="NSE:TEST-EQ", side="BUY", quantity=1, mode="paper",
                             status="closed", entry_at=at(k * 60), exit_at=at(k * 60 + 30), net_pnl=net))
        db.add(AlgoTrade(strategy_id=s.id, symbol="NSE:TEST-EQ", side="BUY", quantity=10, mode="paper", status="open",
                         entry_at=at(600), entry_price=100.0, ref="u", ref_sign=1,
                         legs=[{"symbol": "NSE:TEST-EQ", "kind": "EQ", "act": 1, "qty": 10, "qty0": 10, "per_set": 1,
                                "entry": 100.0, "label": "TEST"}]))
        db.commit()
        sid = s.id

    async def fake_ltp(symbols):
        return {x.upper(): 80.0 for x in symbols}                               # open MTM -200 -> 1,500 down

    monkeypatch.setattr(rn, "_ltp", fake_ltp)
    r = rn.AlgoRunner()
    asyncio.run(r.tick(DAY0 + 3600))
    with dbs.SessionLocal() as db:
        assert db.get(AlgoStrategy, sid).enabled is False
        audit = db.query(AuditLog).filter(AuditLog.action == "algo.guard_pause").one()
        assert "drawdown" in audit.after["why"]
        flat = db.query(AlgoTrade).filter(AlgoTrade.status == "closed", AlgoTrade.exit_reason == "GUARD").one()
        assert flat.quantity == 10
    assert any("GUARD switched it off" in e["msg"] for e in r.events)
