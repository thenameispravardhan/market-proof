"""Exchange-side lists and bands that make brokers reject an order.

Each helper here turns a known broker/exchange rejection into a named,
pre-trade refusal with a reason the operator can read, rather than an
order that is sent and bounced:

  * ASM / GSM surveillance lists and trade-to-trade (T2T) series: brokers
    refuse intraday (MIS) orders on these. T2T is visible in the Fyers
    symbol itself (`-BE`, `-BZ` series); ASM/GSM come from NSE's daily
    reports.
  * The F&O ban list (securities past 95% of the market-wide position
    limit): new positions are refused until the stock leaves the list.
  * Circuit limits (the daily price band): a BUY a few ticks under the
    upper circuit fills, if it fills at all, at the worst price of the day.
  * NSE Limit Price Protection (LPP): a limit price too far from the
    contract's reference price is rejected by the exchange. Published
    bands: options ±₹20 when the reference premium is ≤ ₹50, else ±40%;
    futures ±3% (index futures are tighter by some broker accounts, so 2%
    is used for them here, the conservative side).

Lists are fetched at most once per IST day, in the background, and the
gates FAIL OPEN when a list could not be loaded. A gate is something the
operator opts into, and a missing list must not stop all trading. Every
fail-open decision is logged and reported in `status()`.
"""
from __future__ import annotations

import asyncio
import csv
import io
import math
from typing import Any, Iterable, Optional

from app.logging_config import get_logger

log = get_logger(__name__)

FNO_BAN_URL = "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"
ASM_URL = "https://www.nseindia.com/api/reportASM"
GSM_URL = "https://www.nseindia.com/api/reportGSM"
NSE_SEED_URL = "https://www.nseindia.com/"

# Trade-to-trade and other settlement series that brokers refuse for MIS.
T2T_SERIES = frozenset({"BE", "BZ", "BT", "TS"})

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/reports/asm",
}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def bare_symbol(symbol: str) -> str:
    """NSE:RELIANCE-EQ -> RELIANCE, RELIANCE -> RELIANCE."""
    s = (symbol or "").strip().upper()
    s = s.split(":", 1)[-1]
    if "-" in s:
        head, series = s.rsplit("-", 1)
        if series.isalpha() and len(series) <= 5:
            return head
    return s


def series_of(symbol: str) -> Optional[str]:
    s = (symbol or "").strip().upper().split(":", 1)[-1]
    if "-" not in s:
        return None
    return s.rsplit("-", 1)[1]


def is_t2t(symbol: str) -> bool:
    return series_of(symbol) in T2T_SERIES


def parse_fno_ban_csv(text: str) -> set[str]:
    """NSE's fo_secban.csv: a title line, then `<n>,<SYMBOL>` rows."""
    out: set[str] = set()
    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 2 and row[0].strip().isdigit() and row[1].strip():
            out.add(row[1].strip().upper())
    return out


def collect_symbols(payload: Any) -> set[str]:
    """Every value under a `symbol` key anywhere in an NSE report JSON.
    The ASM/GSM report layout has changed shape before (long/short-term
    blocks, stage buckets); walking the tree survives that."""
    out: set[str] = set()
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for k, v in node.items():
                if k.lower() == "symbol" and isinstance(v, str) and v.strip():
                    out.add(v.strip().upper())
                elif isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(node, list):
            stack.extend(node)
    return out


def lpp_band(reference: float, kind: str, *, index_underlying: bool = False) -> tuple[float, float]:
    """(low, high) limit prices NSE's LPP accepts around `reference`.

    `kind` is CE / PE (options) or FUT. Raises ValueError for anything else:
    LPP is a derivatives mechanism and silently returning a band for an
    equity would be a made-up number."""
    ref = float(reference)
    if ref <= 0:
        raise ValueError("reference price must be > 0")
    k = kind.upper()
    if k in ("CE", "PE"):
        width = 20.0 if ref <= 50 else ref * 0.40
    elif k == "FUT":
        width = ref * (0.02 if index_underlying else 0.03)
    else:
        raise ValueError(f"LPP applies to options and futures, not {kind!r}")
    return max(0.05, ref - width), ref + width


def circuit_headroom_pct(side: str, ltp: float, upper: Optional[float], lower: Optional[float]) -> Optional[float]:
    """% distance from `ltp` to the circuit the trade runs INTO: the upper
    circuit for a BUY, the lower for a SELL. None when the band is unknown."""
    if not ltp or ltp <= 0:
        return None
    if side.upper() == "BUY":
        return None if not upper else (float(upper) - ltp) / ltp * 100.0
    return None if not lower else (ltp - float(lower)) / ltp * 100.0


def clamp_to_band(price: float, lower: Optional[float], upper: Optional[float], tick: float = 0.05) -> float:
    """Pull a limit price inside [lower, upper], rounding INWARD to the tick
    so the clamp can never push it back outside the band."""
    p = float(price)
    if upper and p > upper:
        p = math.floor(float(upper) / tick + 1e-9) * tick
    if lower and p < lower:
        p = math.ceil(float(lower) / tick - 1e-9) * tick
    return round(p, 2)


# ---------------------------------------------------------------------------
# Cached lists
# ---------------------------------------------------------------------------


class ExchangeLists:
    def __init__(self) -> None:
        self.asm: set[str] = set()
        self.gsm: set[str] = set()
        self.fno_ban: set[str] = set()
        self.loaded_day: dict[str, str] = {}
        self.errors: dict[str, str] = {}
        self._refreshing: Optional[asyncio.Task[None]] = None
        self._circuits: dict[tuple[str, str], tuple[Optional[float], Optional[float]]] = {}

    # -- state -----------------------------------------------------------

    @staticmethod
    def _today() -> str:
        from app.risk.market_clock import to_ist

        return to_ist(None).date().isoformat()

    def fresh(self, name: str) -> bool:
        return self.loaded_day.get(name) == self._today()

    def status(self) -> dict[str, Any]:
        return {
            "asm": {"count": len(self.asm), "loaded_day": self.loaded_day.get("asm")},
            "gsm": {"count": len(self.gsm), "loaded_day": self.loaded_day.get("gsm")},
            "fno_ban": {"count": len(self.fno_ban), "loaded_day": self.loaded_day.get("fno_ban"),
                        "symbols": sorted(self.fno_ban)},
            "errors": dict(self.errors),
        }

    def set_list(self, name: str, symbols: Iterable[str]) -> None:
        setattr(self, name, {s.upper() for s in symbols})
        self.loaded_day[name] = self._today()
        self.errors.pop(name, None)

    # -- fetching --------------------------------------------------------

    async def refresh(self, *, force: bool = False, timeout: float = 15.0) -> dict[str, Any]:
        """Fetch whatever is stale. Each list fails independently."""
        import httpx

        names = [n for n in ("fno_ban", "asm", "gsm") if force or not self.fresh(n)]
        if not names:
            return self.status()
        async with httpx.AsyncClient(headers=_HEADERS, timeout=timeout, follow_redirects=True) as client:
            if "fno_ban" in names:
                try:
                    r = await client.get(FNO_BAN_URL)
                    r.raise_for_status()
                    self.set_list("fno_ban", parse_fno_ban_csv(r.text))
                except Exception as e:  # noqa: BLE001
                    self.errors["fno_ban"] = str(e)[:200]
            if "asm" in names or "gsm" in names:
                try:
                    await client.get(NSE_SEED_URL)   # NSE's API needs the cookie this sets
                except Exception as e:  # noqa: BLE001
                    log.debug("exchange_lists.seed_failed", error=str(e))
                for name, url in (("asm", ASM_URL), ("gsm", GSM_URL)):
                    if name not in names:
                        continue
                    try:
                        r = await client.get(url)
                        r.raise_for_status()
                        syms = collect_symbols(r.json())
                        if not syms:
                            raise ValueError("report parsed to zero symbols")
                        self.set_list(name, syms)
                    except Exception as e:  # noqa: BLE001
                        self.errors[name] = str(e)[:200]
        if self.errors:
            log.warning("exchange_lists.refresh_errors", errors=self.errors)
        else:
            log.info("exchange_lists.refreshed", asm=len(self.asm), gsm=len(self.gsm),
                     fno_ban=len(self.fno_ban))
        return self.status()

    def refresh_in_background(self) -> None:
        """Kick a refresh without waiting (hot path). No-op if one is running."""
        if self._refreshing is not None and not self._refreshing.done():
            return
        try:
            self._refreshing = asyncio.get_running_loop().create_task(self.refresh())
        except RuntimeError:
            pass

    # -- gates -----------------------------------------------------------

    def surveillance_reason(self, symbol: str, broker_symbol: Optional[str] = None) -> Optional[str]:
        """Why an intraday entry would be refused, or None. Fails open for a
        list that is not loaded (and kicks a refresh)."""
        if is_t2t(broker_symbol or symbol):
            return f"{broker_symbol or symbol} trades in a trade-to-trade series; brokers refuse intraday orders"
        sym = bare_symbol(symbol)
        if not (self.fresh("asm") and self.fresh("gsm")):
            self.refresh_in_background()
        if sym in self.gsm:
            return f"{sym} is on NSE's GSM surveillance list"
        if sym in self.asm:
            return f"{sym} is on NSE's ASM surveillance list"
        return None

    def fno_ban_reason(self, underlying: str) -> Optional[str]:
        if not self.fresh("fno_ban"):
            self.refresh_in_background()
        name = bare_symbol(underlying)
        if name in self.fno_ban:
            return f"{name} is in the F&O ban period — no new positions until it leaves the list"
        return None

    # -- circuit band ----------------------------------------------------

    def cached_band(self, symbol: str) -> tuple[Optional[float], Optional[float]]:
        return self._circuits.get((symbol.upper(), self._today()), (None, None))

    async def circuit_band(self, symbol: str, backend: Any, *, timeout: float = 1.0
                           ) -> tuple[Optional[float], Optional[float]]:
        """(lower, upper) circuit for today, from Fyers depth; cached per day
        (equity bands are fixed for the session). (None, None) if unknown."""
        key = (symbol.upper(), self._today())
        if key in self._circuits:
            return self._circuits[key]
        if backend is None or not hasattr(backend, "get_depth"):
            return (None, None)
        try:
            book = await asyncio.wait_for(backend.get_depth(symbol), timeout=timeout)
        except Exception as e:  # noqa: BLE001
            log.debug("exchange_lists.depth_failed", symbol=symbol, error=str(e))
            return (None, None)
        band = ((book or {}).get("lower_circuit"), (book or {}).get("upper_circuit"))
        if band[0] or band[1]:
            self._circuits[key] = band
        return band


lists = ExchangeLists()
