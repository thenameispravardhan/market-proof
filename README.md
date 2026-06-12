# AI News Trading Bot — Infrastructure (T1)

Foundation for an AI-powered news trading bot. This track ships:

- FastAPI app with `/health`, `/api/settings`, `/ws`
- SQLite schema with 14 tables (6 core + 8 advanced)
- Pydantic-settings based config (`.env` driven)
- JSON structured logging with correlation IDs
- In-process event bus (asyncio.Queue per subscriber)
- Pytest suite (in-memory SQLite + TestClient)
- **No authentication** — local-first single-user

## Install

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
# source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env       # then edit secrets
```

## Run

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Open <http://127.0.0.1:8000/health> and <http://127.0.0.1:8000/docs>.

## Test

```bash
pytest -q
```

## Security: local-first, no auth

The app binds to `127.0.0.1` by default and ships **without any authentication**.
This is intentional: the system is designed to run on a single operator's
machine. There is exactly one user, and they have full filesystem + network
access already.

> **Do not bind to `0.0.0.0` unless you are on a trusted LAN with no untrusted
> users.** Every endpoint — including `/api/settings` (which can flip
> `TRADING_MODE` to `live`) — is wide open. If you need remote access, put the
> app behind a reverse proxy with mTLS / SSO.

If you change `BIND_HOST=0.0.0.0`, the server logs a loud warning at startup.

## Public contracts (for sibling tracks)

### Event bus — `app.services.event_bus`

In-process pub/sub. One `asyncio.Queue` per subscriber.

```python
from app.services.event_bus import event_bus

# Publish (non-blocking, drops if subscriber queue is full)
await event_bus.publish("settings.updated", {"key": "TRADING_MODE"})

# Subscribe
async def my_worker():
    sub = event_bus.subscribe("settings.updated")
    try:
        while True:
            event = await sub.get()        # type: Event
            # event.channel, event.payload, event.event_id, event.ts
    finally:
        event_bus.unsubscribe("settings.updated", sub)
```

- `event_bus.subscribe(channel) -> Queue` (must be `unsubscribe`d)
- `event_bus.unsubscribe(channel, queue)`
- `await event_bus.publish(channel, payload)` — fire-and-forget; backpressure
  by default drops on full queues (logged as warning)
- `await event_bus.publish_blocking(channel, payload)` — awaits drain of full
  queues; use sparingly
- Channels used in v1: `settings.updated`, `signal.created`, `trade.executed`,
  `risk.halt`, `log`. Use any string; subscribers match exact channel.

### WebSocket — `GET /ws`

```text
Server frames (JSON):
  {"type": "connected", "ts": "..."}
  {"type": "event",    "channel": "...", "payload": {...}, "ts": "..."}
  {"type": "ping",     "ts": "..."}
  {"type": "pong",     "ts": "..."}

Client frames:
  {"type": "subscribe",   "channels": ["settings.updated", "signal.created"]}
  {"type": "unsubscribe", "channels": ["..."]}
  {"type": "ping"}
```

Connections authenticate by source IP (`127.0.0.1` only by default).

### Settings API — `/api/settings`

```http
GET /api/settings
  -> 200 {"global": {TRADING_MODE, MAX_CAPITAL_RISK_PCT, ...}, "sections": {...}}

PUT /api/settings
  Body: {"global": {...}, "sections": {...}}  # partial updates ok
  -> 200 updated settings
  -> 422 invalid range / unknown key
  -> 409 if a value fails cross-validation (e.g. live without fyers token)
```

Side effect: writes to `audit_log` and broadcasts `settings.updated` on the
event bus.

### Database — `app.db.models`

All ORM models are importable from `app.db.models`. Use `app.db.session.SessionLocal`
as the session factory, and `app.db.init.init_db()` to create tables.

Models: `Strategy`, `BrokerAccount`, `SignalRule`, `PromptTemplate`, `PromptHistory`,
`Webhook`, `WebhookDelivery`, `NotificationChannel`, `NotificationLog`,
`BacktestRun`, `AuditLog`, plus the 6 core models from the original plan
(`Announcement`, `Analysis`, `Signal`, `Trade`, `Position`, `RiskEvent`).

**Announcement dedupe:** the `announcements` table has a nullable
`content_hash VARCHAR(64)` column with a `UNIQUE(content_hash)` constraint.
T2 (scraper) computes the hash via `app.db.models.compute_content_hash(...)`
(SHA-256 hex of normalised `exchange|symbol|filed_at|pdf_url`) and uses the
unique constraint to dedupe repeat filings.

## Layout

```
app/
  __init__.py
  main.py
  config.py
  logging_config.py
  api/
    health.py
    settings_api.py
    ws.py
  db/
    session.py
    models.py
    init.py
  services/
    event_bus.py
  tests/
    conftest.py
    test_db.py
    test_health.py
    test_settings_api.py
```

## Logging

JSON lines to stdout. Each request gets a `correlation_id` (from
`X-Request-ID` header, or generated). Secrets are redacted by name (any key
containing `KEY`, `TOKEN`, `SECRET`, `PASSWORD` is replaced with `***` before
emission).
