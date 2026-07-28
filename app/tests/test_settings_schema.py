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


# ---------------------------------------------------------------------------
# Reset / export / import — the customisation surface on top of PUT.
# ---------------------------------------------------------------------------


def test_reset_one_falls_back_to_the_code_default(client):
    default = Settings.model_fields["MAX_HOLD_SECONDS"].default
    client.put("/api/settings", json={"global": {"MAX_HOLD_SECONDS": 600}})
    assert client.get("/api/settings").json()["global"]["MAX_HOLD_SECONDS"] == 600

    r = client.delete("/api/settings/overrides/MAX_HOLD_SECONDS")
    assert r.status_code == 200, r.text
    # Regression: the handler used to return the request-scoped Settings, which
    # is resolved BEFORE the override row is deleted, so it echoed the stale
    # value and the UI showed the reset as a no-op.
    assert r.json()["global"]["MAX_HOLD_SECONDS"] == default


def test_reset_unknown_key_404s(client):
    assert client.delete("/api/settings/overrides/NOT_A_SETTING").status_code == 404


def test_bulk_reset_requires_confirmation(client):
    client.put("/api/settings", json={"global": {"MAX_HOLD_SECONDS": 600}})
    assert client.post("/api/settings/reset", json={}).status_code == 422
    assert client.get("/api/settings").json()["global"]["MAX_HOLD_SECONDS"] == 600


def test_group_reset_leaves_other_groups_alone(client):
    # Overrides persist across tests in this module, so start from a known
    # state rather than asserting on an exact reset list.
    client.post("/api/settings/reset", json={"confirm": True})
    client.put(
        "/api/settings",
        json={"global": {"STALL_ROC_PCT": 0.9, "WEEKLY_MAX_LOSS_PCT": 3.0}},
    )
    r = client.post("/api/settings/reset", json={"group": "exit_rules", "confirm": True})
    assert r.status_code == 200, r.text
    # STALL_ROC_PCT is in exit_rules; WEEKLY_MAX_LOSS_PCT is in breakers.
    assert "STALL_ROC_PCT" in r.json()["reset"]
    assert "WEEKLY_MAX_LOSS_PCT" not in r.json()["reset"]
    assert r.json()["global"]["WEEKLY_MAX_LOSS_PCT"] == 3.0


def test_export_carries_only_overrides_not_the_whole_config(client):
    client.put("/api/settings", json={"global": {"MAX_HOLD_SECONDS": 600}})
    overrides = client.get("/api/settings/export").json()["overrides"]
    assert overrides["MAX_HOLD_SECONDS"] == 600
    # Exporting effective values would pin every key on import, silently
    # freezing them against future default changes.
    assert "STALL_ROC_PCT" not in overrides


def test_import_round_trips_and_reports_unknown_keys(client):
    client.put("/api/settings", json={"global": {"MAX_HOLD_SECONDS": 600}})
    exported = client.get("/api/settings/export").json()
    client.post("/api/settings/reset", json={"confirm": True})

    exported["overrides"]["SOME_REMOVED_KEY"] = 1
    r = client.post("/api/settings/import", json={"overrides": exported["overrides"]})
    assert r.status_code == 200, r.text
    assert "MAX_HOLD_SECONDS" in r.json()["imported"]
    assert r.json()["ignored"] == ["SOME_REMOVED_KEY"]
    assert r.json()["global"]["MAX_HOLD_SECONDS"] == 600


def test_import_validates_like_a_normal_put(client):
    assert client.post(
        "/api/settings/import", json={"overrides": {"INTRADAY_LEVERAGE": 99}}
    ).status_code == 422


def test_schema_reports_which_keys_are_overridden(client):
    client.post("/api/settings/reset", json={"confirm": True})
    client.put("/api/settings", json={"global": {"MAX_HOLD_SECONDS": 600}})
    body = client.get("/api/settings/schema").json()
    fields = {f["key"]: f for g in body["groups"] for f in g["fields"]}
    assert fields["MAX_HOLD_SECONDS"]["overridden"] is True
    assert fields["STALL_ROC_PCT"]["overridden"] is False
    # Not an exact count: a bulk reset deliberately spares read-only keys like
    # TRADING_MODE, which other test modules set.
    assert body["overridden_count"] == sum(
        1 for f in fields.values() if f["overridden"]
    )
