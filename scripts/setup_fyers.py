"""One-shot automation: Fyers webhook + OAuth, end-to-end.

What this does (in order):

  1. **Pre-flight**
     - Checks that the bot is reachable on the configured port
       (default 127.0.0.1:8000). If not, prints the right
       command to start it and exits.
     - Checks the Fyers creds in .env (FYERS_APP_ID +
       FYERS_SECRET_KEY). If missing, prints what to fill in and
       exits.

  2. **Tunnel**
     - Auto-detects `cloudflared` (preferred) or `ngrok` on PATH.
     - Spawns it as a subprocess pointing at the bot's local port.
     - Captures the public HTTPS URL from its startup output.
     - Tears it down cleanly on Ctrl-C / exit.

  3. **Redirect URI auto-registration**
     - Patches the bot's `/api/settings` so
       `FYERS_REDIRECT_URI` = `<public>/api/fyers/callback` for
       this run. (The dashboard needs to be configured to the same
       URL; this keeps the two in sync.)

  4. **OAuth**
     - Asks the bot for a fresh authorize URL (it registers the
       CSRF `state` server-side).
     - Opens it in the operator's default browser.
     - Polls `/api/fyers/status` until the connection flips to
       `connected`, with a sane default timeout (3 minutes).

  5. **Webhook registration**
     - Generates / refreshes `FYERS_POSTBACK_SECRET`.
     - Upserts the `fyers-postback-orders` webhook row.
     - Re-points the URL at the public tunnel.
     - Probes the receiver with a synthetic Fyers payload to
       confirm the round-trip works.

  6. **Summary**
     - Prints the dashboard URL to paste into Fyers.
     - Prints the Fyers postback URL.
     - Prints the next steps (place a test order, watch logs).

Usage:

    python scripts/setup_fyers.py

    # Skip the OAuth step (e.g. you already authorised today and
    # just want to refresh the tunnel + webhook registration):
    python scripts/setup_fyers.py --skip-oauth

    # Use a different local port (must match the bot's BIND_PORT):
    python scripts/setup_fyers.py --port 9000

    # Force a fresh secret:
    python scripts/setup_fyers.py --rotate-secret

    # Status-only check (no tunnel, no browser):
    python scripts/setup_fyers.py status

    # Tear down the running tunnel (looks up PID in .tunnel.pid):
    python scripts/setup_fyers.py teardown
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("TESTING", "0")

# Local imports (after sys.path tweak) -------------------------------
from app.config import get_settings  # noqa: E402
from app.logging_config import configure_logging, get_logger  # noqa: E402

from scripts._tunnel import (  # noqa: E402
    Tunnel,
    TunnelError,
    detect_backend,
    install_hint,
    start_tunnel,
    track_for_atexit,
)
from scripts._oauth import OAuthError, run_oauth_flow  # noqa: E402
from scripts.register_fyers_webhook import (  # noqa: E402
    FYERS_WEBHOOK_NAME,
    _ensure_secret,
    _get_or_create_webhook,
    _probe_receiver,
)


log = get_logger(__name__)


TUNNEL_PID_FILE = PROJECT_ROOT / ".tunnel.pid"
TUNNEL_INFO_FILE = PROJECT_ROOT / ".tunnel.json"


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------


def _check_bot_reachable(host: str, port: int, timeout: float = 1.5) -> tuple[bool, Optional[dict]]:
    """Quick TCP + /health probe. Returns (reachable, health_json_or_None)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError:
        return False, None
    try:
        import urllib.request

        with urllib.request.urlopen(
            f"http://{host}:{port}/health", timeout=timeout
        ) as r:
            return True, json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return True, None  # port is open even if /health is broken


def _require_fyers_creds() -> tuple[str, str]:
    settings = get_settings()
    app_id = (settings.FYERS_APP_ID or "").strip()
    secret = (settings.FYERS_SECRET_KEY or "").strip()
    if not app_id or not secret:
        print("=" * 70)
        print("Fyers credentials missing in .env")
        print("=" * 70)
        print()
        print("Open your .env file and set:")
        print()
        print("  FYERS_APP_ID=YOUR_APP_ID_HERE")
        print("  FYERS_SECRET_KEY=YOUR_SECRET_KEY_HERE")
        print()
        print("Get them from: https://myapi.fyers.in/dashboard/")
        print("  -> App -> App ID / Secret Key")
        print()
        sys.exit(2)
    return app_id, secret


# ---------------------------------------------------------------------------
# State files
# ---------------------------------------------------------------------------


def _save_tunnel_info(t: Tunnel) -> None:
    info = {
        "backend": t.backend,
        "public_url": t.public_url,
        "local_port": t.local_port,
        "pid": t.proc.pid if t.proc else None,
        "started_at": int(time.time()),
    }
    TUNNEL_INFO_FILE.write_text(json.dumps(info, indent=2), encoding="utf-8")
    if t.proc:
        TUNNEL_PID_FILE.write_text(str(t.proc.pid), encoding="utf-8")


def _read_tunnel_info() -> Optional[dict]:
    if not TUNNEL_INFO_FILE.is_file():
        return None
    try:
        return json.loads(TUNNEL_INFO_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Redirect-URI sync
# ---------------------------------------------------------------------------


def _patch_redirect_uri(bot_public_url: str) -> None:
    """Tell the running bot to use the public URL for FYERS_REDIRECT_URI
    so the dashboard config matches. The bot's settings API applies
    this through `apply_overrides_to_env` on the next lifespan cycle —
    for the in-process FastAPI app we patch the env directly via the
    settings API."""
    desired = f"{bot_public_url.rstrip('/')}/api/fyers/callback"
    settings = get_settings()
    if (settings.FYERS_REDIRECT_URI or "").rstrip("/") == desired.rstrip("/"):
        return
    # Best-effort: PUT /api/settings with the section override.
    # The settings API in this project supports per-section keys; we
    # don't know its exact contract, so fall back to a hint if it
    # doesn't work. The critical thing is that Fyers is configured
    # to redirect to the same URL we're now exposing.
    try:
        import urllib.request

        body = json.dumps(
            {
                "global": {},
                "sections": {"fyers": {"FYERS_REDIRECT_URI": desired}},
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"http://{settings.BIND_HOST}:{settings.BIND_PORT}/api/settings",
            data=body,
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        urllib.request.urlopen(req, timeout=3.0).read()
        log.info("setup.redirect_uri_patched", redirect_uri=desired)
    except Exception as e:  # noqa: BLE001
        log.warning(
            "setup.redirect_uri_patch_failed",
            desired=desired,
            error=str(e),
            hint=(
                "Manually set FYERS_REDIRECT_URI in .env to the value "
                "above and restart the bot if Fyers rejects the redirect."
            ),
        )


# ---------------------------------------------------------------------------
# Main: setup
# ---------------------------------------------------------------------------


@contextmanager
def _tunnel_context(local_port: int, backend: Optional[str], timeout_s: float) -> Iterator[Tunnel]:
    print(f"==> Starting tunnel (port {local_port})...")
    t = start_tunnel(local_port=local_port, backend=backend, timeout_s=timeout_s)
    _save_tunnel_info(t)
    track_for_atexit(t)
    print(f"    backend: {t.backend}")
    print(f"    public : {t.public_url}")
    print(f"    local  : http://127.0.0.1:{t.local_port}")
    try:
        yield t
    finally:
        print("==> Stopping tunnel...")
        t.stop()
        for p in (TUNNEL_PID_FILE, TUNNEL_INFO_FILE):
            try:
                p.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass


def cmd_setup(args: argparse.Namespace) -> int:
    settings = get_settings()
    host = settings.BIND_HOST
    port = settings.BIND_PORT

    print("=" * 70)
    print("Fyers webhook + OAuth setup")
    print("=" * 70)
    print()

    # ---- pre-flight ----
    print("==> Pre-flight")
    reachable, health = _check_bot_reachable(host, port)
    if not reachable:
        print(f"    The bot is not reachable on http://{host}:{port}.")
        print("    Start it in another terminal first:")
        print()
        print("        .venv\\Scripts\\activate")
        print("        uvicorn app.main:app --host 127.0.0.1 --port 8000")
        print()
        return 1
    if health is not None:
        print(
            f"    bot health: ok (trading_mode={health.get('trading_mode')}, "
            f"fyers_configured={health.get('fyers_configured')})"
        )
    else:
        print("    bot is reachable on the port; /health returned no JSON.")

    app_id, _ = _require_fyers_creds()
    print(f"    FYERS_APP_ID = {app_id[:6]}...{app_id[-4:] if len(app_id) > 10 else ''}")
    print()

    if not detect_backend():
        print("==> No tunnel binary on PATH.")
        print(install_hint())
        return 3

    # ---- tunnel ----
    bot_local = f"http://{host}:{port}"
    with _tunnel_context(
        local_port=port,
        backend=args.backend,
        timeout_s=args.tunnel_timeout,
    ) as tunnel:
        bot_public = tunnel.public_url
        # NOTE: OAuth talks to the bot over LOCALHOST and uses the
        # redirect URI already in .env (normally
        # http://localhost:8000/api/fyers/callback — the value
        # registered on the Fyers app for a single-machine setup).
        # We deliberately do NOT repoint the redirect at the tunnel:
        #   * the operator's own machine often can't resolve a
        #     just-created trycloudflare hostname yet (getaddrinfo
        #     fails), and
        #   * Fyers rejects any redirect that isn't the one registered
        #     on the app.
        # The tunnel is used ONLY for the inbound webhook (Fyers -> bot)
        # set up below.
        # ---- oauth ----
        if args.skip_oauth:
            print("==> OAuth step SKIPPED (--skip-oauth).")
        else:
            print("==> Starting Fyers OAuth (login round-trips over localhost)...")
            try:
                outcome = run_oauth_flow(
                    bot_public_url=bot_local,
                    timeout_s=args.oauth_timeout,
                    open_browser=not args.no_browser,
                )
            except OAuthError as e:
                print(f"    OAuth FAILED: {e}")
                print("    Tip: log in via the bot's Accounts -> Connect Fyers button,")
                print("    then re-run with --skip-oauth for just the tunnel + webhook.")
                return 4
            print("    OAuth complete.")
            print(f"      account_id : {outcome.account_id}")
            print(f"      app_id     : {outcome.app_id}")
        # ---- webhook registration ----
        print()
        print("==> Registering Fyers postback webhook...")
        secret = _ensure_secret(rotate=args.rotate_secret)
        wh = _get_or_create_webhook(secret)
        # The RECONCILING endpoint is /api/fyers/postback?token=<secret>
        # (it updates the matching trade's status / fill price / position).
        # The generic /api/webhooks/in/<id> path only journals the event
        # as a HOLD analysis, so we point the operator at the postback
        # URL. We also persist PUBLIC_BASE_URL so the Accounts "Fyers
        # Order Postback" card shows the same live URL + token.
        postback_url = f"{bot_public}/api/fyers/postback?token={secret}"
        with _db_session() as s:
            from app.db.infra_models import AppSetting
            import json as _json

            row = s.get(AppSetting, "PUBLIC_BASE_URL")
            if row is None:
                s.add(AppSetting(key="PUBLIC_BASE_URL", value=_json.dumps(bot_public)))
            else:
                row.value = _json.dumps(bot_public)
            wh = s.get(_WebhookModel, wh.id)
            wh.url = postback_url
            s.commit()
            wid = wh.id

        ok, summary = _probe_receiver(wid)
        print(f"    receiver probe: {'OK' if ok else 'FAILED'}: {summary}")
        if not ok:
            return 5
        # ---- summary ----
        print()
        print("=" * 70)
        print("SETUP COMPLETE")
        print("=" * 70)
        print()
        print("Paste this URL in the Fyers API dashboard -> App -> Webhooks/Postbacks:")
        print(f"    {postback_url}")
        print()
        print("Enable these triggers in the Fyers dashboard:")
        print("    [x] Order Placed / Pending")
        print("    [x] Order Filled / Traded")
        print("    [x] Order Rejected")
        print("    [x] Order Cancelled")
        print()
        # The tunnel only lives as long as THIS process. Block here so it
        # stays up until the operator stops it — without this the `with`
        # block would exit immediately, tear the tunnel down, and leave a
        # dead PUBLIC_BASE_URL behind (the "webhook URL is dead" bug).
        print("Leave this terminal OPEN — the tunnel dies when this process")
        print("exits, and Fyers can then no longer reach the webhook.")
        print()
        print("Waiting for order postbacks... press Ctrl-C to stop the tunnel.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n==> Ctrl-C received — tearing down the tunnel.")
    return 0


# ---------------------------------------------------------------------------
# Status / teardown
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:  # noqa: ARG001
    _ensure_models_loaded()
    settings = get_settings()
    print("Fyers setup status")
    print("-" * 40)
    info = _read_tunnel_info()
    if info:
        print(f"  tunnel backend  : {info.get('backend')}")
        print(f"  tunnel public   : {info.get('public_url')}")
        print(f"  tunnel local    : 127.0.0.1:{info.get('local_port')}")
        # Check the process is actually alive (best-effort)
        pid = info.get("pid")
        alive = False
        if pid:
            try:
                if sys.platform == "win32":
                    import ctypes

                    PROCESS_QUERY_LIMITED = 0x1000
                    kernel32 = ctypes.windll.kernel32
                    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED, False, int(pid))
                    if handle:
                        alive = True
                        kernel32.CloseHandle(handle)
                else:
                    alive = os.path.exists(f"/proc/{pid}")
            except Exception:  # noqa: BLE001
                alive = None
        if pid:
            tag = (
                "alive"
                if alive is True
                else ("DEAD — leftover file" if alive is False else "unknown")
            )
            print(f"  tunnel pid      : {pid} ({tag})")
    else:
        print("  tunnel          : not running")
    reachable, health = _check_bot_reachable(settings.BIND_HOST, settings.BIND_PORT)
    print(f"  bot reachable   : {reachable}")
    if health:
        print(f"  fyers_configured: {health.get('fyers_configured')}")
    # Webhook row
    with _db_session() as s:
        wh = s.query(_WebhookModel).filter_by(name=FYERS_WEBHOOK_NAME).one_or_none()
    if wh:
        print(f"  webhook row id  : {wh.id}")
        print(f"  webhook enabled : {wh.enabled}")
    else:
        print("  webhook row     : not registered")
    return 0


def cmd_teardown(args: argparse.Namespace) -> int:  # noqa: ARG001
    info = _read_tunnel_info()
    if not info:
        print("No tunnel info file (.tunnel.json) — nothing to tear down.")
        return 0
    pid = info.get("pid")
    if pid:
        if sys.platform == "win32":
            import subprocess

            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                check=False,
            )
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError as e:
                print(f"permission denied killing pid {pid}: {e}")
                return 1
    for p in (TUNNEL_PID_FILE, TUNNEL_INFO_FILE):
        try:
            p.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
    print("Tunnel teardown complete.")
    return 0


# ---------------------------------------------------------------------------
# Lazy import (after env is set) helpers
# ---------------------------------------------------------------------------


def _db_session():
    from app.db import session as db_session_mod

    return db_session_mod.SessionLocal()


_WebhookModel: Any = None  # filled in on first use


def _ensure_models_loaded() -> None:
    global _WebhookModel
    if _WebhookModel is not None:
        return
    from app.db.models import Webhook

    _WebhookModel = Webhook


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Fyers webhook + OAuth automation")
    sub = p.add_subparsers(dest="cmd", required=False)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--port", type=int, default=0, help="override BIND_PORT")
    common.add_argument("--tunnel-timeout", type=float, default=25.0)
    common.add_argument(
        "--backend", default="", help="force cloudflared|ngrok (default: auto-detect)"
    )

    p_setup = sub.add_parser("setup", parents=[common], help="(default) run the full setup")
    p_setup.add_argument("--skip-oauth", action="store_true")
    p_setup.add_argument("--no-browser", action="store_true",
                         help="don't open the browser automatically")
    p_setup.add_argument("--oauth-timeout", type=float, default=180.0)
    p_setup.add_argument("--rotate-secret", action="store_true")

    p_status = sub.add_parser("status", parents=[common], help="show what's currently running")
    p_teardown = sub.add_parser("teardown", parents=[common],
                                help="kill the running tunnel (if any)")

    if argv is None:
        argv = sys.argv[1:]
    # When invoked with no subcommand, default to `setup` and re-parse
    # under that subparser so all its arguments are attached to `args`.
    if not argv or argv[0].startswith("-"):
        argv = ["setup", *argv]
    args = p.parse_args(argv)

    configure_logging("INFO")
    _ensure_models_loaded()

    # Apply --port override on top of settings (BIND_PORT is normally
    # read from .env, but the operator may have started the bot on
    # a different port).
    if getattr(args, "port", 0):
        get_settings.cache_clear()
        os.environ["BIND_PORT"] = str(args.port)

    cmd = args.cmd or "setup"
    if cmd == "setup":
        return cmd_setup(args)
    if cmd == "status":
        return cmd_status(args)
    if cmd == "teardown":
        return cmd_teardown(args)
    p.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
