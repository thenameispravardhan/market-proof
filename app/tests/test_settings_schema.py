"""The settings registry must stay in lockstep with `Settings`.

These are the guards that stop the failure this module was built to fix: a knob
declared in config.py that never reaches the UI, and a parallel key list whose
defaults quietly drift from the model's.
"""
from __future__ import annotations

import pytest

from app.api.settings_schema import (
    EXCLUDED,
    GROUPS,
    build_registry,
    schema_payload,
)
from app.config import Settings


def test_every_setting_is_exposed_or_deliberately_excluded():
    """A new field in config.py is operator-editable by default.

    If this fails you added a Settings field: either it belongs in the UI (do
    nothing, it is already there) or it is a secret/boot-time key that must be
    named in settings_schema.EXCLUDED.
    """
    registry = build_registry()
    unaccounted = set(Settings.model_fields) - set(registry) - EXCLUDED
    assert not unaccounted, (
        f"settings not exposed and not excluded: {sorted(unaccounted)}. "
        "Add them to EXCLUDED if they are credentials or boot-time config."
    )


def test_registry_defaults_match_the_model():
    """The registry reports Settings' own defaults, never a second copy."""
    for key, spec in build_registry().items():
        assert spec["default"] == Settings.model_fields[key].default, (
            f"{key}: registry default {spec['default']!r} != model default "
            f"{Settings.model_fields[key].default!r}"
        )


def test_no_credentials_leak_into_the_registry():
    registry = build_registry()
    for key in ("DEEPSEEK_API_KEY", "FYERS_SECRET_KEY", "FYERS_ACCESS_TOKEN"):
        assert key not in registry, f"{key} must never be exposed via /api/settings"


def test_every_field_lands_in_a_named_group():
    """'Other' is the unclassified bucket — it should stay empty."""
    payload = schema_payload({})
    other = [g for g in payload if g["id"] == "other"]
    assert not other, (
        "unclassified settings: "
        f"{[f['key'] for f in other[0]['fields']]} — add a prefix to GROUPS"
    )


def test_group_ids_are_unique():
    ids = [g["id"] for g in GROUPS]
    assert len(ids) == len(set(ids)), f"duplicate group ids: {ids}"


@pytest.mark.parametrize(
    "key",
    ["MAX_CAPITAL_RISK_PCT", "SQUARE_OFF_TIME_IST", "EDGE_GATE_ENABLED", "MAX_HOLD_SECONDS"],
)
def test_known_keys_carry_usable_widget_metadata(key):
    spec = build_registry()[key]
    assert spec["label"], f"{key} has no label"
    assert spec["widget"] in {"number", "toggle", "time", "text", "select"}
    if spec["widget"] == "number":
        assert spec["min"] is not None and spec["max"] is not None
        assert spec["min"] < spec["max"], f"{key}: empty range"


def test_defaults_sit_inside_their_own_declared_bounds():
    """A default outside its widget range would make the UI show an invalid
    value on first load and reject an unchanged save."""
    bad = []
    for key, spec in build_registry().items():
        if spec["widget"] != "number" or spec["default"] is None:
            continue
        if not (spec["min"] <= spec["default"] <= spec["max"]):
            bad.append(f"{key}={spec['default']} not in [{spec['min']}, {spec['max']}]")
    assert not bad, "defaults outside their bounds: " + "; ".join(bad)
