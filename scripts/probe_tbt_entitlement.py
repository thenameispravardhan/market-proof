"""Answer the tbt_ws question — c++.text §7.6.

"ACTION BEFORE PHASE 13 — do this in week one, it costs one day... One day of
checking can save three months of work and the largest risk in the project."

The OFFLINE half is already done and answered YES:
scripts/recover_tbt_proto.py recovers the schema from the shipped descriptor,
`protoc --cpp_out` generates C++ bindings from it, and those bindings compile
and round-trip a realistic tick. So IF the feed is usable, Phase 13 really is
codegen rather than reverse-engineering.

This script is the ONLINE half, and it needs a live Fyers session:

    1. Is the account entitled to the tbt / versova feed at all?
       (It may be a paid or derivatives-only add-on.)
    2. Does it carry LTP / bid / ask for NSE CASH EQUITIES, not just depth
       or tick-by-tick for derivatives?
    3. How does its latency compare with the HSM data feed?

    .venv/bin/python scripts/probe_tbt_entitlement.py \
        --symbols NSE:RELIANCE-EQ,NSE:SBIN-EQ --seconds 120

Run it DURING market hours — outside them a silent socket proves nothing, and
this script says so rather than reporting a false negative.

It only subscribes. It places no order and writes to no table.
"""
from __future__ import annotations

import argparse
import collections
import sys
import threading
import time
from datetime import datetime, timezone


def load_ws_token() -> str:
    """The `<app_id>:<access_token>` string the Fyers WebSocket SDK expects.

    The token lives on the `broker_accounts` row the OAuth callback writes --
    NOT in .env, which only holds FYERS_APP_ID / FYERS_SECRET_KEY. Reading
    settings.FYERS_ACCESS_TOKEN reports "no token" even when the operator is
    perfectly well logged in; that false negative is what this prevents.

    Returns "" when there is no usable token.
    """
    from sqlalchemy import select

    from app.db.models import BrokerAccount
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        row = (
            db.execute(
                select(BrokerAccount)
                .where(BrokerAccount.broker == "fyers")
                .where(BrokerAccount.access_token.is_not(None))
                .order_by(BrokerAccount.id.asc())
            )
            .scalars()
            .first()
        )
        if row is None or not row.access_token:
            return ""
        token = str(row.access_token)
        app_id = str(row.app_id or "")
    return token if ":" in token else f"{app_id}:{token}"



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="NSE:RELIANCE-EQ,NSE:SBIN-EQ")
    ap.add_argument("--seconds", type=int, default=120)
    args = ap.parse_args()

    from app.risk.market_clock import is_market_open

    access = load_ws_token()
    if not access:
        print("No Fyers access token on any broker_accounts row. "
              "Log in via the dashboard's Fyers panel, then re-run.", file=sys.stderr)
        return 2

    open_now = is_market_open(datetime.now(timezone.utc))
    if not open_now:
        print("WARNING: the market is CLOSED. A silent socket now means nothing —\n"
              "         an entitled feed is also silent outside session hours.\n"
              "         Connection/auth errors are still meaningful; absence of\n"
              "         data is not. Re-run between 09:15 and 15:30 IST.\n")

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    seen: collections.Counter[str] = collections.Counter()
    fields: collections.Counter[str] = collections.Counter()
    per_symbol: dict[str, int] = collections.defaultdict(int)
    first_at: dict[str, float] = {}
    errors: list[str] = []
    lock = threading.Lock()

    def note(msg) -> None:  # noqa: ANN001
        with lock:
            ticker = getattr(msg, "ticker", None) or (
                msg.get("ticker") if isinstance(msg, dict) else None)
            if ticker:
                per_symbol[ticker] += 1
                first_at.setdefault(ticker, time.monotonic())
            # Which of §7.6's Quote fields actually arrive? ltp is the one that
            # decides whether this feed can drive the strategy at all.
            for f in ("ltp", "bid", "ask", "bid_price", "ask_price", "ltq", "vtt", "depth"):
                v = getattr(msg, f, None)
                if v is None and isinstance(msg, dict):
                    v = msg.get(f)
                if v is not None:
                    fields[f] += 1
            seen["messages"] += 1

    try:
        from fyers_apiv3.FyersWebsocket import tbt_ws
    except ImportError as e:
        print(f"tbt_ws not importable: {e}", file=sys.stderr)
        return 2

    started = time.monotonic()
    try:
        sock = tbt_ws.FyersTbtSocket(
            access_token=access,
            write_to_file=False,
            log_path="logs",
            on_error=lambda e: errors.append(str(e)),
            on_open=lambda: sock.subscribe(
                symbol_tickers=set(symbols), channelNo="1",
                mode=tbt_ws.SubscriptionModes.DEPTH),
            on_depth_update=lambda ticker, message: note(message),
            on_close=lambda m: errors.append(f"close: {m}"),
        )
        threading.Thread(target=sock.connect, daemon=True).start()
    except Exception as e:  # noqa: BLE001 — the failure mode IS the answer
        print(f"\nRESULT: could not open the tbt socket.\n  {type(e).__name__}: {e}")
        print("\nThis usually means the account is NOT entitled (§7.6 item 1).")
        return 1

    while time.monotonic() - started < args.seconds:
        time.sleep(5)
        print(f"  {int(time.monotonic()-started):>3}s  messages={seen['messages']} "
              f"symbols={len(per_symbol)} errors={len(errors)}", flush=True)

    try:
        sock.close_connection()
    except Exception:  # noqa: BLE001
        pass

    print("\n" + "=" * 68)
    print("§7.6 ENTITLEMENT PROBE RESULT")
    print("=" * 68)
    print(f"market open during probe : {open_now}")
    print(f"messages received        : {seen['messages']}")
    print(f"symbols with data        : {sorted(per_symbol)}")
    print(f"fields seen              : {dict(fields)}")
    if errors:
        print(f"errors                   : {errors[:5]}")

    # Not every error is a rejection. A clean close (code 200 / s: ok) and a
    # TypeError raised inside this script or the SDK say nothing about
    # entitlement, and concluding "not entitled" from them would send Phase 13
    # down the 16-week path for no reason. Only auth-shaped failures count.
    auth_words = ("invalid", "unauthor", "forbidden", "denied", "token",
                  "not subscribed", "not entitled", "permission", "401", "403")
    rejections = [e for e in errors
                  if any(w in e.lower() for w in auth_words)
                  and "'s': 'ok'" not in e]
    script_bugs = [e for e in errors
                   if "cannot be instantiated" in e or "TypeError" in e]

    print("\nVERDICT")
    if script_bugs:
        print("  INCONCLUSIVE — this probe errored before it could subscribe:")
        for e in script_bugs[:2]:
            print(f"    {e}")
        print("  That is a bug in the probe or the SDK call, NOT a rejection.")
        print("  Fix it and re-run before drawing any conclusion about §7.6.")
    elif rejections:
        print("  NOT entitled, or the subscription was rejected:")
        for e in rejections[:2]:
            print(f"    {e}")
        print("  §9 PHASE 13 stays the 16-week HSM binary decoder path.")
    elif seen["messages"] == 0 and not open_now:
        print("  INCONCLUSIVE — market closed. Re-run during session hours.")
    elif seen["messages"] == 0:
        print("  Connected but silent during an open market: entitled to the socket")
        print("  but probably NOT to NSE cash equities (§7.6 item 2).")
    elif fields.get("ltp"):
        print("  ENTITLED and carrying LTP for NSE cash equities.")
        print("  §9 PHASE 13 becomes 'generate protobuf bindings' — ~8 weeks, not 16.")
        print("  The C++ bindings already generate and round-trip; see cpp/proto/.")
    else:
        print("  Entitled and receiving data, but NO ltp field was observed —")
        print("  likely depth-only. Check §7.6 item 2 before betting Phase 13 on it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
