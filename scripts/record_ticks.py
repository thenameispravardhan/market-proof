"""Record a full trading day of RAW Fyers WebSocket frames.

c++.text §9 PHASE 0: "Record a full trading day of raw Fyers WS frames to a
file. (You cannot capture the past. Without this, Phase 13 is blocked.)"

Phase 13 writes a C++ decoder for the binary data feed and then has to prove it
agrees with the Python SDK on every tick (§9 PHASE 13: "SHADOW the C++ decoder
against the sidecar for 20+ trading days, byte-comparing every decoded tick").
That needs BOTH halves of each frame:

    <out>.raw    the bytes off the wire        -- the decoder's INPUT
    <out>.jsonl  the SDK's decoded message     -- the decoder's EXPECTED OUTPUT

paired by a monotonic sequence number, so a replay can assert "frame N decodes
to exactly this". Recording only the decoded side would be useless: there would
be nothing to feed the decoder.

    # on the server, during market hours
    .venv/bin/python scripts/record_ticks.py --symbols NSE:RELIANCE-EQ,NSE:SBIN-EQ \
        --out data/tickrec/$(date +%F) --until 15:30

Raw frame format (append-only, no index needed -- it is read sequentially):

    magic  "TBRAW1\\n"                     once, at the top
    record  u64 epoch_ns | u32 len | bytes  repeated

READ-ONLY with respect to trading: this opens its OWN socket and never places
an order. It does add a second market-data subscription on the same Fyers
account, which is the one real cost -- run it alongside the live bot only if
the account's connection limit allows, otherwise run it on a day the bot is
paused.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import struct
import sys
import threading
import time
from datetime import datetime, time as dtime, timezone
from pathlib import Path

MAGIC = b"TBRAW1\n"
IST = timezone.utc  # replaced below; IST is UTC+5:30 and has no DST


def _ist_now() -> datetime:
    from app.risk.market_clock import to_ist

    return to_ist(datetime.now(timezone.utc))


class Recorder:
    """Tees every raw WebSocket frame to disk, then lets the SDK handle it."""

    def __init__(self, out: Path) -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        self._raw = open(f"{out}.raw", "wb")
        self._raw.write(MAGIC)
        self._jsonl = open(f"{out}.jsonl", "w", encoding="utf-8")
        self._lock = threading.Lock()
        self.frames = 0
        self.decoded = 0
        self.bytes = 0

    def raw_frame(self, payload: object) -> None:
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, (bytes, bytearray)):
            return
        with self._lock:
            self._raw.write(struct.pack("<QI", time.time_ns(), len(payload)))
            self._raw.write(payload)
            self.frames += 1
            self.bytes += len(payload)

    def decoded_msg(self, msg: object) -> None:
        with self._lock:
            # `seq` pairs this decoded message with the raw frame count at the
            # moment it was produced -- that pairing is the whole artifact.
            self._jsonl.write(
                json.dumps({"seq": self.frames, "ts_ns": time.time_ns(), "msg": msg},
                           default=str, ensure_ascii=False)
                + "\n"
            )
            self.decoded += 1

    def close(self) -> None:
        with self._lock:
            self._raw.flush()
            self._raw.close()
            self._jsonl.flush()
            self._jsonl.close()


def install_raw_tap(rec: Recorder) -> None:
    """Wrap websocket.WebSocketApp so every inbound frame is teed to disk.

    The SDK builds its socket as
        websocket.WebSocketApp(url, on_message=lambda ws, msg: ...)
    (fyers_apiv3/FyersWebsocket/data_ws.py), so wrapping the callback at
    construction catches the bytes BEFORE any SDK decoding, without reaching
    into private methods that a version bump would rename.
    """
    import websocket

    original_init = websocket.WebSocketApp.__init__

    def patched_init(self, url, *a, **kw):  # noqa: ANN001
        user_on_message = kw.get("on_message")

        def tee(ws, message):  # noqa: ANN001
            rec.raw_frame(message)
            if user_on_message is not None:
                return user_on_message(ws, message)
            return None

        if user_on_message is not None:
            kw["on_message"] = tee
        return original_init(self, url, *a, **kw)

    websocket.WebSocketApp.__init__ = patched_init


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", required=True, help="comma-separated Fyers ids")
    ap.add_argument("--out", type=Path, required=True, help="path prefix (.raw/.jsonl appended)")
    ap.add_argument("--until", default="15:30", help="IST HH:MM to stop (default: market close)")
    ap.add_argument("--max-seconds", type=int, default=0, help="stop after N seconds (0 = use --until)")
    args = ap.parse_args()

    from app.config import get_settings

    settings = get_settings()
    token = getattr(settings, "FYERS_ACCESS_TOKEN", "") or ""
    app_id = getattr(settings, "FYERS_APP_ID", "") or ""
    if not token:
        print("No FYERS_ACCESS_TOKEN. The daily token has expired -- log in via the\n"
              "dashboard's Fyers panel first, then re-run. Nothing was recorded.",
              file=sys.stderr)
        return 2

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    rec = Recorder(args.out)
    install_raw_tap(rec)

    stop = threading.Event()
    hh, _, mm = args.until.partition(":")
    stop_at = dtime(int(hh), int(mm))

    def on_message(msg):  # noqa: ANN001
        rec.decoded_msg(msg)

    def on_error(e):  # noqa: ANN001
        print(f"[error] {e}", file=sys.stderr)

    from app.execution.fyers_stream import _default_data_socket_factory

    sock = _default_data_socket_factory(
        access_token=f"{app_id}:{token}" if ":" not in token else token,
        on_message=on_message,
        on_connect=lambda: (sock.subscribe(symbols=symbols, data_type="SymbolUpdate"),
                            print(f"subscribed to {len(symbols)} symbols")),
        on_error=on_error,
        on_close=lambda m: print(f"[close] {m}", file=sys.stderr),
    )

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    started = time.monotonic()
    threading.Thread(target=sock.connect, daemon=True).start()
    print(f"recording -> {args.out}.raw / .jsonl   (until {args.until} IST, Ctrl-C to stop)")

    try:
        while not stop.is_set():
            time.sleep(5)
            if args.max_seconds and (time.monotonic() - started) >= args.max_seconds:
                break
            if not args.max_seconds and _ist_now().time() >= stop_at:
                break
            print(f"  frames={rec.frames} decoded={rec.decoded} bytes={rec.bytes}", flush=True)
    finally:
        rec.close()
        try:
            sock.close_connection()
        except Exception:  # noqa: BLE001 — we are already shutting down
            pass

    print(f"\ndone. frames={rec.frames} decoded={rec.decoded} bytes={rec.bytes}")
    if rec.frames == 0:
        print("NO FRAMES RECORDED — was the market open, and is the token valid?",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
