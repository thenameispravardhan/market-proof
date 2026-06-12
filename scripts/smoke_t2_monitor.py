"""30-second smoke test: drive `MonitorManager` with a stub fetcher
that emits a few announcements over time, confirm rows land in the
DB, and that the event_bus sees one publish per new row.

This is a manual smoke run — NOT a pytest test. Invoke from the
project root with:

    .venv/Scripts/python.exe scripts/smoke_t2_monitor.py

The script deliberately does NOT touch the production data/
directory; it points the app at a temp sqlite file via env vars.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Set env BEFORE importing app modules.
_TMP = Path(tempfile.mkdtemp(prefix="t2_smoke_"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'smoke.db'}"
os.environ["POLL_INTERVAL_SECONDS"] = "2"  # speed up for smoke
os.environ["LOG_LEVEL"] = "INFO"
# Ensure we do not pick up an .env file that overrides us.
os.environ["TESTING"] = "0"

# Make sure the project root is on sys.path so `app` resolves.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


async def main() -> int:
    from app.config import reset_settings_cache
    from app.db import init as db_init
    from app.db.models import Announcement
    from app.db.session import SessionLocal
    from app.logging_config import configure_logging, get_logger
    from app.monitors.base import BaseMonitor, RawAnnouncement
    from app.services.event_bus import event_bus

    reset_settings_cache()
    configure_logging("INFO")
    log = get_logger("smoke")

    # Init schema.
    db_init.init_db()

    # Build a canned payload pool: 3 distinct announcements that will
    # be served in rotation, so the monitor sees new content each tick.
    nse_payloads = [
        RawAnnouncement(
            company="RELIANCE",
            title=f"RELIANCE update {i}",
            posted_at=datetime.now(timezone.utc) - timedelta(minutes=i),
            pdf_url=f"https://nse.example/r{i}.pdf",
        )
        for i in range(3)
    ]
    bse_payloads = [
        RawAnnouncement(
            company="TCS",
            title=f"TCS update {i}",
            posted_at=datetime.now(timezone.utc) - timedelta(minutes=i),
            pdf_url=f"https://bse.example/t{i}.pdf",
        )
        for i in range(3)
    ]

    nse_iter = iter(nse_payloads)
    bse_iter = iter(bse_payloads)

    def _nse_stub_fetch(_url):
        async def _f():
            try:
                return next(nse_iter)
            except StopIteration:
                # Repeat the last one to keep the parser returning
                # the same content (so the dedup check fires).
                return nse_payloads[-1]
        return _f()

    def _bse_stub_fetch(_url):
        async def _f():
            try:
                return next(bse_iter)
            except StopIteration:
                return bse_payloads[-1]
        return _f()

    def _nse_parser(raw_a, _url):
        return [raw_a]

    def _bse_parser(raw_a, _url):
        return [raw_a]

    nse = BaseMonitor(
        fetcher=_nse_stub_fetch,
        parser=_nse_parser,
        poll_interval=2.0,
        source_url="https://stub-nse",
    )
    nse.exchange = "NSE"
    bse = BaseMonitor(
        fetcher=_bse_stub_fetch,
        parser=_bse_parser,
        poll_interval=2.0,
        source_url="https://stub-bse",
    )
    bse.exchange = "BSE"

    # Subscribe so we can count publishes.
    publishes: list[dict] = []
    sub = event_bus.subscribe("announcements.new")

    async def _drain():
        while True:
            evt = await sub.get()
            publishes.append(evt.payload)

    drain_task = asyncio.create_task(_drain())

    nse.start()
    bse.start()
    log.info("smoke.start", duration_s=30, tick_interval_s=2.0)
    t0 = time.perf_counter()
    try:
        # Run for ~30s.
        await asyncio.sleep(30)
    finally:
        nse.stop()
        bse.stop()
        await asyncio.gather(nse.wait_until_stopped(), bse.wait_until_stopped())
        drain_task.cancel()
        try:
            await drain_task
        except asyncio.CancelledError:
            pass

    elapsed = time.perf_counter() - t0
    # Check rows.
    with SessionLocal() as s:
        rows = s.query(Announcement).all()
    log.info(
        "smoke.done",
        elapsed_s=round(elapsed, 2),
        row_count=len(rows),
        publish_count=len(publishes),
        exchanges=sorted({r.exchange for r in rows}),
        symbols=sorted({r.symbol for r in rows}),
    )

    # Validate: at least 2 rows, at least 2 publishes, both exchanges represented.
    assert len(rows) >= 2, f"expected >=2 rows, got {len(rows)}"
    assert len(publishes) >= 2, f"expected >=2 publishes, got {len(publishes)}"
    exchanges = {r.exchange for r in rows}
    assert exchanges == {"NSE", "BSE"}, f"missing exchanges: {exchanges}"

    # Cleanup temp DB.
    try:
        shutil.rmtree(_TMP, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass

    print(f"SMOKE OK: {len(rows)} rows, {len(publishes)} publishes, "
          f"exchanges={sorted(exchanges)}, elapsed={elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
