#!/usr/bin/env python
"""Check every News page feed the way the collector reads it: one GET with its user agent, client and parser
(app/services/news_wire.py), plus robots.txt for the feed's path and how fresh the newest headline is.

    python scripts/check_news_feeds.py                     # every feed in DEFAULT_FEEDS
    python scripts/check_news_feeds.py --source NSE --source "ET IPO"
    python scripts/check_news_feeds.py --summary           # also a Markdown table into $GITHUB_STEP_SUMMARY
    python scripts/check_news_feeds.py --json out.json

Report only: exit 0 whatever the publishers answer (a site being down is no bug); exit 2 when the check itself
breaks. The CI job "News feeds" runs it whenever the feed list changes, from a GitHub runner, which reaches
publishers a development sandbox may not.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

os.environ.setdefault("TESTING", "1")              # never touch the real DB: this only reads feeds
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from protego import Protego  # noqa: E402

from app.services import news_wire as nw  # noqa: E402

FRESH_DAYS = 7                                     # a feed whose newest headline is older than this is stale


async def check(feed: dict, client, robots: dict[str, Protego | None]) -> dict:
    url, t0 = feed["url"], time.monotonic()
    out = {"source": feed["source"], "url": url, "group": feed.get("group", ""), "status": None, "items": 0,
           "dated": 0, "newest_h": None, "robots": None, "ms": None, "error": None, "ok": False}
    host = urlsplit(url)
    key = f"{host.scheme}://{host.netloc}"
    if key not in robots:                          # one robots.txt per host, like the scraper
        try:
            r = await client.get(f"{key}/robots.txt")
            robots[key] = Protego.parse(r.text) if r.status_code == 200 else None
        except Exception:  # noqa: BLE001 — no robots.txt reachable: judged by the feed alone
            robots[key] = None
    rp = robots[key]
    out["robots"] = True if rp is None else rp.can_fetch(url, nw.UA)
    try:
        r = await client.get(url)
        out["status"], out["ms"] = r.status_code, int((time.monotonic() - t0) * 1000)
        if r.status_code == 200:
            items = nw.parse_feed(r.content[:nw.MAX_BYTES], ist=feed.get("ist", False), subject=feed.get("subject", False))
            dates = [i["published_at"] for i in items if i["published_at"]]
            out["items"], out["dated"] = len(items), len(dates)
            if dates:                              # negative: the feed dates stories in the future (a wrong zone)
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                out["newest_h"] = round((now - max(dates)).total_seconds() / 3600, 1)
            out["sample"] = [i["title"] for i in items[:2]]
            if not items:
                out["error"] = f"no headlines ({r.headers.get('content-type', '?')})"
        else:
            out["error"] = f"HTTP {r.status_code}"
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    fresh = out["newest_h"] is None or out["newest_h"] <= FRESH_DAYS * 24
    out["ok"] = bool(out["status"] == 200 and out["items"] >= 3 and out["robots"] and fresh)
    if out["ok"] is False and not out["error"]:
        out["error"] = "robots.txt disallows" if not out["robots"] else "stale" if not fresh else "under 3 headlines"
    return out


async def run(feeds: list[dict]) -> list[dict]:
    client = nw.NewsWire().client()
    robots: dict[str, Protego | None] = {}
    sem = asyncio.Semaphore(6)

    async def one(f: dict) -> dict:
        async with sem:
            return await check(f, client, robots)
    try:
        return await asyncio.gather(*(one(f) for f in feeds))
    finally:
        await client.aclose()


def table(rows: list[dict]) -> str:
    lines = ["| | Source | Group | HTTP | Headlines | Dated | Newest | robots | ms | Problem |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: (not r["ok"], r["group"], r["source"])):
        on = " (on)" if nw.DEFAULTS["sources"].get(r["source"], True) else ""
        newest = "" if r["newest_h"] is None else f"{r['newest_h']} h"
        lines.append(f"| {'✅' if r['ok'] else '❌'} | {r['source']}{on} | {r['group']} | {r['status'] or '–'} | {r['items']} "
                     f"| {r['dated']} | {newest} | {'yes' if r['robots'] else 'NO'} | {r['ms'] or '–'} | {r['error'] or ''} |")
    ok = sum(r["ok"] for r in rows)
    return "\n".join(lines) + f"\n\n{ok}/{len(rows)} feeds OK (checked {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC)\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", action="append", help="only this source (repeatable)")
    ap.add_argument("--summary", action="store_true", help="append the table to $GITHUB_STEP_SUMMARY")
    ap.add_argument("--json", metavar="FILE", help="write every result as JSON")
    a = ap.parse_args()
    feeds = [f for f in nw.DEFAULT_FEEDS if not a.source or f["source"] in a.source]
    if not feeds:
        print("no such source", file=sys.stderr)
        return 2
    rows = asyncio.run(run(feeds))
    md = table(rows)
    print(md)
    if a.summary and os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write("## News feeds\n\n" + md)
    if a.json:
        Path(a.json).write_text(json.dumps(rows, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        print(f"check failed: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)
