"""Email notifier tests — mock smtplib.SMTP."""
from __future__ import annotations

import smtplib
import ssl
from typing import Any, Callable
from unittest.mock import MagicMock

import pytest

from app.notifications.base import NotificationContext
from app.notifications.email import EmailNotifier


class _FakeSMTP:
    """Minimal mock that records the protocol-level calls (starttls /
    login / send_message) so tests can assert TLS was used."""

    instances: list["_FakeSMTP"] = []

    def __init__(self, host: str, port: int, timeout: float = 0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.logged_in: tuple[str, str] | None = None
        self.sent: list[Any] = []
        self.closed = False
        _FakeSMTP.instances.append(self)

    def ehlo(self) -> None:
        pass

    def starttls(self, context: ssl.SSLContext | None = None) -> None:
        self.started_tls = True

    def login(self, user: str, password: str) -> None:
        self.logged_in = (user, password)

    def send_message(self, msg, from_addr=None, to_addrs=None) -> None:
        self.sent.append((msg, from_addr, to_addrs))

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset():
    _FakeSMTP.instances = []
    yield
    _FakeSMTP.instances = []


def _factory() -> Callable[..., _FakeSMTP]:
    return _FakeSMTP


@pytest.mark.asyncio
async def test_sends_with_starttls_and_login():
    n = EmailNotifier(smtp_factory=_factory())
    res = await n.send(
        {
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "username": "u",
            "password": "p",
            "from_addr": "a@example.com",
            "to_addrs": ["b@example.com"],
            "use_tls": "starttls",
        },
        NotificationContext("signal", "BUY RELIANCE", "hello", {}),
    )
    assert res.ok
    inst = _FakeSMTP.instances[-1]
    assert inst.started_tls is True
    assert inst.logged_in == ("u", "p")
    assert len(inst.sent) == 1
    msg, from_addr, to_addrs = inst.sent[0]
    assert from_addr == "a@example.com"
    assert to_addrs == ["b@example.com"]
    # Subject should include the subject_prefix (default empty).
    assert "BUY RELIANCE" in msg["Subject"]


@pytest.mark.asyncio
async def test_sends_with_no_auth_when_no_credentials():
    n = EmailNotifier(smtp_factory=_factory())
    res = await n.send(
        {
            "smtp_host": "smtp.example.com",
            "smtp_port": 25,
            "from_addr": "a@example.com",
            "to_addrs": ["b@example.com"],
            "use_tls": "none",
        },
        NotificationContext("signal", "X", "body", {}),
    )
    assert res.ok
    inst = _FakeSMTP.instances[-1]
    assert inst.started_tls is False
    assert inst.logged_in is None
    assert len(inst.sent) == 1


@pytest.mark.asyncio
async def test_sends_with_implicit_ssl_via_smtp_ssl(monkeypatch):
    fake_ssl_inst = _FakeSMTP("smtp.example.com", 465)
    # When use_tls='ssl', EmailNotifier uses smtplib.SMTP_SSL directly;
    # we monkey-patch that class to return our fake.
    import app.notifications.email as email_mod

    class FakeSMTP_SSL(_FakeSMTP):
        def __init__(self, host, port, timeout=0, context=None):
            super().__init__(host, port, timeout)
            self.context = context

    monkeypatch.setattr(email_mod.smtplib, "SMTP_SSL", FakeSMTP_SSL)
    n = EmailNotifier()
    res = await n.send(
        {
            "smtp_host": "smtp.example.com",
            "smtp_port": 465,
            "username": "u",
            "password": "p",
            "from_addr": "a@example.com",
            "to_addrs": ["b@example.com"],
            "use_tls": "ssl",
        },
        NotificationContext("signal", "X", "body", {}),
    )
    assert res.ok
    inst = _FakeSMTP.instances[-1]
    assert inst.context is not None  # SSLContext provided
    assert inst.logged_in == ("u", "p")


@pytest.mark.asyncio
async def test_missing_required_fields_return_not_ok():
    n = EmailNotifier(smtp_factory=_factory())
    for missing_field in ("smtp_host", "from_addr"):
        cfg = {
            "smtp_host": "x",
            "from_addr": "a@b",
            "to_addrs": ["c@d"],
        }
        cfg.pop(missing_field)
        res = await n.send(
            cfg,
            NotificationContext("signal", "X", "body", {}),
        )
        assert not res.ok
        assert missing_field in (res.error or "")


@pytest.mark.asyncio
async def test_missing_to_addrs_returns_not_ok():
    n = EmailNotifier(smtp_factory=_factory())
    res = await n.send(
        {"smtp_host": "x", "from_addr": "a@b", "to_addrs": []},
        NotificationContext("signal", "X", "body", {}),
    )
    assert not res.ok


@pytest.mark.asyncio
async def test_smtp_exception_caught_and_reported():
    def bad_factory(*args, **kwargs):
        raise OSError("connection refused")

    n = EmailNotifier(smtp_factory=bad_factory)
    res = await n.send(
        {
            "smtp_host": "x",
            "from_addr": "a@b",
            "to_addrs": ["c@d"],
            "use_tls": "none",
        },
        NotificationContext("signal", "X", "body", {}),
    )
    assert not res.ok
    assert "smtp" in (res.error or "").lower()
