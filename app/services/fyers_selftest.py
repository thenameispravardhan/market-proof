"""Pre-market Fyers self-test — catches a silent no-trade day before the open.

Since 1 April 2026 (SEBI retail-algo framework, enforced by every broker)
Fyers has three properties that turn an unattended morning into a lost
trading day without any error the dashboard would show:

  1. The access token needs a DAILY login with 2FA and there is no
     refresh-token session any more. A forgotten login means every entry
     blocks NO_LIVE_PRICE while REST, the LLM and the rules all look fine.
  2. Orders are accepted only from an app of type 200 ("order placement")
     mapped to a whitelisted static IP. An older type-100 app is data-only:
     quotes keep working, every order is refused with code -50.
  3. The whitelisted IP is checked on every order, so a server whose egress
     address changed (new Elastic IP, IPv6 preference, a VPN) is refused
     the same way.

Each check below turns one of those into a named, actionable line before
09:15 instead of a rejected order at 09:31. The 2FA login itself is NOT
automated — scripting it would defeat the point of the rule — so the job
here is only to notice and shout.

`run_fyers_selftest()` is pure apart from its injected probes, so tests
drive it without the network. The latest result is kept in-process and
served by GET /api/system/fyers-selftest for the dashboard banner.
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dt_time, timezone
from typing import Any, Awaitable, Callable, Optional

from app.config import get_settings
from app.logging_config import get_logger

log = get_logger(__name__)

# Severity of a failed check. "error" = orders or data will fail today;
# "warn" = something an operator should look at, but trading can proceed.
ERROR = "error"
WARN = "warn"
OK = "ok"
SKIP = "skip"

DEFAULT_IP_ECHO_URL = "https://api.ipify.org"


@dataclass
class Check:
    name: str
    status: str            # ok | error | warn | skip
    detail: str

    @property
    def failed(self) -> bool:
        return self.status in (ERROR, WARN)


@dataclass
class SelfTestResult:
    ran_at: str
    checks: list[Check] = field(default_factory=list)

    @property
    def problems(self) -> list[str]:
        return [f"{c.name}: {c.detail}" for c in self.checks if c.failed]

    @property
    def ok(self) -> bool:
        return not any(c.status == ERROR for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ran_at": self.ran_at,
            "ok": self.ok,
            "problems": self.problems,
            "checks": [asdict(c) for c in self.checks],
        }


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def app_type_of(app_id: Optional[str]) -> Optional[int]:
    """The numeric app type Fyers encodes as the app-id suffix
    (`XC12345-100` → 100, `XC12345-200` → 200). None when absent."""
    if not app_id or "-" not in app_id:
        return None
    suffix = app_id.rsplit("-", 1)[1].strip()
    return int(suffix) if suffix.isdigit() else None


def token_claims(access_token: Optional[str]) -> dict[str, Any]:
    """Unverified JWT payload of a Fyers access token ({} if unreadable).
    Only claims are read; the signature is Fyers' business, not ours."""
    if not access_token or access_token.count(".") != 2:
        return {}
    payload = access_token.split(".")[1]
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception:  # noqa: BLE001 — unreadable means unknown
        return {}
    return data if isinstance(data, dict) else {}


def parse_ip_list(raw: Optional[str]) -> list[str]:
    return [p.strip() for p in str(raw or "").replace(";", ",").split(",") if p.strip()]


def _market_close_epoch(now_epoch: float) -> float:
    """Epoch seconds of today's IST market close (15:30 by default)."""
    from app.risk.market_clock import IST

    raw = str(getattr(get_settings(), "MARKET_CLOSE_IST", "15:30") or "15:30")
    try:
        hh, mm = (int(x) for x in raw.split(":"))
    except ValueError:
        hh, mm = 15, 30
    today = datetime.fromtimestamp(now_epoch, tz=IST).date()
    return datetime.combine(today, dt_time(hh, mm), tzinfo=IST).timestamp()


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def check_app_type(app_id: str, required: str, *, live: bool) -> Check:
    if not app_id:
        return Check("app_id", ERROR, "FYERS_APP_ID is blank in .env — no Fyers app configured.")
    if not str(required).strip():
        return Check("app_type", SKIP, "FYERS_REQUIRED_APP_TYPE is blank; check disabled.")
    have = app_type_of(app_id)
    want = int(str(required).strip())
    if have == want:
        return Check("app_type", OK, f"app {app_id} is type {have}.")
    detail = (
        f"app {app_id} is type {have if have is not None else 'unknown'}, "
        f"order placement needs type {want}. Create a type-{want} app on "
        "myapi.fyers.in, map the server's static IP, and update FYERS_APP_ID."
    )
    # A data-only app is fatal for live orders but harmless in paper mode.
    return Check("app_type", ERROR if live else WARN, detail)


def check_token(
    account_token: Optional[str],
    *,
    account_app_id: Optional[str],
    env_app_id: str,
    now_epoch: float,
) -> list[Check]:
    out: list[Check] = []
    if not account_token:
        out.append(Check(
            "token", ERROR,
            "no Fyers access token stored — log in (Accounts → Connect Fyers) "
            "before the open or every entry blocks NO_LIVE_PRICE.",
        ))
        return out
    exp = token_claims(account_token).get("exp")
    try:
        exp_f = float(exp) if exp is not None else None
    except (TypeError, ValueError):
        exp_f = None
    if exp_f is None:
        out.append(Check("token", WARN, "token expiry unreadable; relying on the live probe."))
    elif exp_f <= now_epoch:
        out.append(Check(
            "token", ERROR,
            "the Fyers token has EXPIRED (daily 2FA login required, no refresh "
            "session since the April 2026 rules) — log in now.",
        ))
    elif exp_f < _market_close_epoch(now_epoch):
        left_min = int((exp_f - now_epoch) // 60)
        out.append(Check(
            "token", WARN,
            f"token expires before today's close (in {left_min} min) — re-login "
            "before then or positions lose their exit feed.",
        ))
    else:
        out.append(Check("token", OK, "token present and valid through the close."))
    # The token is minted for ONE app. `.env` is the source of truth for
    # which app places orders (manager._effective_app_id), so a token
    # minted under a different app id authenticates as nothing.
    if account_app_id and env_app_id and account_app_id.strip() != env_app_id.strip():
        out.append(Check(
            "token_app", ERROR,
            f"stored token was issued for app {account_app_id} but .env says "
            f"{env_app_id} — re-run Connect Fyers so the token matches the app.",
        ))
    return out


def check_egress_ip(egress_ip: Optional[str], whitelist: list[str]) -> Check:
    if not whitelist:
        return Check(
            "egress_ip", SKIP,
            f"FYERS_WHITELISTED_IPS not set (server egress is {egress_ip or 'unknown'}).",
        )
    if not egress_ip:
        return Check("egress_ip", WARN, "could not determine the server's public IPv4.")
    if egress_ip in whitelist:
        return Check("egress_ip", OK, f"egress {egress_ip} is whitelisted.")
    return Check(
        "egress_ip", ERROR,
        f"server egress IP {egress_ip} is not in the Fyers whitelist "
        f"({', '.join(whitelist)}) — every order will be refused with code -50.",
    )


# ---------------------------------------------------------------------------
# Default probes (network)
# ---------------------------------------------------------------------------


async def default_egress_ip(url: str = DEFAULT_IP_ECHO_URL) -> Optional[str]:
    """The server's public IPv4, measured the same way orders leave the box
    (IPv4-forced, like FyersClient), so the answer is the address Fyers sees."""
    import httpx

    try:
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0"), timeout=5.0
        ) as client:
            resp = await client.get(url)
            text = resp.text.strip()
    except Exception as e:  # noqa: BLE001
        log.warning("fyers_selftest.egress_probe_failed", error=str(e))
        return None
    return text if text and len(text) <= 45 and " " not in text else None


def default_account_lookup() -> tuple[Optional[str], Optional[str]]:
    """(access_token, app_id) of the first live Fyers account, or (None, None)."""
    from sqlalchemy import select

    from app.db.models import BrokerAccount
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        acc = db.execute(
            select(BrokerAccount).where(
                BrokerAccount.broker == "fyers",
                BrokerAccount.paper_mode == False,  # noqa: E712
            ).order_by(BrokerAccount.id.asc())
        ).scalars().first()
        if acc is None:
            return None, None
        return acc.access_token, acc.app_id


async def default_profile_probe() -> tuple[bool, str]:
    """Hit /profile — the cheapest authenticated endpoint. (ok, detail)."""
    from app.api.market import _fyers_backend

    backend = _fyers_backend()
    if backend is None or not hasattr(backend, "get_profile"):
        return False, "no live Fyers account with a token is configured."
    try:
        prof = await backend.get_profile()
    except Exception as e:  # noqa: BLE001
        return False, f"authenticated call failed: {str(e)[:160]}"
    who = prof.get("fy_id") or prof.get("display_name") or "account"
    return True, f"authenticated as {who}."


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

_last_result: Optional[SelfTestResult] = None


def last_result() -> Optional[dict[str, Any]]:
    return _last_result.to_dict() if _last_result is not None else None


async def run_fyers_selftest(
    *,
    account_lookup: Callable[[], tuple[Optional[str], Optional[str]]] = default_account_lookup,
    profile_probe: Callable[[], Awaitable[tuple[bool, str]]] = default_profile_probe,
    egress_probe: Callable[[], Awaitable[Optional[str]]] | None = None,
    now_epoch: Optional[float] = None,
) -> SelfTestResult:
    """Run every check. Never raises: a broken probe becomes a failed check,
    which is the safe direction for an alarm."""
    global _last_result
    s = get_settings()
    now_epoch = time.time() if now_epoch is None else now_epoch
    env_app_id = str(getattr(s, "FYERS_APP_ID", "") or "").strip()
    live = str(getattr(s, "TRADING_MODE", "paper")) == "live"
    result = SelfTestResult(
        ran_at=datetime.fromtimestamp(now_epoch, tz=timezone.utc).isoformat()
    )

    result.checks.append(check_app_type(
        env_app_id, str(getattr(s, "FYERS_REQUIRED_APP_TYPE", "200") or ""), live=live
    ))

    try:
        token, acc_app_id = account_lookup()
    except Exception as e:  # noqa: BLE001
        token, acc_app_id = None, None
        result.checks.append(Check("account", WARN, f"account lookup failed: {e}"))
    result.checks.extend(check_token(
        token, account_app_id=acc_app_id, env_app_id=env_app_id, now_epoch=now_epoch
    ))

    if token:
        try:
            ok, detail = await profile_probe()
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"probe crashed: {e}"
        result.checks.append(Check("auth_probe", OK if ok else ERROR, detail))

    whitelist = parse_ip_list(getattr(s, "FYERS_WHITELISTED_IPS", ""))
    if egress_probe is None:
        url = str(getattr(s, "FYERS_EGRESS_IP_URL", "") or DEFAULT_IP_ECHO_URL)

        async def egress_probe() -> Optional[str]:
            return await default_egress_ip(url)
    try:
        egress = await egress_probe()
    except Exception:  # noqa: BLE001
        egress = None
    result.checks.append(check_egress_ip(egress, whitelist))

    _last_result = result
    log.info("fyers_selftest.done", ok=result.ok, problems=result.problems)
    return result
