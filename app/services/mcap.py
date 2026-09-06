"""Market capitalisation lookup and the operator's cap-tier bands.

`data/mcap.csv` is AMFI's half-yearly "Average Market Capitalization of
listed companies" sheet reduced to `symbol,mcap_cr` by
`scripts/build_mcap.py`. 5,239 symbols, 81 KB, read once into a dict —
the risk engine consults this on every signal, and a DB round-trip in
that path is exactly the latency the early-entry design refuses to spend.

The *tier* is deliberately not stored. AMFI ships its own SEBI
Large/Mid/Small column, but the operator sets the two boundaries in
Settings (`CAP_LARGE_MIN_CR`, `CAP_MID_MIN_CR`), so a retune is a form
field rather than a refetch.

NOT to be confused with `mover_model._cap_tier`, which bands the same
number against the thresholds the model was *trained* on. Those are
frozen by the training run and must never follow this setting.
"""
from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from app.logging_config import get_logger

log = get_logger(__name__)

MCAP_FILE = Path(__file__).resolve().parents[2] / "data" / "mcap.csv"

Tier = Literal["large", "mid", "small"]


@lru_cache(maxsize=1)
def _table() -> dict[str, float]:
    """symbol -> average market cap in Rs crore. Missing file = empty map.

    A missing or malformed sheet degrades to "market cap unknown for every
    symbol", which the caller handles explicitly. It must never raise: the
    cap filter is an operator preference, and an enrichment file has no
    business taking the risk engine down.
    """
    if not MCAP_FILE.exists():
        log.warning("mcap.file_missing", path=str(MCAP_FILE))
        return {}
    out: dict[str, float] = {}
    try:
        with MCAP_FILE.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                try:
                    out[(row["symbol"] or "").strip().upper()] = float(row["mcap_cr"])
                except (KeyError, TypeError, ValueError):
                    continue
    except OSError as exc:  # unreadable file — same degrade as absent
        log.warning("mcap.file_unreadable", path=str(MCAP_FILE), error=str(exc))
        return {}
    out.pop("", None)
    log.info("mcap.loaded", symbols=len(out))
    return out


def lookup(symbol: Optional[str]) -> Optional[float]:
    """Average market cap in Rs crore, or None when the symbol is unknown."""
    if not symbol:
        return None
    return _table().get(symbol.strip().upper())


def tier_of(mcap_cr: Optional[float], large_min: float, mid_min: float) -> Optional[Tier]:
    """Band a market cap against the operator's two boundaries.

    `large_min` below `mid_min` would make "large" unreachable, so the
    pair is normalised rather than trusted — the settings validator
    rejects it too, but this is the function the engine calls.
    """
    if mcap_cr is None:
        return None
    hi, lo = max(large_min, mid_min), min(large_min, mid_min)
    if mcap_cr >= hi:
        return "large"
    if mcap_cr >= lo:
        return "mid"
    return "small"


def reset_cache() -> None:
    """Drop the in-memory table so a rebuilt csv is picked up. Tests, and
    the operator's 'reload market caps' action."""
    _table.cache_clear()


def _selftest() -> None:
    assert tier_of(60_000, 50_000, 15_000) == "large"
    assert tier_of(50_000, 50_000, 15_000) == "large"   # boundary is inclusive
    assert tier_of(20_000, 50_000, 15_000) == "mid"
    assert tier_of(15_000, 50_000, 15_000) == "mid"
    assert tier_of(14_999, 50_000, 15_000) == "small"
    assert tier_of(None, 50_000, 15_000) is None
    # Swapped boundaries must not make a tier unreachable.
    assert tier_of(60_000, 15_000, 50_000) == "large"
    assert lookup(None) is None
    t = _table()
    if t:
        assert lookup("reliance") == lookup("RELIANCE"), "lookup must be case-insensitive"
        assert lookup("__nope__") is None
    print(f"mcap selftest ok ({len(t):,} symbols)")


if __name__ == "__main__":
    _selftest()
