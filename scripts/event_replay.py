"""Cost-aware event replay over the whole signal history.

Replays every recorded signal (taken, blocked, and HOLD signals on the
analysis' side) through the bot's own stop/target/hold rules on real
1-minute candles, charges Indian intraday costs, and writes:

    data/research/replay-<stamp>.json     the report (CIs, by block reason,
                                          by event type, confidence buckets)
    data/research/replay-<stamp>.csv      one row per simulated trade

Usage (from the repo root, with the bot's venv):

    python scripts/event_replay.py                         # all history, defaults
    python scripts/event_replay.py --days 90 --delay 30 --slippage-bps 10
    python scripts/event_replay.py --signals-only          # skip HOLD hypotheticals
    python scripts/event_replay.py --sweep-delay 5,20,60   # latency sensitivity

Read-only against the database; safe to run while the bot is live (it reads
a snapshot, and candle files are only read).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=0, help="only signals from the last N days (0 = all)")
    ap.add_argument("--delay", type=float, default=None, help="entry delay after the signal, seconds")
    ap.add_argument("--slippage-bps", type=float, default=None, help="adverse slippage per fill, bps")
    ap.add_argument("--notional", type=float, default=None, help="rupees per trade (sets qty and costs)")
    ap.add_argument("--signals-only", action="store_true", help="skip HOLD signals (no hypothetical side)")
    ap.add_argument("--sweep-delay", default="", help="comma-separated delays to compare, e.g. 5,20,60")
    ap.add_argument("--meta", action="store_true",
                    help="also fit and evaluate the meta-labeling model on the replayed trades")
    ap.add_argument("--out", default=str(ROOT / "data" / "research"), help="output directory")
    args = ap.parse_args(argv)

    from app.config import get_settings
    from app.db.session import SessionLocal
    from app.research import replay as rp

    settings = get_settings()
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).replace(tzinfo=None) if args.days else None
    with SessionLocal() as db:
        events = rp.load_events(db, since=since, include_hypothetical=not args.signals_only)
    print(f"loaded {len(events)} events", file=sys.stderr)

    source = rp.ParquetCandleSource()
    capital = float(getattr(settings, "PORTFOLIO_VALUE", 1e6))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    if args.sweep_delay:
        sweep = {}
        for d in [float(x) for x in args.sweep_delay.split(",") if x.strip()]:
            p = rp.ReplayParams.from_settings(settings, entry_delay_s=d, slippage_bps=args.slippage_bps,
                                              notional=args.notional)
            rep = rp.report(rp.run_replay(events, source, p), capital=capital)
            sweep[str(d)] = {"all": rep["all_directional"], "taken": rep["taken"]}
            print(f"delay {d:>6.1f}s  n={rep['all_directional']['n']:>6}  "
                  f"E[R]={rep['all_directional']['expectancy_r']}  "
                  f"CI={rep['all_directional']['expectancy_r_ci95']}", file=sys.stderr)
        path = out_dir / f"replay-sweep-{stamp}.json"
        path.write_text(json.dumps(sweep, indent=2))
        print(path)
        return 0

    p = rp.ReplayParams.from_settings(settings, entry_delay_s=args.delay, slippage_bps=args.slippage_bps,
                                      notional=args.notional)
    result = rp.run_replay(events, source, p)
    rep = rp.report(result, capital=capital)
    rep["events_loaded"] = len(events)
    if args.meta:
        from app.research import meta_label

        rep["meta_label"] = meta_label.run(result.trades)
        m = rep["meta_label"]
        if m.get("ok"):
            print(f"meta-label: test AUC {m['test_auc_meta']} vs LLM confidence "
                  f"{m['test_auc_llm_confidence']}; kept E[R] {m['kept_by_meta_model']['expectancy_r']} "
                  f"vs all {m['all_test_trades']['expectancy_r']}", file=sys.stderr)
        else:
            print(f"meta-label: {m.get('reason')}", file=sys.stderr)
    json_path = out_dir / f"replay-{stamp}.json"
    json_path.write_text(json.dumps(rep, indent=2, default=str))
    csv_path = out_dir / f"replay-{stamp}.csv"
    rows = [asdict(t) for t in result.trades]
    with csv_path.open("w", newline="") as fh:
        if rows:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    a = rep["all_directional"]
    print(f"simulated {a['n']} trades, skipped {sum(result.skipped.values())} {result.skipped}", file=sys.stderr)
    print(f"expectancy {a['expectancy_r']} R  95% CI {a['expectancy_r_ci95']}  "
          f"win rate {a['win_rate']}  net ₹{a['net_pnl']:,}", file=sys.stderr)
    print(json_path)
    print(csv_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
