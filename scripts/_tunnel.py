"""Tunnel manager for the webhook setup automation.

Fyers' API dashboard needs a publicly reachable HTTPS URL to POST
order updates to. Your local bot binds to 127.0.0.1 only (see
`app.config.Settings.bind_public`), so the only practical way to
expose it from a single-operator machine is a tunnel.

This module:
  - Detects whether `cloudflared` or `ngrok` is on PATH.
  - Spawns it as a subprocess, captures the public URL from the
    startup logs, and tracks the process for clean teardown.
  - Is intentionally OS-agnostic. The only requirement is that
    either binary is callable as `cloudflared` or `ngrok`.

For tests the subprocess is replaced by a `FakeTunnel` that just
returns a fixed URL — see `app/tests/test_setup_automation.py`.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Optional


# Regexes for the public URL line in each tool's startup output.
# Cloudflared quick-tunnels: "https://....trycloudflare.com"
# Ngrok v3:                 "https://....ngrok-free.app"
# Ngrok v2:                 "https://....ngrok.io"
_TUNNEL_URL_RE = re.compile(r"https?://[^\s]*?(trycloudflare\.com|ngrok(?:-free)?\.(?:io|app))", re.IGNORECASE)


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


@dataclass
class Tunnel:
    """A running tunnel process. The caller owns one of these and
    is responsible for calling `.stop()` (the `setup` command uses
    a context manager that does this automatically)."""

    backend: str
    public_url: str
    local_port: int
    proc: Optional[subprocess.Popen] = field(default=None, repr=False)
    log_path: Optional[str] = field(default=None, repr=False)

    def stop(self, timeout: float = 5.0) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        try:
            if sys.platform == "win32":
                # CTRL_BREAK is the cleanest way to ask a Windows
                # child to exit. Fall back to terminate if it
                # doesn't honour it.
                try:
                    self.proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    self.proc.terminate()
            else:
                self.proc.terminate()
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=timeout)
        except Exception:  # noqa: BLE001
            pass


class TunnelError(Exception):
    """The tunnel binary failed to start or never produced a URL."""


# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------


def detect_backend() -> Optional[str]:
    """Return the best tunnel backend available, or None.

    Order of preference: cloudflared (no signup, no account, free),
    then ngrok (requires a free account for static domains but works
    without one for the quick tunnels — same as cloudflared).
    """
    if _have("cloudflared"):
        return "cloudflared"
    if _have("ngrok"):
        return "ngrok"
    return None


def install_hint() -> str:
    """OS-appropriate one-liner the user can paste to install a
    tunnel binary."""
    if sys.platform == "win32":
        return (
            "Install one of:\n"
            "  winget install --id Cloudflare.cloudflared     # preferred\n"
            "  choco install cloudflared                      # alt\n"
            "  winget install Ngrok.Ngrok                     # alt\n"
            "Then re-run this command."
        )
    if sys.platform == "darwin":
        return (
            "Install one of:\n"
            "  brew install cloudflared     # preferred\n"
            "  brew install ngrok           # alt\n"
            "Then re-run this command."
        )
    return (
        "Install one of:\n"
        "  sudo apt-get install -y cloudflared      # Debian/Ubuntu\n"
        "  sudo snap install cloudflared            # snap\n"
        "  brew install cloudflared                 # macOS\n"
        "Then re-run this command."
    )


# ---------------------------------------------------------------------------
# Spawning
# ---------------------------------------------------------------------------


def _read_until_url(proc: subprocess.Popen, timeout: float) -> str:
    """Read the subprocess stdout until a public URL is observed
    or the timeout expires. Returns the URL or raises."""
    deadline = time.monotonic() + timeout
    buf: list[str] = []
    assert proc.stdout is not None
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            time.sleep(0.05)
            if proc.poll() is not None:
                # Process died — give up.
                break
            continue
        line_s = line if isinstance(line, str) else line.decode("utf-8", errors="replace")
        buf.append(line_s)
        m = _TUNNEL_URL_RE.search(line_s)
        if m:
            return m.group(0)
    # One last sweep over the buffer in case the URL was split across lines.
    blob = "".join(buf)
    m = _TUNNEL_URL_RE.search(blob)
    if m:
        return m.group(0)
    raise TunnelError(
        f"tunnel backend did not print a public URL within {timeout:.0f}s.\n"
        f"Captured output:\n{blob[-2000:]}"
    )


def start_tunnel(
    *,
    local_port: int,
    backend: Optional[str] = None,
    timeout_s: float = 25.0,
    log_dir: Optional[str] = None,
) -> Tunnel:
    """Start a tunnel pointing at `localhost:local_port`. Returns a
    `Tunnel` whose `.public_url` is the public HTTPS URL Fyers
    should hit."""
    backend = backend or detect_backend()
    if backend is None:
        raise TunnelError(
            "no tunnel binary on PATH (cloudflared/ngrok).\n" + install_hint()
        )
    if backend == "cloudflared":
        return _start_cloudflared(local_port=local_port, timeout_s=timeout_s, log_dir=log_dir)
    if backend == "ngrok":
        return _start_ngrok(local_port=local_port, timeout_s=timeout_s, log_dir=log_dir)
    raise TunnelError(f"unknown tunnel backend: {backend!r}")


def _start_cloudflared(
    *, local_port: int, timeout_s: float, log_dir: Optional[str]
) -> Tunnel:
    log_path = None
    stdout = subprocess.PIPE
    stderr = subprocess.STDOUT
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f"cloudflared-{int(time.time())}.log")
        log_fh = open(log_path, "w", encoding="utf-8")
        stdout = log_fh
        stderr = subprocess.STDOUT
    creationflags = 0
    if sys.platform == "win32":
        # CREATE_NEW_PROCESS_GROUP lets us send CTRL_BREAK cleanly.
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        proc = subprocess.Popen(
            [
                "cloudflared",
                "tunnel",
                "--url",
                f"http://127.0.0.1:{local_port}",
                "--no-autoupdate",
            ],
            stdout=stdout,
            stderr=stderr,
            text=True,
            bufsize=1,
            creationflags=creationflags,
        )
    except FileNotFoundError as e:
        raise TunnelError(f"cloudflared not on PATH: {e}") from e
    try:
        url = _read_until_url(proc, timeout_s)
    except Exception:
        proc.terminate()
        raise
    return Tunnel(
        backend="cloudflared",
        public_url=url.rstrip("/"),
        local_port=local_port,
        proc=proc,
        log_path=log_path,
    )


def _start_ngrok(
    *, local_port: int, timeout_s: float, log_dir: Optional[str]
) -> Tunnel:
    # `ngrok http <port>` — public URL goes to stdout. To avoid
    # ngrok trying to phone home with telemetry, we set a flag.
    env = dict(os.environ)
    env.setdefault("NGROK_SKIP_GO_PLAYGROUND", "1")
    log_path = None
    stdout = subprocess.PIPE
    stderr = subprocess.STDOUT
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f"ngrok-{int(time.time())}.log")
        log_fh = open(log_path, "w", encoding="utf-8")
        stdout = log_fh
        stderr = subprocess.STDOUT
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        proc = subprocess.Popen(
            ["ngrok", "http", str(local_port), "--log=stdout"],
            stdout=stdout,
            stderr=stderr,
            text=True,
            bufsize=1,
            env=env,
            creationflags=creationflags,
        )
    except FileNotFoundError as e:
        raise TunnelError(f"ngrok not on PATH: {e}") from e
    try:
        url = _read_until_url(proc, timeout_s)
    except Exception:
        proc.terminate()
        raise
    return Tunnel(
        backend="ngrok",
        public_url=url.rstrip("/"),
        local_port=local_port,
        proc=proc,
        log_path=log_path,
    )


# A small helper used by the setup script to make sure the tunnel
# is killed if the parent process is killed (best-effort).


def _atexit_kill(t: Tunnel) -> None:
    try:
        t.stop(timeout=1.0)
    except Exception:  # noqa: BLE001
        pass


def track_for_atexit(t: Tunnel) -> None:
    import atexit
    atexit.register(_atexit_kill, t)
