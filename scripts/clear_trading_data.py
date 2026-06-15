"""Clear the local trading database — a clean slate without re-doing setup.

WIPES the operational / trade data:
    trades, positions, signals, analyses, announcements, risk_events,
    audit_log, backtest_runs, prompt_history, notification_log,
    webhook_deliveries

KEEPS your setup / config so you don't have to reconnect or reconfigure:
    broker_accounts (Fyers connection + token), app_settings (settings,
    postback config), prompt_templates, signal_rules, strategies,
    webhooks, notification_channels

Usage:
    python scripts/clear_trading_data.py            # clear trade data, keep setup
    python scripts/clear_trading_data.py --all      # ALSO wipe Fyers account + config

After running, RESTART the backend so its in-memory state (positions,
managed book, caches) resets too.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "data" / "trading.db"

# Child → parent order so foreign-key constraints never trip.
_DATA_TABLES = [
    "trades",
    "positions",
    "risk_events",
    "signals",
    "analyses",
    "announcements",
    "audit_log",
    "backtest_runs",
    "prompt_history",
    "notification_log",
    "webhook_deliveries",
]

# Only cleared with --all.
_CONFIG_TABLES = [
    "broker_accounts",
    "app_settings",
    "prompt_templates",
    "signal_rules",
    "strategies",
    "webhooks",
    "notification_channels",
]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Clear the local trading database")
    p.add_argument(
        "--all",
        action="store_true",
        help="ALSO wipe broker accounts (Fyers), settings and config (full reset)",
    )
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = p.parse_args(argv)

    if not DB_PATH.exists():
        print(f"No database at {DB_PATH} — nothing to clear.")
        return 0

    targets = list(_DATA_TABLES)
    if args.all:
        targets += _CONFIG_TABLES

    if not args.yes:
        scope = "EVERYTHING (incl. Fyers account + config)" if args.all else "trade/operational data (Fyers + settings KEPT)"
        print(f"About to clear {scope} from {DB_PATH}.")
        reply = input("Type 'clear' to proceed: ").strip().lower()
        if reply != "clear":
            print("Aborted.")
            return 1

    # A longer busy timeout so a DELETE doesn't fail if the backend has
    # the file open momentarily.
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.execute("PRAGMA foreign_keys=OFF")
    existing = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    cleared: dict[str, int] = {}
    for table in targets:
        if table not in existing:
            continue
        before = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        conn.execute(f'DELETE FROM "{table}"')
        cleared[table] = before
    conn.commit()
    conn.execute("VACUUM")
    conn.commit()
    conn.close()

    print("=" * 56)
    print("Cleared:")
    for table, n in cleared.items():
        print(f"  {table:24} {n:>8} rows")
    if not args.all:
        print("\nKept (setup/config):", ", ".join(_CONFIG_TABLES))
    print("=" * 56)
    print("Now RESTART the backend so its in-memory state resets too.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
