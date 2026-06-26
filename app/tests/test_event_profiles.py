"""Tests for app/risk/event_profiles.py — the event-type trade matrix."""
from __future__ import annotations

from app.config import get_settings
from app.risk import event_profiles


def test_profile_for_unknown_is_default():
    assert event_profiles.profile_for("NOPE") is event_profiles.DEFAULT_PROFILE
    assert event_profiles.profile_for(None) is event_profiles.DEFAULT_PROFILE


def test_profile_for_is_case_insensitive():
    assert event_profiles.profile_for("order_win") is event_profiles.EVENT_PROFILES["ORDER_WIN"]


def test_default_profile_resolves_to_settings():
    s = get_settings()
    r = event_profiles.DEFAULT_PROFILE.resolved(s)
    assert r.sl_atr_mult == s.ATR_STOP_MULT
    assert r.sl_default_pct == s.DEFAULT_SL_PCT
    assert r.target_rr == s.DEFAULT_TARGET_RR
    assert r.max_hold_seconds == s.MAX_HOLD_SECONDS
    assert r.min_confidence == s.MIN_SENTIMENT_CONFIDENCE


def test_order_win_runs_wider_and_longer():
    s = get_settings()
    r = event_profiles.profile_for("ORDER_WIN").resolved(s)
    assert r.target_rr >= 3.5
    assert r.max_hold_seconds >= s.MAX_HOLD_SECONDS


def test_priced_in_event_raises_confidence_bar():
    s = get_settings()
    div = event_profiles.profile_for("DIVIDEND").resolved(s)
    # Priced-in events demand stronger conviction and a tighter stop.
    assert div.min_confidence >= s.MIN_SENTIMENT_CONFIDENCE
    assert div.min_confidence >= 0.75
    assert div.max_hold_seconds <= s.MAX_HOLD_SECONDS


def test_min_confidence_never_below_global_floor():
    s = get_settings()
    # Even a profile that sets a low min_confidence can't drop below the
    # global floor (resolved() takes the max of the two).
    p = event_profiles.EventProfile(min_confidence=0.1)
    assert p.resolved(s).min_confidence == s.MIN_SENTIMENT_CONFIDENCE


def test_every_listed_event_resolves():
    s = get_settings()
    for name in event_profiles.EVENT_PROFILES:
        r = event_profiles.profile_for(name).resolved(s)
        assert r.sl_atr_mult > 0
        assert r.target_rr > 0
        assert r.max_hold_seconds > 0
        assert 0 < r.min_confidence <= 1.0
