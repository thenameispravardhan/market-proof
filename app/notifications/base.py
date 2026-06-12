"""Notifier interface + shared helpers.

A `Notifier` is a thin async function that takes a rendered
`NotificationContext` (event_type, subject, body, payload) and
delivers it to a third-party system. The `Manager` (manager.py)
picks the right notifier for each `NotificationChannel` row and
runs it with retries.

The `redact_config` helper is the single source of truth for
"never put these keys in a log line": we strip them before
persisting any error / config snapshot / log payload that might
leak secrets.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# Keys we treat as secret — same as `app/logging_config.py` redactor.
_SECRET_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "secret",
        "token",
        "bot_token",
        "access_token",
        "authorization",
        "api_key",
        "smtp_password",
    }
)


def redact_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return a deep-copied config with secret keys masked as '***'.

    Defence-in-depth: notifier implementations should never include
    `channel.config` in error messages or logs, but the
    `NotificationLog` row stores the rendered payload (not the
    config), and individual channels may want to dump config snippets
    on failure for debugging. This helper makes that safe.
    """
    out = copy.deepcopy(config or {})
    for k in list(out.keys()):
        if k.lower() in _SECRET_CONFIG_KEYS:
            out[k] = "***"
    return out


@dataclass(frozen=True)
class NotificationContext:
    """The unit of work for a notifier.

    `event_type`: one of the canonical event types (signal, trade,
                  risk_halt, error, ...). Channels filter on this.
    `subject`:    short one-line title (e.g. "BUY RELIANCE @ 2450").
    `body`:       multi-line human-readable message body.
    `payload`:    the raw event payload — used by the 'webhook'
                  kind to forward the JSON body verbatim.
    """

    event_type: str
    subject: str
    body: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class NotificationResult:
    """Result of one notifier.send() call (one attempt, not the full
    retry sequence — retries are handled by the manager)."""

    ok: bool
    error: str | None = None
    status_code: int | None = None


@runtime_checkable
class Notifier(Protocol):
    """Channel-specific delivery interface.

    Implementations must be async, must NEVER raise (catch
    exceptions and return `NotificationResult(ok=False, error=...)`),
    and must never log `channel.config` raw (use `redact_config` if
    you need to surface it for debugging).
    """

    async def send(
        self,
        channel_config: dict[str, Any],
        ctx: NotificationContext,
    ) -> NotificationResult: ...
