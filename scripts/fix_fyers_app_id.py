"""One-shot migration: re-associate the operator's real Fyers
broker_accounts row from the deleted app_id to the new one.

The OAuth callback now does this automatically (it updates the
row's app_id when the configured FYERS_APP_ID differs from the
row's app_id). This script is a one-shot to fix the live DB
right now, without waiting for the operator to re-run OAuth
and the callback to fire.

Run with: .venv\Scripts\python.exe scripts/fix_fyers_app_id.py
"""
import sqlite3
import sys
import os
import shutil

OLD_APP_ID = "XTK927LW7V-100"
NEW_APP_ID = "9TJPAJSKCX-100"

# Pick the live DB.
candidates = [
    "data/trading.db",
    "instance/tradebot.db",
    "data/dev.db",
]
db_path = next((p for p in candidates if os.path.exists(p)), None)
if not db_path:
    print("No live DB found at", candidates, file=sys.stderr)
    sys.exit(1)
print(f"Using DB: {db_path}")

# Backup first.
backup = db_path + ".bak"
shutil.copyfile(db_path, backup)
print(f"Backed up to: {backup}")

conn = sqlite3.connect(db_path)
c = conn.cursor()
c.execute(
    "SELECT id, name, app_id, access_token IS NOT NULL FROM broker_accounts "
    "WHERE broker = 'fyers' AND paper_mode = 0"
)
print("Real Fyers rows BEFORE update:")
for row in c.fetchall():
    print(f"  id={row[0]} name={row[1]!r} app_id={row[2]!r} has_token={row[3]}")

c.execute(
    "UPDATE broker_accounts SET app_id = ? "
    "WHERE broker = 'fyers' AND paper_mode = 0 AND app_id = ?",
    (NEW_APP_ID, OLD_APP_ID),
)
updated = c.rowcount
conn.commit()

c.execute(
    "SELECT id, name, app_id, access_token IS NOT NULL FROM broker_accounts "
    "WHERE broker = 'fyers' AND paper_mode = 0"
)
print(f"Real Fyers rows AFTER update ({updated} row(s) updated):")
for row in c.fetchall():
    print(f"  id={row[0]} name={row[1]!r} app_id={row[2]!r} has_token={row[3]}")
conn.close()

if updated == 0:
    print()
    print(f"No rows had app_id={OLD_APP_ID!r} — nothing to do.")
    print(f"Check `SELECT * FROM broker_accounts WHERE broker='fyers';` in the DB.")
else:
    print()
    print("Done. Now restart the backend so the live FyersLiveBackend")
    print("rebuilds with the new app_id + access_token.")
