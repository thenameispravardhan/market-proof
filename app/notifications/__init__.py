"""Notification channels: telegram, discord, email, generic webhook.

Each channel implements the `Notifier` Protocol in `base.py`. The
`NotificationManager` (manager.py) subscribes to event-bus channels
and dispatches to all enabled notification channels whose
`events_filter` matches the event_type.

The outbound webhook (T7) is a separate layer that lives in
`app/webhooks/dispatcher.py` and uses the `webhooks` table — those
are configuration-driven fan-out for plugin extensibility, distinct
from the operator-curated notification channels.
"""
from app.notifications.base import (
    Notifier,
    NotificationContext,
    NotificationResult,
    redact_config,
)
from app.notifications.manager import NotificationManager

__all__ = [
    "Notifier",
    "NotificationContext",
    "NotificationResult",
    "redact_config",
    "NotificationManager",
]
