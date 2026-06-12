"""Announcement monitors (NSE / BSE).

A monitor owns:
  - a Playwright async context (one per monitor)
  - a polling loop that ticks every `POLL_INTERVAL_SECONDS` (default 5)
  - a fetcher that returns the raw page payload (HTML / JSON)
  - a parser that turns the payload into a list of `RawAnnouncement`
  - a dedup path: `content_hash` UNIQUE column on `announcements`
  - a publish path: `event_bus.publish("announcements.new", {...})`
  - a webhook path: `webhook_service.emit("announcement", payload)`

Each monitor is one independent asyncio task — one bad exchange does not
take the other down. Backoff is per-monitor and exponential up to 60s
on 429 / 5xx / network errors; resets on the first successful tick.
"""
