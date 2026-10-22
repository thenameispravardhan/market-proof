"""Point-in-time index membership: which stocks were IN an index on a given day.

A ranking backtest over "the NIFTY 50" that uses today's 50 names has
survivorship bias built in: it never holds the stocks that were dropped
after falling, and holds the winners that were added after rising — before
they were added. The honest universe on each day is the index as it was
that day.

Two sources, per index, under data/universe/<index>/:

  snapshots/<YYYYMMDD>.txt  the constituent list as published that day
                            (niftyindices.com), one ticker per line —
                            recorded whenever the list is fetched;
  changes.csv               date,symbol,action (add | remove), the effective
                            date of each inclusion / exclusion — NSE's
                            index-change announcements, imported.

Membership on a day = the nearest snapshot ON or AFTER it with the changes
in between undone (or, after the last snapshot, the last one with later
changes applied). Before the change log's first entry nothing can be known
about changes: `coverage()` says from when the answer is trustworthy.
"""
from __future__ import annotations

import csv
import io
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

ROOT = Path(__file__).resolve().parents[2] / "data" / "universe"
IST = 19800
DAY = 86400
LIST_URL = "https://www.niftyindices.com/IndexConstituent/{file}"
INDEXES: dict[str, dict[str, str]] = {
    "nifty50": {"label": "NIFTY 50", "file": "ind_nifty50list.csv"},
    "nifty100": {"label": "NIFTY 100", "file": "ind_nifty100list.csv"},
    "niftynext50": {"label": "NIFTY Next 50", "file": "ind_niftynext50list.csv"},
    "niftybank": {"label": "NIFTY Bank", "file": "ind_niftybanklist.csv"},
    "nifty500": {"label": "NIFTY 500", "file": "ind_nifty500list.csv"},
    "midcap150": {"label": "NIFTY Midcap 150", "file": "ind_niftymidcap150list.csv"},
    "smallcap250": {"label": "NIFTY Smallcap 250", "file": "ind_niftysmallcap250list.csv"},
}
SNAP_MAX_AGE_DAYS = 7


def ist_day(ts: float) -> int:
    return int((ts + IST) // DAY)


def day_of(d: date) -> int:
    return (d - date(1970, 1, 1)).days


def ymd(day: int) -> str:
    return (date(1970, 1, 1) + timedelta(days=day)).strftime("%Y%m%d")


def _dir(index: str, root: Optional[Path]) -> Path:
    if index not in INDEXES:
        raise ValueError(f"unknown index {index!r} (one of {', '.join(INDEXES)})")
    return (root or ROOT) / index


def snapshots(index: str, root: Optional[Path] = None) -> dict[int, set[str]]:
    """{IST day: tickers} of every recorded list."""
    p = _dir(index, root) / "snapshots"
    out = {}
    for f in sorted(p.glob("*.txt")) if p.is_dir() else []:
        try:
            d = day_of(datetime.strptime(f.stem, "%Y%m%d").date())
        except ValueError:
            continue
        out[d] = {x.strip().upper() for x in f.read_text(encoding="utf-8").splitlines() if x.strip()}
    return out


def record_snapshot(index: str, symbols: Iterable[str], day: Optional[int] = None, root: Optional[Path] = None) -> int:
    """Save `symbols` as `index`'s list on `day` (default today); returns how many."""
    syms = sorted({s.strip().upper() for s in symbols if s and s.strip()})
    if not syms:
        raise ValueError("an empty list is not a snapshot")
    p = _dir(index, root) / "snapshots"
    p.mkdir(parents=True, exist_ok=True)
    (p / f"{ymd(ist_day(time.time()) if day is None else day)}.txt").write_text("\n".join(syms) + "\n", encoding="utf-8")
    return len(syms)


def changes(index: str, root: Optional[Path] = None) -> list[tuple[int, str, str]]:
    """(IST day it took effect, ticker, add | remove), oldest first."""
    p = _dir(index, root) / "changes.csv"
    if not p.exists():
        return []
    out = []
    for r in csv.DictReader(p.open(encoding="utf-8")):
        out.append((int(r["day"]), r["symbol"], r["action"]))
    return sorted(out)


def import_changes(index: str, text: str, root: Optional[Path] = None) -> dict[str, int]:
    """Merge a CSV of `date,symbol,action` (date YYYY-MM-DD, action add |
    remove; a header row is fine) into the index's change log. Bad rows are
    an error naming the line — a silently skipped exclusion would put a
    dropped stock back in the universe."""
    rows = list(csv.reader(io.StringIO(text.strip())))
    if rows and rows[0] and rows[0][0].strip().lower() in ("date", "effective", "effective_date"):
        rows = rows[1:]
    new: set[tuple[int, str, str]] = set()
    for n, r in enumerate(rows, 1):
        if not r or not any(x.strip() for x in r):
            continue
        if len(r) < 3:
            raise ValueError(f"line {n}: want date,symbol,action")
        try:
            d = day_of(date.fromisoformat(r[0].strip()))
        except ValueError:
            raise ValueError(f"line {n}: date {r[0]!r} is not YYYY-MM-DD") from None
        act = r[2].strip().lower()
        act = {"in": "add", "include": "add", "inclusion": "add", "out": "remove", "exclude": "remove",
               "exclusion": "remove"}.get(act, act)
        if act not in ("add", "remove"):
            raise ValueError(f"line {n}: action {r[2]!r} is add or remove")
        sym = r[1].strip().upper().removeprefix("NSE:").removesuffix("-EQ")
        if not sym:
            raise ValueError(f"line {n}: no symbol")
        new.add((d, sym, act))
    old = set(changes(index, root))
    merged = sorted(old | new)
    p = _dir(index, root)
    p.mkdir(parents=True, exist_ok=True)
    with (p / "changes.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["day", "symbol", "action"])
        w.writerows(merged)
    return {"added": len(merged) - len(old), "total": len(merged)}


def members_at(index: str, day: int, root: Optional[Path] = None, *, _snaps=None, _chg=None) -> Optional[set[str]]:
    """The index's members on `day`, or None with no snapshot to start from."""
    snaps = snapshots(index, root) if _snaps is None else _snaps
    chg = changes(index, root) if _chg is None else _chg
    if not snaps:
        return None
    after = [d for d in snaps if d >= day]
    if after:                                        # walk back from the nearest later list
        s = min(after)
        out = set(snaps[s])
        for d, sym, act in reversed(chg):
            if day < d <= s:
                if act == "add":
                    out.discard(sym)
                else:
                    out.add(sym)
        return out
    s = max(snaps)                                   # forward from the last list
    out = set(snaps[s])
    for d, sym, act in chg:
        if s < d <= day:
            if act == "add":
                out.add(sym)
            else:
                out.discard(sym)
    return out


def members_between(index: str, start_day: int, end_day: int, root: Optional[Path] = None) -> dict[str, set[int]]:
    """{ticker: the IST days in [start_day, end_day] it was a member} — every
    stock that was in the index at any point of the range, dropped ones too."""
    snaps, chg = snapshots(index, root), changes(index, root)
    if not snaps:
        raise ValueError(f"no recorded list of {INDEXES[index]['label']} yet — record one (Research → universes)")
    out: dict[str, set[int]] = {}
    for d in range(start_day, end_day + 1):
        for s in members_at(index, d, root, _snaps=snaps, _chg=chg) or ():
            out.setdefault(s, set()).add(d)
    return out


def coverage(index: str, root: Optional[Path] = None) -> dict[str, Any]:
    snaps, chg = snapshots(index, root), changes(index, root)
    first_change = chg[0][0] if chg else None
    first_snap = min(snaps) if snaps else None
    # Before the change log starts (or, with no log, before the first list)
    # membership is the earliest known list unchanged: an assumption, not data.
    trusted = min(x for x in (first_change, first_snap) if x is not None) if snaps else None
    return {"index": index, "label": INDEXES[index]["label"], "snapshots": len(snaps),
            "first_snapshot": ymd(first_snap) if first_snap is not None else None,
            "last_snapshot": ymd(max(snaps)) if snaps else None,
            "changes": len(chg), "first_change": ymd(first_change) if first_change is not None else None,
            "trusted_from": ymd(trusted) if trusted is not None else None,
            "members_now": len(members_at(index, ist_day(time.time()), root, _snaps=snaps, _chg=chg) or ())}


def fetch_snapshot(index: str, root: Optional[Path] = None, force: bool = False) -> dict[str, Any]:
    """Today's list from niftyindices.com, recorded unless one from the last
    week is already there (or `force`)."""
    from app.algo.flow_universe import _parse_list

    snaps = snapshots(index, root)
    today = ist_day(time.time())
    if snaps and not force and today - max(snaps) < SNAP_MAX_AGE_DAYS:
        return {"recorded": False, "day": ymd(max(snaps)), "members": len(snaps[max(snaps)])}
    import httpx

    r = httpx.get(LIST_URL.format(file=INDEXES[index]["file"]), timeout=20, follow_redirects=True,
                  headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
                           "Accept": "text/csv,*/*"})
    syms = _parse_list(r.text) if r.status_code == 200 else []
    if len(syms) < 5:
        raise RuntimeError(f"niftyindices.com returned no list for {INDEXES[index]['label']} (HTTP {r.status_code})")
    return {"recorded": True, "day": ymd(today), "members": record_snapshot(index, syms, today, root)}
