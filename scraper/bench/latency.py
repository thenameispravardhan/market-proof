"""Real-feed latency: poll feeds every --every seconds for --minutes in one process, then print --report.

  python bench/latency.py feeds.txt --minutes 375 --db latency.db --sources sources.example.json

Run it in market hours (09:15-15:30 IST, Mon-Fri): off-hours feeds barely change, so there is nothing to time.
The report's "feed pub->seen" is publication -> first seen per source (a feed's first-poll backlog excluded),
"gaps" counts polls where every item was new (stories may have scrolled out between polls).
Per-source ttl / delay / budget / pause in --sources still decide what is actually requested.
"""
import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import scrape  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("feeds", help="file with one feed URL per line")
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--every", type=float, default=60, help="seconds between poll starts")
    ap.add_argument("--db", default="latency.db")
    ap.add_argument("--sources", default=None)
    a = ap.parse_args()
    feeds = [ln.strip() for ln in open(a.feeds, encoding="utf8") if ln.strip()]
    argv = ["--db", a.db, "--workers", "2"] + (["--sources", a.sources] if a.sources else [])
    end, n, took = time.time() + a.minutes * 60, 0, []
    while time.time() < end:
        t0 = time.time()
        asyncio.run(scrape.Crawler(scrape.parse_args(argv)).run(feeds))
        n += 1
        took.append(time.time() - t0)
        print(f"poll {n}: {took[-1]:.1f}s", flush=True)
        time.sleep(max(0.0, a.every - (time.time() - t0)))
    print(f"\n{n} polls; poll time median {sorted(took)[len(took) // 2]:.1f}s, max {max(took):.1f}s\n")
    scrape.Crawler(scrape.parse_args(argv)).report()
