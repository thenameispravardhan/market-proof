"""Register the Fyers postback webhook in the bot's database.

Run this once (or whenever you rotate the secret). It:

  1. Reads (or generates) a shared secret.
     - If `FYERS_POSTBACK_SECRET` is set in `.env`, uses that.
     - Otherwise generates a 32-byte URL-safe token and APPENDS it
       to `.env` so future runs stay consistent. (Backs up the
       original `.env` to `.env.bak` first.)
  2. Upserts a `Webhook` row named `fyers-postback-orders` with
     `direction='in'`, `enabled=True`, `event_filter='*'`.
  3. Prints the URL the operator must paste in the Fyers API
     dashboard's Postback field, e.g.:

       https://YOUR-PUBLIC-HOST/api/webhooks/in/12

     plus the matching secret for the dashboard's optional
     "shared secret" field (Fyers itself doesn't use a header
     secret — see NOTES below).

  4. Sends a synthetic Fyers-shaped postback through the receiver
     end-to-end so you can confirm the round-trip works BEFORE
     touching the Fyers dashboard.

Usage:

    python scripts/register_fyers_webhook.py                  # use / generate secret
    python scripts/register_fyers_webhook.py --public-url https://bot.example.com
    python scripts/register_fyers_webhook.py --rotate-secret  # force-regenerate

NOTES on Fyers postback:
  Fyers currently does NOT send an HMAC signature header on its
  postback requests. The bot's receiver still allows `secret=None`
  webhooks to accept unsigned payloads, so leaving `secret` blank
  on the Fyers row is the simplest path. If Fyers adds signing
  later, the receiver will pick it up automatically (X-Mavis-Signature
  header is checked whenever `webhook.secret` is non-empty).

  The "secret" we store in the DB is still useful for the bot's
  own audit trail — it identifies the row, and a future Fyers
  upgrade can use it as the shared secret.
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# Set default env so settings picks up FYERS_POSTBACK_SECRET before
# we import app.* modules.
os.environ.setdefault("TESTING", "0")

from app.config import get_settings  # noqa: E402
from app.db import init as db_init  # noqa: E402
from app.db import session as db_session_mod  # noqa: E402
from app.db.models import Webhook  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402


FYERS_WEBHOOK_NAME = "fyers-postback-orders"


def _read_env_file() -> str:
    env_path = PROJECT_ROOT / ".env"
    return env_path.read_text(encoding="utf-8") if env_path.exists() else ""


def _write_env_file(text: str) -> None:
    env_path = PROJECT_ROOT / ".env"
    bak = PROJECT_ROOT / ".env.bak"
    if env_path.exists() and not bak.exists():
        bak.write_text(env_path.read_text(encoding="utf-8"), encoding="utf-8")
    env_path.write_text(text, encoding="utf-8")


def _ensure_secret(rotate: bool) -> str:
    """Return the Fyers postback secret. If `rotate` is True or the
    secret is missing, generate a fresh one and persist it to .env."""
    settings = get_settings()
    secret = (settings.FYERS_POSTBACK_SECRET or "").strip()
    if secret and not rotate:
        return secret
    new_secret = "fy_" + secrets.token_urlsafe(32)
    env_text = _read_env_file()
    line = f"FYERS_POSTBACK_SECRET={new_secret}\n"
    if "FYERS_POSTBACK_SECRET=" in env_text:
        # Replace existing
        import re
        env_text = re.sub(
            r"^FYERS_POSTBACK_SECRET=.*\n",
            line.rstrip("\n") + "\n",
            env_text,
            flags=re.MULTILINE,
        )
    else:
        # Append with a section comment
        if env_text and not env_text.endswith("\n"):
            env_text += "\n"
        env_text += "\n# ---------- FYERS postback (rotated by register_fyers_webhook.py) ----------\n"
        env_text += line
    _write_env_file(env_text)
    # Bust the lru_cache so subsequent reads see the new value
    get_settings.cache_clear()
    return new_secret


def _get_or_create_webhook(secret: str) -> Webhook:
    """Find the Fyers webhook row by name; create it if missing;
    update the secret if it changed."""
    SessionLocal = db_session_mod.SessionLocal
    with SessionLocal() as s:
        existing = s.query(Webhook).filter_by(name=FYERS_WEBHOOK_NAME).one_or_none()
        if existing is None:
            w = Webhook(
                name=FYERS_WEBHOOK_NAME,
                direction="in",
                event_filter="*",
                url="https://api-t1.fyers.in/postback",  # placeholder; Fyers POSTs to us
                secret=None,  # Fyers does not sign postbacks (see module docstring)
                enabled=True,
            )
            s.add(w)
            s.commit()
            s.refresh(w)
            return w
        # Update secret only when the user explicitly rotated
        # (so we don't overwrite a known-good value on re-runs).
        if (existing.secret or "") != (secret or ""):
            existing.secret = secret or None
            s.commit()
            s.refresh(existing)
        return existing


def _probe_receiver(webhook_id: int) -> tuple[bool, str]:
    """Send a synthetic Fyers-shaped payload through the receiver
    directly (no HTTP) to confirm the normalizer + DB insert path
    works. Returns (ok, summary)."""
    import json
    import time
    from app.webhooks.receiver import process_inbound

    fake_postback = {
        "order_id": "PROBE-12345",
        "status": "2",  # Fyers code for FILLED
        "symbol": "NSE:SBIN-EQ",
        "orderType": "1",
        "productType": "INTRADAY",
        "transactionType": "BUY",
        "qty": 1,
        "tradedPrice": 612.5,
        "orderTimestamp": int(time.time()),
    }
    body = json.dumps(fake_postback).encode("utf-8")
    with db_session_mod.SessionLocal() as s:
        wh = s.get(Webhook, webhook_id)
        if wh is None:
            return (False, "webhook row vanished")
        try:
            result = process_inbound(s, wh, body, headers={})
            return (True, f"analysis_id={result['analysis_id']} signal_id={result['signal_id']}")
        except Exception as e:  # noqa: BLE001
            return (False, f"{type(e).__name__}: {e}")


def main() -> int:
    p = argparse.ArgumentParser(description="Register the Fyers postback webhook")
    p.add_argument(
        "--public-url",
        default="",
        help="Public base URL where Fyers can reach this bot (e.g. https://bot.example.com). "
             "If omitted, the script assumes you'll tunnel via cloudflared/ngrok and prints "
             "a placeholder for the dashboard.",
    )
    p.add_argument(
        "--rotate-secret",
        action="store_true",
        help="Force-regenerate FYERS_POSTBACK_SECRET and write it back to .env",
    )
    p.add_argument(
        "--skip-probe",
        action="store_true",
        help="Don't send the synthetic Fyers payload to the receiver",
    )
    args = p.parse_args()

    configure_logging("INFO")
    db_init.init_db()  # idempotent

    secret = _ensure_secret(rotate=args.rotate_secret)
    webhook = _get_or_create_webhook(secret)

    public = args.public_url.strip().rstrip("/") or "https://YOUR-PUBLIC-HOST"
    url = f"{public}/api/webhooks/in/{webhook.id}"

    print("=" * 70)
    print("Fyers postback webhook registered")
    print("=" * 70)
    print(f"  webhook row id : {webhook.id}")
    print(f"  name           : {webhook.name}")
    print(f"  direction      : {webhook.direction}")
    print(f"  enabled        : {webhook.enabled}")
    print(f"  has secret     : {bool((webhook.secret or '').strip())}")
    print()
    print("  Paste this in the Fyers API Dashboard -> App -> Postback URL:")
    print(f"    {url}")
    print()
    print("  Triggers to enable (in the Fyers dashboard):")
    print("    [x] Order Placed")
    print("    [x] Order Filled")
    print("    [x] Order Rejected")
    print("    [x] Order Cancelled")
    print()
    if not args.skip_probe:
        print("  Probing the receiver with a synthetic Fyers payload...")
        ok, summary = _probe_receiver(webhook.id)
        print(f"    -> {'OK' if ok else 'FAILED'}: {summary}")
        if not ok:
            return 1
    else:
        print("  (probe skipped)")
    print()
    print("Next steps:")
    print("  1. Expose this bot to the public internet (cloudflared / ngrok / reverse proxy).")
    print("  2. Paste the URL above into the Fyers dashboard's Postback field.")
    print("  3. Place a small test order; watch the bot log for `webhook_inbound.processed`.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
