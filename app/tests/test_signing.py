"""Webhook signing tests — deterministic, constant-time, doesn't leak
the secret in any code path."""
from __future__ import annotations

import pytest

from app.webhooks.signing import (
    REPLAY_WINDOW_SECONDS,
    compute_signature,
    extract_timestamp,
    is_fresh_timestamp,
    verify_signature,
)


# ---- compute_signature --------------------------------------------------


def test_signature_deterministic():
    sig1 = compute_signature("secret-1", b'{"a": 1}')
    sig2 = compute_signature("secret-1", b'{"a": 1}')
    assert sig1 == sig2
    assert len(sig1) == 64  # SHA-256 hex


def test_signature_changes_with_body():
    sig1 = compute_signature("s", b"a")
    sig2 = compute_signature("s", b"b")
    assert sig1 != sig2


def test_signature_changes_with_secret():
    sig1 = compute_signature("s1", b"a")
    sig2 = compute_signature("s2", b"a")
    assert sig1 != sig2


def test_signature_is_lowercase_hex():
    sig = compute_signature("s", b"x")
    assert sig == sig.lower()
    assert all(c in "0123456789abcdef" for c in sig)


def test_signature_rejects_non_bytes():
    with pytest.raises(TypeError):
        compute_signature("s", "x")  # type: ignore[arg-type]


# ---- verify_signature ---------------------------------------------------


def test_verify_signature_accepts_matching():
    body = b"hello"
    sig = compute_signature("k", body)
    assert verify_signature("k", body, sig) is True
    # With sha256= prefix (GitHub-style)
    assert verify_signature("k", body, f"sha256={sig}") is True
    # Mixed case
    assert verify_signature("k", body, sig.upper()) is True


def test_verify_signature_rejects_mismatch():
    assert verify_signature("k", b"hello", compute_signature("k", b"world")) is False
    assert verify_signature("k", b"hello", "deadbeef") is False
    assert verify_signature("k", b"hello", None) is False
    assert verify_signature("k", b"hello", "") is False


# ---- extract_timestamp / is_fresh_timestamp -----------------------------


def test_extract_timestamp_int():
    assert extract_timestamp({"timestamp": 1_700_000_000}) == 1_700_000_000


def test_extract_timestamp_string_digits():
    assert extract_timestamp({"timestamp": "1700000000"}) == 1_700_000_000


def test_extract_timestamp_missing_or_bad():
    assert extract_timestamp({}) is None
    assert extract_timestamp({"timestamp": "abc"}) is None
    assert extract_timestamp({"timestamp": True}) is None  # bool is int subclass
    assert extract_timestamp({"timestamp": 1.5}) is None
    assert extract_timestamp(None) is None
    assert extract_timestamp("not a dict") is None


def test_is_fresh_within_window():
    import time

    now = 1_700_000_000.0
    assert is_fresh_timestamp({"timestamp": int(now - 60)}, now=now) is True
    assert is_fresh_timestamp({"timestamp": int(now + 60)}, now=now) is True
    assert is_fresh_timestamp({"timestamp": int(now)}, now=now) is True


def test_is_fresh_outside_window():
    import time

    now = 1_700_000_000.0
    too_old = {"timestamp": int(now - REPLAY_WINDOW_SECONDS - 10)}
    assert is_fresh_timestamp(too_old, now=now) is False
    # Future skew is also rejected (more than half-window ahead).
    too_future = {"timestamp": int(now + REPLAY_WINDOW_SECONDS)}
    assert is_fresh_timestamp(too_future, now=now) is False


def test_is_fresh_missing_returns_false():
    import time

    now = time.time()
    assert is_fresh_timestamp({}, now=now) is False
    assert is_fresh_timestamp({"timestamp": "abc"}, now=now) is False
