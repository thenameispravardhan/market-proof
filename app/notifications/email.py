"""Email channel — SMTP send with optional TLS (STARTTLS or implicit).

Config:
  smtp_host     required
  smtp_port     int, default 587
  username      optional
  password      optional (we redact it in logs)
  from_addr     required
  to_addrs      list[str], required
  use_tls       "starttls" (default) | "ssl" | "none"
  subject_prefix optional, e.g. "[TradingBot] "

We use `smtplib.SMTP` / `SMTP_SSL` (stdlib, no extra dependency).
The notifier is async-friendly via `asyncio.to_thread` because
`smtplib` is blocking.
"""
from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any, Optional

from app.logging_config import get_logger
from app.notifications.base import NotificationContext, NotificationResult

log = get_logger(__name__)


class EmailNotifier:
    def __init__(
        self,
        *,
        smtp_factory: Optional[Any] = None,
    ) -> None:
        # smtp_factory: optional callable `(host, port, timeout) -> smtplib.SMTP`
        # for tests to inject a mock. Defaults to `smtplib.SMTP`.
        self._smtp_factory = smtp_factory or smtplib.SMTP

    async def send(
        self,
        channel_config: dict[str, Any],
        ctx: NotificationContext,
    ) -> NotificationResult:
        host = (channel_config.get("smtp_host") or "").strip()
        from_addr = (channel_config.get("from_addr") or "").strip()
        to_addrs = channel_config.get("to_addrs") or []
        if not host:
            return NotificationResult(ok=False, error="email: missing smtp_host")
        if not from_addr:
            return NotificationResult(ok=False, error="email: missing from_addr")
        if not to_addrs:
            return NotificationResult(ok=False, error="email: missing to_addrs")
        try:
            port = int(channel_config.get("smtp_port") or 587)
        except (TypeError, ValueError):
            return NotificationResult(ok=False, error="email: smtp_port must be int")
        username = channel_config.get("username") or None
        password = channel_config.get("password") or None
        use_tls = (channel_config.get("use_tls") or "starttls").lower()
        subject_prefix = channel_config.get("subject_prefix") or ""

        msg = EmailMessage()
        msg["From"] = from_addr
        msg["To"] = ", ".join(str(a) for a in to_addrs)
        msg["Subject"] = f"{subject_prefix}{ctx.subject}"
        msg.set_content(f"{ctx.subject}\n\n{ctx.body}\n")

        try:
            await asyncio.to_thread(
                self._send_blocking,
                host=host,
                port=port,
                username=username,
                password=password,
                from_addr=from_addr,
                to_addrs=list(to_addrs),
                msg=msg,
                use_tls=use_tls,
            )
        except Exception as e:  # noqa: BLE001
            return NotificationResult(ok=False, error=f"smtp: {e!r}")
        return NotificationResult(ok=True)

    def _send_blocking(
        self,
        *,
        host: str,
        port: int,
        username: Optional[str],
        password: Optional[str],
        from_addr: str,
        to_addrs: list[str],
        msg: EmailMessage,
        use_tls: str,
    ) -> None:
        if use_tls == "ssl":
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, timeout=15, context=ctx) as s:
                if username and password:
                    s.login(username, password)
                s.send_message(msg, from_addr=from_addr, to_addrs=to_addrs)
        else:
            with self._smtp_factory(host, port, timeout=15) as s:
                s.ehlo()
                if use_tls == "starttls":
                    s.starttls(context=ssl.create_default_context())
                    s.ehlo()
                if username and password:
                    s.login(username, password)
                s.send_message(msg, from_addr=from_addr, to_addrs=to_addrs)
