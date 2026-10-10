"""/api/model — the router that can switch on a trade veto.

The scorer's maths is covered by test_mover_model.py; this file covers the
HTTP layer an operator actually uses to decide whether to turn the gate on:
status, what-if scoring and the replay preview.
"""
from __future__ import annotations

import pytest

from app.services import mover_model as mm
from app.tests.factories import seed_signal_chain

pytestmark = pytest.mark.skipif(mm.load() is None, reason="no live_model.json")


def test_status_reports_artifact_and_live_toggles(client):
    r = client.get("/api/model/status")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert set(body["settings"]) == {
        "MODEL_ENABLED", "MODEL_GATE_ENABLED", "MODEL_VARIANT",
        "MODEL_MIN_PROBABILITY", "MODEL_MIN_COVERAGE",
    }
    # The gate must never be on by default — it can veto real trades.
    assert body["settings"]["MODEL_GATE_ENABLED"] is False


def test_variants_lists_the_default(client):
    keys = [v.get("key") or v.get("name") for v in client.get("/api/model/variants").json()["variants"]]
    assert mm.load()["default_variant"] in keys


def test_score_with_raw_features_and_live_fields_agree_on_shape(client):
    empty = client.post("/api/model/score", json={"features": {}}).json()
    assert empty["score"]["coverage"] == 0.0
    # With zero coverage the verdict must abstain rather than block.
    assert empty["verdict"] == "insufficient"

    live = client.post("/api/model/score", json={
        "symbol": "RELIANCE", "headline": "Receives order worth Rs 500 crore",
        "event_type": "ORDER_WIN", "sentiment": "positive", "sentiment_score": 70,
        "confidence": 0.9, "recommendation": "buy", "last_price": 2500.0,
        "min_coverage": 0.0,
    }).json()
    assert 0.0 <= live["score"]["probability"] <= 1.0
    assert live["verdict"] in ("allow", "block")


def test_preview_reports_mover_rates_per_verdict(client, db_session, isolated_db):
    for i, mv in enumerate([3.0, -2.5, 0.2, 0.1, -0.4, 4.0]):
        seed_signal_chain(db_session, symbol=f"SYM{i}", move_30m=mv, move_5m=mv / 2)
    # An outcome with no 30-minute move is not a sample.
    seed_signal_chain(db_session, symbol="PENDING", move_30m=None)

    body = client.get("/api/model/preview", params={"min_probability": 0.0, "min_coverage": 0.0}).json()
    assert body["n_scored"] == 6
    assert body["base_mover_rate"] == pytest.approx(3 / 6, abs=1e-4)
    # Threshold 0 allows everything: nothing blocked, allowed == base rate.
    assert body["blocked"]["n"] == 0
    assert body["allowed"]["mover_rate"] == body["base_mover_rate"]

    strict = client.get("/api/model/preview", params={"min_probability": 1.0, "min_coverage": 0.0}).json()
    assert strict["blocked"]["n"] == 6 and strict["allowed"]["n"] == 0


def test_reload_is_idempotent(client):
    a = client.post("/api/model/reload").json()
    b = client.post("/api/model/reload").json()
    assert a["available"] == b["available"] is True
