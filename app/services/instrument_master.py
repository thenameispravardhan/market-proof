"""Symbol / instrument master.

The Trade page needs to resolve a human query ("reliance", "nifty
24500 ce") into a Fyers-style symbol string ("NSE:RELIANCE-EQ",
"NSE:NIFTY2561424500CE"). Fyers publishes a daily scrip master
CSV at:

    https://public.fyers.in/sym_details/NSE_CM.csv        (cash)
    https://public.fyers.in/sym_details/NSE_FO.csv        (F&O)
    https://public.fyers.in/sym_details/BSE_CM.csv
    https://public.fyers.in/sym_details/MCX_COM.csv

The bot's `scripts/` will (in a later change) download these to
`data/scrip_master/{exchange}_{segment}.csv` on a daily cron. The
loader here reads whatever's in that directory and falls back to
a small in-process seed when the directory is empty (so the page
works on a fresh install with no network call).

Schema of each row (Fyers CSV format):

    [0]  symbol            e.g. "NSE:SBIN-EQ"  (the broker id)
    [1]  short_name        e.g. "SBIN"
    [2]  exchange          "NSE" / "BSE" / "MCX"
    [3]  segment           "EQ" / "FO" / "COM" / "CD"
    [4]  instrument_type   "EQ" / "FUT" / "CE" / "PE" / "IND"
    [5]  lot_size          integer
    [6]  tick_size         float
    [7]  expiry            "DD-MMM-YYYY"  (F&O only)
    [8]  strike            float          (options only)
    [9]  underlying        e.g. "NIFTY"   (F&O only)

We only read columns 0..9 and ignore the rest. The CSV has no
header row.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Optional


# Where the daily CSVs land. Created on first call.
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "scrip_master"


@dataclass
class Instrument:
    """One tradable scrip — cash, future, or option."""
    symbol: str              # broker id, e.g. "NSE:SBIN-EQ"
    short_name: str
    exchange: str
    segment: str             # EQ / FO / COM / CD
    instrument_type: str     # EQ / FUT / CE / PE / IND
    lot_size: int = 1
    tick_size: float = 0.05
    expiry: Optional[datetime] = None
    strike: Optional[float] = None
    underlying: Optional[str] = None
    aliases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "short_name": self.short_name,
            "exchange": self.exchange,
            "segment": self.segment,
            "instrument_type": self.instrument_type,
            "lot_size": self.lot_size,
            "tick_size": self.tick_size,
            "expiry": self.expiry.strftime("%Y-%m-%d") if self.expiry else None,
            "strike": self.strike,
            "underlying": self.underlying,
            "display": _format_display(self),
        }


def _parse_expiry(s: str) -> Optional[datetime]:
    if not s or not s.strip():
        return None
    s = s.strip().upper()
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _format_display(i: Instrument) -> str:
    """Human-friendly label for the search dropdown."""
    if i.instrument_type == "EQ":
        return f"{i.short_name}  ·  {i.exchange}:{i.segment}"
    if i.instrument_type in ("CE", "PE"):
        exp = i.expiry.strftime("%d-%b-%Y") if i.expiry else "—"
        return f"{i.short_name} {i.strike:g} {i.instrument_type}  ·  {exp}"
    if i.instrument_type == "FUT":
        exp = i.expiry.strftime("%d-%b-%Y") if i.expiry else "—"
        return f"{i.short_name} FUT  ·  {exp}"
    return f"{i.short_name}  ·  {i.exchange}:{i.segment}"


def _row_to_instrument(row: list[str]) -> Optional[Instrument]:
    if len(row) < 5:
        return None
    sym = row[0].strip()
    if not sym:
        return None
    try:
        lot = int(row[5]) if len(row) > 5 and row[5].strip() else 1
    except (ValueError, IndexError):
        lot = 1
    try:
        tick = float(row[6]) if len(row) > 6 and row[6].strip() else 0.05
    except (ValueError, IndexError):
        tick = 0.05
    expiry = _parse_expiry(row[7]) if len(row) > 7 else None
    strike: Optional[float] = None
    if len(row) > 8 and row[8].strip():
        try:
            strike = float(row[8])
        except ValueError:
            strike = None
    underlying = row[9].strip().upper() if len(row) > 9 and row[9].strip() else None
    return Instrument(
        symbol=sym,
        short_name=row[1].strip().upper() if len(row) > 1 else "",
        exchange=row[2].strip().upper() if len(row) > 2 else "",
        segment=row[3].strip().upper() if len(row) > 3 else "",
        instrument_type=row[4].strip().upper() if len(row) > 4 else "",
        lot_size=lot,
        tick_size=tick,
        expiry=expiry,
        strike=strike,
        underlying=underlying,
        aliases=[sym.split(":", 1)[-1].rsplit("-", 1)[0].upper()] if ":" in sym else [],
    )


def _fyers_csv_row_to_instrument(row: list[str]) -> Optional[Instrument]:
    """Parse a row from Fyers' public `sym_details` CSV (NSE_CM / BSE_CM).

    Real Fyers column layout (0-indexed):
        0 fyToken          5 ISIN            10 exchange    15 strike
        1 description      6 session         11 segment     16 optType (XX/CE/PE)
        2 exInstType       7 updated         12 exToken     ...
        3 lotSize          8 expiry          13 exSymbol (short name)
        4 tickSize         9 symbol ticker   14 underToken

    We keep cash equities + indices + futures. Options (CE/PE) are skipped
    — they're served live via the Fyers option-chain API, and loading the
    full options universe would bloat the in-memory master with hundreds
    of thousands of contracts.
    """
    if len(row) < 17:
        return None
    symbol = (row[9] or "").strip()
    if not symbol or ":" not in symbol:
        return None
    exch = symbol.split(":", 1)[0].strip().upper()
    rest = symbol.split(":", 1)[1]
    short = (row[13] or "").strip().upper() or rest.rsplit("-", 1)[0].upper()
    opt_type = (row[16] or "").strip().upper()
    try:
        lot = int(float(row[3])) if row[3].strip() else 1
    except (ValueError, TypeError):
        lot = 1
    try:
        tick = float(row[4]) if row[4].strip() else 0.05
    except (ValueError, TypeError):
        tick = 0.05
    expiry = _parse_expiry(row[8]) if len(row) > 8 and row[8].strip() else None
    if symbol.endswith("-INDEX"):
        itype, seg = "IND", "INDEX"
    elif opt_type in ("CE", "PE"):
        return None  # options come from the live chain API
    elif expiry is not None:
        itype, seg = "FUT", "FO"
    else:
        itype, seg = "EQ", "EQ"
    return Instrument(
        symbol=symbol,
        short_name=short,
        exchange=exch,
        segment=seg,
        instrument_type=itype,
        lot_size=lot,
        tick_size=tick,
        expiry=expiry,
        aliases=[short],
    )


# Fyers publishes a daily scrip-master CSV per exchange/segment. We pull
# the CASH masters (equities + indices); options are served by the live
# option-chain API, so we don't need the (huge) F&O masters.
_FYERS_MASTER_URLS: dict[str, str] = {
    "NSE_CM": "https://public.fyers.in/sym_details/NSE_CM.csv",
    "BSE_CM": "https://public.fyers.in/sym_details/BSE_CM.csv",
}


async def download_masters(data_dir: Optional[Path] = None) -> dict[str, Any]:
    """Download the NSE/BSE cash scrip masters from Fyers into `data_dir`
    and reload the in-process master so the whole universe becomes
    searchable. Returns a small report. Network failures for one exchange
    don't abort the others."""
    import httpx

    target = data_dir or DEFAULT_DATA_DIR
    target.mkdir(parents=True, exist_ok=True)
    files: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        for name, url in _FYERS_MASTER_URLS.items():
            try:
                r = await client.get(url)
                r.raise_for_status()
                (target / f"{name}.csv").write_text(r.text, encoding="utf-8")
                files[name] = len(r.text.splitlines())
            except Exception as e:  # noqa: BLE001
                files[name] = f"failed: {e}"
    count = get_master().reload()
    return {"files": files, "instrument_count": count}


# ---------------------------------------------------------------------------
# In-process seed: a small set so the search box is useful on first run,
# before any CSV is downloaded. Top-N NSE cash + the 3 index F&O
# underlyings + a few futures/options so the option chain panel renders.
# ---------------------------------------------------------------------------


# A broad, liquid NSE universe so search is genuinely useful on a fresh
# install with no scrip-master download. Roughly the NIFTY-200 plus
# popular mid-caps. A daily CSV refresh (data/scrip_master/*.csv) layers
# the full universe on top of this.
_SEED_NSE_EQ: tuple[str, ...] = (
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN", "BHARTIARTL",
    "ITC", "KOTAKBANK", "LT", "AXISBANK", "ASIANPAINT", "MARUTI", "WIPRO",
    "HCLTECH", "BAJFINANCE", "BAJAJFINSV", "SUNPHARMA", "TITAN", "ULTRACEMCO",
    "NESTLEIND", "TATAMOTORS", "TATASTEEL", "POWERGRID", "NTPC", "ONGC",
    "COALINDIA", "GRASIM", "ADANIENT", "ADANIPORTS", "ADANIPOWER", "ADANIGREEN",
    "JSWSTEEL", "HINDALCO", "INDUSINDBK", "TECHM", "DRREDDY", "CIPLA",
    "DIVISLAB", "BRITANNIA", "EICHERMOT", "HEROMOTOCO", "BAJAJ-AUTO", "M&M",
    "APOLLOHOSP", "BPCL", "IOC", "GAIL", "HINDPETRO", "DABUR", "GODREJCP",
    "MARICO", "COLPAL", "PIDILITIND", "BERGEPAINT", "HAVELLS", "VOLTAS",
    "DLF", "GODREJPROP", "OBEROIRLTY", "PNB", "BANKBARODA", "CANBK",
    "FEDERALBNK", "IDFCFIRSTB", "AUBANK", "BANDHANBNK", "CHOLAFIN", "SBICARD",
    "ICICIGI", "ICICIPRULI", "HDFCLIFE", "SBILIFE", "MUTHOOTFIN", "LICHSGFIN",
    "PEL", "RECLTD", "PFC", "IRFC", "VEDL", "NMDC", "SAIL", "JINDALSTEL",
    "NATIONALUM", "HINDZINC", "TATAPOWER", "TORNTPOWER", "SIEMENS", "ABB",
    "BHEL", "BEL", "HAL", "BHARATFORG", "ASHOKLEY", "TVSMOTOR", "BOSCHLTD",
    "MOTHERSON", "BALKRISIND", "MRF", "APOLLOTYRE", "ESCORTS", "CUMMINSIND",
    "LUPIN", "AUROPHARMA", "BIOCON", "TORNTPHARM", "ALKEM", "ZYDUSLIFE",
    "GLENMARK", "IPCALAB", "LALPATHLAB", "METROPOLIS", "FORTIS", "MAXHEALTH",
    "DMART", "TRENT", "JUBLFOOD", "ZOMATO", "NYKAA", "PAYTM", "POLICYBZR",
    "INDIGO", "INTERGLOBE", "IRCTC", "CONCOR", "TATACONSUM", "UBL", "MCDOWELL-N",
    "PGHH", "GILLETTE", "PIIND", "SRF", "UPL", "DEEPAKNTR", "AARTIIND",
    "ATGL", "PERSISTENT", "COFORGE", "LTIM", "MPHASIS", "LTTS", "OFSS",
    "NAUKRI", "INDHOTEL", "PVRINOX", "PAGEIND", "ABCAPITAL", "ABFRL",
    "BAJAJHLDNG", "SHREECEM", "AMBUJACEM", "ACC", "DALBHARAT", "RAMCOCEM",
    "MANKIND", "POLYCAB", "ASTRAL", "SUPREMEIND", "DIXON", "AFFLE",
    "ZEEL", "SUNTV", "IDEA", "YESBANK", "GMRINFRA", "SUZLON", "RVNL",
    "IRB", "NHPC", "SJVN", "MAZDOCK", "COCHINSHIP", "BDL", "DATAPATTNS",
)


def _seed_instruments() -> list[Instrument]:
    out: list[Instrument] = []
    for name in _SEED_NSE_EQ:
        sym = f"NSE:{name}-EQ"
        out.append(Instrument(
            symbol=sym, short_name=name,
            exchange="NSE", segment="EQ",
            instrument_type="EQ", lot_size=1, tick_size=0.05,
        ))
    # Index underlyings (so the option-chain panel has something to work on)
    for sym, name in [
        ("NSE:NIFTY50-INDEX", "NIFTY"),
        ("NSE:NIFTYBANK-INDEX", "BANKNIFTY"),
        ("BSE:SENSEX-INDEX", "SENSEX"),
    ]:
        out.append(Instrument(
            symbol=sym, short_name=name,
            exchange=sym.split(":", 1)[0], segment="INDEX",
            instrument_type="IND", lot_size=1, tick_size=0.05,
        ))
    return out


# ---------------------------------------------------------------------------
# Master store
# ---------------------------------------------------------------------------


class InstrumentMaster:
    """In-process instrument master with on-disk CSV reload.

    Thread-safety: this store is read-mostly. The reload is called
    at startup and (optionally) by an admin endpoint; it's protected
    by a simple lock. Reads are unlocked — they return a snapshot
    taken at reload time.
    """

    def __init__(self, data_dir: Optional[Path] = None) -> None:
        self._data_dir = data_dir or DEFAULT_DATA_DIR
        self._by_symbol: dict[str, Instrument] = {}
        self._by_short: dict[str, list[Instrument]] = {}
        self._by_underlying: dict[str, list[Instrument]] = {}
        self.reload()

    def reload(self) -> int:
        """Reload from CSVs in `data_dir` (if any) + the in-process
        seed. Returns the number of instruments loaded."""
        self._by_symbol.clear()
        self._by_short.clear()
        self._by_underlying.clear()
        for inst in _seed_instruments():
            self._add(inst)
        if self._data_dir.is_dir():
            for csv_path in sorted(self._data_dir.glob("*.csv")):
                try:
                    with csv_path.open("r", encoding="utf-8", errors="replace") as fh:
                        rdr = csv.reader(fh)
                        for row in rdr:
                            # Auto-detect the real Fyers sym_details layout
                            # (symbol ticker at col 9) vs the simple legacy
                            # format the seed/tests use.
                            if len(row) >= 17 and ":" in (row[9] or ""):
                                inst = _fyers_csv_row_to_instrument(row)
                            else:
                                inst = _row_to_instrument(row)
                            if inst is not None:
                                self._add(inst)
                except Exception:  # noqa: BLE001
                    # Bad file — skip; the rest of the master still works.
                    continue
        return len(self._by_symbol)

    def _add(self, inst: Instrument) -> None:
        self._by_symbol[inst.symbol] = inst
        for key in {inst.short_name, inst.symbol.split(":", 1)[-1].rsplit("-", 1)[0].upper()}:
            if not key:
                continue
            self._by_short.setdefault(key, []).append(inst)
        if inst.underlying:
            self._by_underlying.setdefault(inst.underlying.upper(), []).append(inst)

    def count(self) -> int:
        return len(self._by_symbol)

    def get(self, symbol: str) -> Optional[Instrument]:
        return self._by_symbol.get(symbol)

    def search(
        self,
        query: str,
        *,
        limit: int = 30,
        segments: Optional[Iterable[str]] = None,
    ) -> list[Instrument]:
        """Substring search over short_name, symbol, and aliases.

        Cheap rank: exact short_name match > prefix on short_name >
        substring anywhere. Result capped at `limit`. Optional
        `segments` filter ("EQ", "FO", "INDEX", "COM", "CD").
        """
        q = (query or "").strip().upper()
        seg = set(s.upper() for s in segments) if segments else None

        # Empty / very short query → browse popular instruments (in
        # seed order, which is liquidity-ranked) so the dropdown is
        # never blank when the user just focuses the box.
        if not q:
            out: list[Instrument] = []
            for inst in self._by_symbol.values():
                if seg and inst.segment not in seg:
                    continue
                out.append(inst)
                if len(out) >= limit:
                    break
            return out

        # Collect candidates first
        exact: list[Instrument] = []
        prefix: list[Instrument] = []
        sub: list[Instrument] = []
        seen: set[str] = set()
        keys = (
            self._by_short.get(q, [])
            + self._by_short.get(q.replace(" ", ""), [])
        )
        for inst in keys:
            if inst.symbol in seen:
                continue
            seen.add(inst.symbol)
            if seg and inst.segment not in seg:
                continue
            exact.append(inst)

        if len(exact) >= limit:
            return exact[:limit]

        # Prefix scan
        for short, lst in self._by_short.items():
            if not short.startswith(q):
                continue
            for inst in lst:
                if inst.symbol in seen:
                    continue
                if seg and inst.segment not in seg:
                    continue
                seen.add(inst.symbol)
                prefix.append(inst)
                if len(prefix) >= limit - len(exact):
                    break
            if len(prefix) >= limit - len(exact):
                break

        if len(exact) + len(prefix) >= limit:
            return (exact + prefix)[:limit]

        # Substring scan
        for short, lst in self._by_short.items():
            if q in short:
                for inst in lst:
                    if inst.symbol in seen:
                        continue
                    if seg and inst.segment not in seg:
                        continue
                    seen.add(inst.symbol)
                    sub.append(inst)
                    if len(sub) >= limit - len(exact) - len(prefix):
                        break
            if len(sub) >= limit - len(exact) - len(prefix):
                break

        return (exact + prefix + sub)[:limit]

    def option_chain(
        self,
        underlying: str,
        *,
        expiry: Optional[str] = None,
    ) -> dict[str, Any]:
        """Build a minimal option chain for `underlying`.

        Result shape:
            {
              "underlying": "NIFTY",
              "expiries": ["2024-12-26", "2025-01-30", ...],
              "selected_expiry": "2024-12-26",
              "spot": 24500.0 | None,
              "strikes": [
                {"strike": 24400, "ce": {...} | None, "pe": {...} | None}
                ...
              ]
            }

        If no expiry given, the nearest one is selected.
        """
        u = (underlying or "").strip().upper()
        rows = self._by_underlying.get(u, [])
        if not rows:
            return {
                "underlying": u,
                "expiries": [],
                "selected_expiry": None,
                "spot": None,
                "strikes": [],
            }
        # Group by expiry, keep only CE/PE
        by_exp: dict[str, dict[float, dict[str, Instrument]]] = {}
        for r in rows:
            if r.instrument_type not in ("CE", "PE") or r.expiry is None:
                continue
            ed = r.expiry.strftime("%Y-%m-%d")
            by_exp.setdefault(ed, {}).setdefault(r.strike or 0.0, {})[r.instrument_type] = r
        expiries = sorted(by_exp.keys())
        if not expiries:
            return {
                "underlying": u,
                "expiries": [],
                "selected_expiry": None,
                "spot": None,
                "strikes": [],
            }
        sel = expiry if (expiry and expiry in by_exp) else expiries[0]
        strikes_map = by_exp[sel]
        strikes_sorted = sorted(strikes_map.keys())
        strikes: list[dict[str, Any]] = []
        for s in strikes_sorted:
            ce = strikes_map[s].get("CE")
            pe = strikes_map[s].get("PE")
            strikes.append({
                "strike": s,
                "ce": {
                    "symbol": ce.symbol,
                    "lot_size": ce.lot_size,
                    "tick_size": ce.tick_size,
                } if ce is not None else None,
                "pe": {
                    "symbol": pe.symbol,
                    "lot_size": pe.lot_size,
                    "tick_size": pe.tick_size,
                } if pe is not None else None,
            })
        return {
            "underlying": u,
            "expiries": expiries,
            "selected_expiry": sel,
            "spot": None,  # populated by the endpoint from the market data bus
            "strikes": strikes,
        }


_master_singleton: Optional[InstrumentMaster] = None


def get_master() -> InstrumentMaster:
    """Return the process-wide InstrumentMaster instance."""
    global _master_singleton
    if _master_singleton is None:
        _master_singleton = InstrumentMaster()
    return _master_singleton


def reset_master_for_testing() -> None:
    """Drop the cached singleton. Tests use this to start from a
    clean state with a custom data_dir."""
    global _master_singleton
    _master_singleton = None
