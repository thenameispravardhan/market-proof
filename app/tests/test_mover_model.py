"""Mover model: scoring math, fail-open behaviour, and the risk-engine gate.

The properties that matter are not "is the AUC good" — that is measured
offline and printed by export_live.py. They are: the model can never break a
trade, and it can never block one unless the operator explicitly turned the
gate on.
"""
from __future__ import annotations

import math

import pytest

from app.services import mover_model as mm

pytestmark = pytest.mark.skipif(
    mm.load() is None,
    reason="no live_model.json — run AIdataset/model/export_live.py",
)


def _default():
    art = mm.load()
    return art["default_variant"], art["variants"][art["default_variant"]]


def test_empty_features_score_exactly_the_intercept():
    """Every absent feature must sit at its training mean, so the logit is
    the intercept alone. If this drifts, missing inputs have stopped being
    neutral and are silently pushing scores in some direction."""
    key, v = _default()
    s = mm.score({}, key)
    assert s is not None
    assert s.probability == pytest.approx(
        1 / (1 + math.exp(-v["intercept"])), abs=1e-6
    )
    assert s.coverage == 0.0


def test_real_feature_moves_the_score_and_raises_coverage():
    key, v = _default()
    i = v["feature_names"].index("sym_mover_rate")
    base = mm.score({}, key)
    hot = mm.score({"sym_mover_rate": v["mean"][i] + 4 * v["std"][i]}, key)
    assert hot is not None and base is not None
    assert hot.probability != base.probability
    assert hot.coverage > base.coverage


def test_fail_open_on_bad_input():
    """Junk in, "no opinion" out — never an exception into the risk path."""
    key, _ = _default()
    assert mm.score({}, "no-such-variant") is None
    assert mm.score({"sym_mover_rate": "twelve"}, key) is not None
    assert mm.score({"sym_mover_rate": None}, key) is not None


def test_verdict_never_blocks_without_evidence():
    key, _ = _default()
    s = mm.score({}, key)
    assert mm.verdict(None, min_probability=0.99)[0] == "insufficient"
    # A real score with no live data behind it abstains rather than blocks.
    assert mm.verdict(s, min_probability=0.99, min_coverage=0.5)[0] == "insufficient"
    assert mm.verdict(s, min_probability=0.99, min_coverage=0.0)[0] == "block"
    assert mm.verdict(s, min_probability=0.0, min_coverage=0.0)[0] == "allow"


def test_build_features_uses_the_frozen_symbol_priors():
    art = mm.load()
    symbol = next(iter(art["priors"]["symbol"]))
    f = mm.build_features(symbol=symbol, headline="Order win", confidence=0.8)
    assert f["sym_mover_rate"] == art["priors"]["symbol"][symbol]["mover_rate"]
    assert f["headline_len"] == len("Order win")
    # Unknown symbols fall back to the corpus default, they do not blow up.
    g = mm.build_features(symbol="NOT-A-REAL-SYMBOL-XYZ")
    assert g["sym_mover_rate"] == art["priors"]["defaults"]["mover_rate"]


def test_every_variant_scores_and_reports_metrics():
    for v in mm.variants():
        assert 0.0 <= v["metrics"]["roc_auc"] <= 1.0
        s = mm.score({"sym_mover_rate": 0.5}, v["key"])
        assert s is not None and 0.0 <= s.probability <= 1.0
