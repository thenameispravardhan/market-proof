"""Tests for the Fyers setup automation.

Covers:
  - `scripts._tunnel.detect_backend` returns None when no binary is
    on PATH; returns 'cloudflared' first when both exist.
  - `start_tunnel` raises TunnelError with a useful hint when no
    backend is installed.
  - `_read_until_url` correctly extracts cloudflared and ngrok URLs
    from process output.
  - `scripts._oauth.run_oauth_flow` opens the browser, polls the
    bot's status endpoint, and returns a successful outcome when
    the bot flips to connected.
  - `run_oauth_flow` raises OAuthError when the bot never reports
    connected.
  - `scripts.setup_fyers.cmd_status` reports the saved tunnel +
    webhook state without spawning anything.

All tests use monkey-patched subprocess + httpx.Client. The real
`cloudflared` / `ngrok` binaries are never invoked.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest


# Make sure the project root is on sys.path BEFORE importing scripts.*
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Force TESTING=1 so settings / DB use in-memory SQLite.
os.environ["TESTING"] = "1"
os.environ.setdefault("FYERS_APP_ID", "")
os.environ.setdefault("FYERS_SECRET_KEY", "")

from app import config as app_config  # noqa: E402
from app.db import init as db_init  # noqa: E402
from app.db import session as db_session_mod  # noqa: E402
from app.db.session import Base  # noqa: E402

from scripts import _tunnel  # noqa: E402
from scripts import _oauth  # noqa: E402
from scripts import setup_fyers  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _setup_db():
    app_config.reset_settings_cache()
    db_session_mod.rebuild_engine_for_testing()
    db_init.init_db()
    yield


@pytest.fixture()
def isolated_tunnel_state(tmp_path, monkeypatch):
    """Point the setup script's state files at a temp dir so the
    real project's .tunnel.json / .tunnel.pid aren't touched."""
    monkeypatch.setattr(setup_fyers, "TUNNEL_PID_FILE", tmp_path / ".tunnel.pid")
    monkeypatch.setattr(setup_fyers, "TUNNEL_INFO_FILE", tmp_path / ".tunnel.json")
    monkeypatch.setattr(setup_fyers, "PROJECT_ROOT", tmp_path)
    return tmp_path


@pytest.fixture()
def fake_completed_process():
    """A stand-in for subprocess.Popen whose stdout is an in-memory
    buffer the test can write to. We never spawn a real process."""

    def _make(lines: list[str], returncode: int = 0):
        proc = mock.MagicMock(spec=subprocess.Popen)
        proc.poll = mock.MagicMock(return_value=None)
        proc.terminate = mock.MagicMock()
        proc.kill = mock.MagicMock()
        proc.wait = mock.MagicMock(return_value=returncode)
        # send_signal is a no-op
        proc.send_signal = mock.MagicMock()
        # readline is a generator yielding the test lines, then ""
        line_iter = iter(lines + [""])

        def readline(*a, **kw):
            try:
                return next(line_iter)
            except StopIteration:
                return ""

        proc.stdout = mock.MagicMock()
        proc.stdout.readline = readline
        proc.pid = 12345
        return proc

    return _make


# ---------------------------------------------------------------------------
# _tunnel tests
# ---------------------------------------------------------------------------


def test_detect_backend_returns_none_when_nothing_on_path(monkeypatch):
    monkeypatch.setattr(_tunnel.shutil, "which", lambda cmd: None)
    assert _tunnel.detect_backend() is None


def test_detect_backend_prefers_cloudflared_over_ngrok(monkeypatch):
    def fake_which(cmd):
        return f"/usr/bin/{cmd}"
    monkeypatch.setattr(_tunnel.shutil, "which", fake_which)
    assert _tunnel.detect_backend() == "cloudflared"


def test_detect_backend_falls_back_to_ngrok(monkeypatch):
    monkeypatch.setattr(
        _tunnel.shutil, "which", lambda cmd: f"/usr/bin/{cmd}" if cmd == "ngrok" else None
    )
    assert _tunnel.detect_backend() == "ngrok"


def test_start_tunnel_raises_with_hint_when_no_backend(monkeypatch):
    monkeypatch.setattr(_tunnel.shutil, "which", lambda cmd: None)
    with pytest.raises(_tunnel.TunnelError) as exc:
        _tunnel.start_tunnel(local_port=8000)
    assert "cloudflared" in str(exc.value).lower() or "ngrok" in str(exc.value).lower()
    assert "install" in str(exc.value).lower()


def test_read_until_url_extracts_cloudflared_url(fake_completed_process, monkeypatch):
    proc = fake_completed_process(
        lines=[
            "2026-06-13T12:00:00Z INF Starting tunnel\n",
            "2026-06-13T12:00:01Z INF https://random-words.trycloudflare.com some-msg\n",
        ]
    )
    url = _tunnel._read_until_url(proc, 2.0)
    assert url.startswith("https://")
    assert "trycloudflare.com" in url


def test_read_until_url_extracts_ngrok_url(fake_completed_process, monkeypatch):
    proc = fake_completed_process(
        lines=[
            "ngrok starting\n",
            "URL: https://abc-123.ngrok-free.app\n",
        ]
    )
    url = _tunnel._read_until_url(proc, 2.0)
    assert "ngrok-free.app" in url


def test_read_until_url_raises_on_no_url(fake_completed_process):
    proc = fake_completed_process(lines=["hello world", "no url here"])
    with pytest.raises(_tunnel.TunnelError) as exc:
        _tunnel._read_until_url(proc, 0.5)
    assert "did not print a public url" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# _oauth tests
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeClient:
    """Mimics httpx.Client context manager. `responses` is a list
    of (path_suffix, status, payload) tuples the client will serve
    in order. After the list is exhausted, every path returns
    200 with an empty dict."""

    def __init__(self, responses: list[tuple[str, int, dict]]):
        self._responses = list(responses)
        self.calls: list[str] = []
        self._idx = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url: str):
        self.calls.append(url)
        # Pick the next matching queued response
        for i, (suffix, _sc, _payload) in enumerate(self._responses):
            if url.endswith(suffix):
                used = self._responses.pop(i)
                return _FakeResp(used[1], used[2])
        # Otherwise return a non-connected status (simulate "still waiting")
        return _FakeResp(200, {"connected": False, "configured": True})


def test_oauth_happy_path(monkeypatch):
    """Browser opens, bot issues URL, status flips to connected on
    the second poll."""
    client = _FakeClient(
        responses=[
            ("/api/fyers/authorize-url", 200, {
                "configured": True,
                "url": "https://api-t1.fyers.in/api/v3/generate-authcode?...",
                "state": "abc123",
            }),
            ("/api/fyers/status", 200, {
                "connected": False,
                "configured": True,
                "account_id": 7,
                "app_id": "ABC123",
            }),
            ("/api/fyers/status", 200, {
                "connected": True,
                "configured": True,
                "account_id": 7,
                "app_id": "ABC123",
            }),
        ]
    )
    browser_calls: list[str] = []
    with _oauth._install_test_hooks(
        browser_open=lambda url, **kw: browser_calls.append(url) or True,
        client_factory=lambda: client,
    ):
        outcome = _oauth.run_oauth_flow(
            bot_public_url="https://example.trycloudflare.com",
            timeout_s=5.0,
            open_browser=True,
        )
    assert outcome.ok is True
    assert outcome.state == "abc123"
    assert outcome.account_id == 7
    assert len(browser_calls) == 1
    assert "fyers.in" in browser_calls[0]


def test_oauth_times_out(monkeypatch):
    """Bot never reports connected — we must raise OAuthError."""
    client = _FakeClient(
        responses=[
            ("/api/fyers/authorize-url", 200, {
                "configured": True,
                "url": "https://api-t1.fyers.in/api/v3/generate-authcode?...",
                "state": "x",
            }),
        ]
    )
    with _oauth._install_test_hooks(
        browser_open=lambda *a, **kw: True,
        client_factory=lambda: client,
    ):
        with pytest.raises(_oauth.OAuthError) as exc:
            _oauth.run_oauth_flow(
                bot_public_url="https://example.trycloudflare.com",
                timeout_s=0.2,  # so the test is fast
                open_browser=True,
            )
    assert "did not complete" in str(exc.value).lower()


def test_oauth_refuses_when_bot_not_configured():
    client = _FakeClient(
        responses=[
            ("/api/fyers/authorize-url", 200, {
                "configured": False,
                "reason": "FYERS_APP_ID missing",
            }),
        ]
    )
    with _oauth._install_test_hooks(
        browser_open=lambda *a, **kw: True,
        client_factory=lambda: client,
    ):
        with pytest.raises(_oauth.OAuthError) as exc:
            _oauth.run_oauth_flow(
                bot_public_url="https://example.trycloudflare.com",
                timeout_s=2.0,
            )
    assert "refused" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# setup_fyers.cmd_status tests
# ---------------------------------------------------------------------------


def test_status_reports_no_tunnel(isolated_tunnel_state, capsys, monkeypatch):
    monkeypatch.setattr(setup_fyers, "_check_bot_reachable", lambda h, p: (False, None))
    rc = setup_fyers.cmd_status(mock.MagicMock())
    assert rc == 0
    out = capsys.readouterr().out
    assert "not running" in out
    assert "not registered" in out


def test_status_reports_tunnel_and_webhook(isolated_tunnel_state, capsys, monkeypatch):
    # Write a fake tunnel info
    (isolated_tunnel_state / ".tunnel.json").write_text(
        json.dumps({
            "backend": "cloudflared",
            "public_url": "https://abc.trycloudflare.com",
            "local_port": 8000,
            "pid": 99999,
            "started_at": int(time.time()),
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(setup_fyers, "_check_bot_reachable", lambda h, p: (True, {
        "fyers_configured": True,
        "trading_mode": "paper",
    }))
    # Insert a webhook row
    from app.db.models import Webhook
    with db_session_mod.SessionLocal() as s:
        s.add(Webhook(
            name="fyers-postback-orders",
            direction="in",
            event_filter="*",
            url="https://abc.trycloudflare.com/api/webhooks/in/1",
            secret="sek",
            enabled=True,
        ))
        s.commit()
    rc = setup_fyers.cmd_status(mock.MagicMock())
    assert rc == 0
    out = capsys.readouterr().out
    assert "cloudflared" in out
    assert "trycloudflare.com" in out
    assert "webhook row id" in out


# ---------------------------------------------------------------------------
# teardown tests
# ---------------------------------------------------------------------------


def test_teardown_no_info(isolated_tunnel_state, capsys):
    rc = setup_fyers.cmd_teardown(mock.MagicMock())
    assert rc == 0
    out = capsys.readouterr().out
    assert "nothing to tear down" in out


def test_teardown_kills_pid_on_linux(isolated_tunnel_state, monkeypatch, capsys):
    (isolated_tunnel_state / ".tunnel.json").write_text(
        json.dumps({
            "backend": "cloudflared",
            "public_url": "https://abc.trycloudflare.com",
            "local_port": 8000,
            "pid": 99999,
            "started_at": int(time.time()),
        }),
        encoding="utf-8",
    )
    # Simulate Linux even on Windows
    monkeypatch.setattr(setup_fyers.sys, "platform", "linux")
    killed: list[int] = []
    monkeypatch.setattr(setup_fyers.os, "kill", lambda pid, sig: killed.append(pid))
    rc = setup_fyers.cmd_teardown(mock.MagicMock())
    assert rc == 0
    assert killed == [99999]
    assert not (isolated_tunnel_state / ".tunnel.json").exists()
