"""JSON structured logging with secret redaction and correlation IDs."""
from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog

# Context-local correlation id. Set per-request by middleware, included
# in every log line emitted while the request is in flight.
correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)

# Compound secret names — substring match (case-insensitive). These are
# the snake_case / camelCase variants of the common credential fields;
# they match anywhere inside a key so `{"refreshToken": ...}` is caught
# just like `{"refresh_token": ...}`.
_SECRET_PATTERNS: tuple[str, ...] = (
    "api_key", "apikey",
    "access_token", "accesstoken",
    "auth_token", "authtoken", "authtokens",
    "refresh_token", "refreshtoken",
    "session_token", "sessiontoken",
    "csrf_token", "csrftoken",
    "bearer_token", "bearertoken",
    "secret_key", "secretkey",
    "private_key", "privatekey",
    "client_secret", "clientsecret",
    "app_secret", "appsecret",
    "password_hash", "passwd_hash", "pwd_hash",
    "x-api-key", "x_api_key",
    "authorization",  # both header name and field name in responses
)

# Standalone secret names — exact match ONLY (case-insensitive). These
# are short common words that double as field names; matching them as
# substrings would false-positive on `keys`, `keyword`, `tokenizer`,
# `passwordless`, etc., so we only fire when the key IS one of these
# words (nothing else attached).
_STANDALONE_SECRETS: frozenset[str] = frozenset(
    s.lower()
    for s in (
        "key", "token", "secret", "password", "passwd", "pwd",
        "cookie", "credential", "credentials",
    )
)

_REDACTED = "***"


def _is_secret_key(k: str) -> bool:
    """True when `k` (a dict key) looks like a secret field name."""
    lk = k.lower()
    if lk in _STANDALONE_SECRETS:
        return True
    return any(p in lk for p in _SECRET_PATTERNS)


def _redact(value: Any) -> Any:
    """Recursively redact secret-like keys in a dict/list structure."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and _is_secret_key(k):
                out[k] = _REDACTED
            else:
                out[k] = _redact(v)
        return out
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact(v) for v in value)
    return value


def _add_correlation_id(_, __, event_dict: dict[str, Any]) -> dict[str, Any]:
    cid = correlation_id_var.get()
    if cid is not None:
        event_dict.setdefault("correlation_id", cid)
    return event_dict


def _redact_secrets(_, __, event_dict: dict[str, Any]) -> dict[str, Any]:
    return _redact(event_dict)


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog + stdlib logging. Idempotent."""
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Stdlib root — we route everything through structlog's ProcessorFormatter
    handler = logging.StreamHandler(sys.stdout)
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
        foreign_pre_chain=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _add_correlation_id,
            _redact_secrets,
        ],
    )
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)

    # Quiet noisy third-party loggers a bit
    for noisy in ("uvicorn.access", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(max(log_level, logging.WARNING))

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _add_correlation_id,
            _redact_secrets,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
