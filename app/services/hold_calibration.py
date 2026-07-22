"""HOLD calibration — is the bot correctly passing, or missing movers?

The DatasetBuilder already labels every filing with its real 15-minute
forward reaction (ret_15m_pct, MFE/MAE excursions, UP/DOWN/FLAT). The
analyzer, meanwhile, recommends HOLD on ~99.5% of filings. This module
joins those two facts to answer the question that decides whether the
rules/prompt are too tight:

    OF THE FILINGS THE LLM DECLINED, HOW MANY ACTUALLY MOVED?

Pure + read-only: every function takes plain dicts and returns plain
dicts, so the whole thing is unit-testable with no DB and no I/O. The
`/api/dataset/calibration` endpoint does the query + row assembly (it
reuses the dataset row builder) and hands the rows here.

Key definitions
---------------
excursion magnitude — ``max(|MFE|, |MAE|)`` over the 15m horizon: the
    biggest swing in EITHER direction. A HOLD carries no side, so a -3%
    drop is as much a missed (short) opportunity as a +3% rise is a
    missed long. This is the "was there a tradeable move" measure; the
    NET move ``ret_15m_pct`` is reported alongside it.
mover — a filing whose excursion magnitude >= ``move_threshold_pct``.
the comparison that matters — the mover rate among TAKEN filings vs
    among DECLINED ones. If declined filings move about as often as taken
    ones, the selection isn't discriminating, and THAT — not the raw
    99.5% HOLD rate — is the real verdict on the prompt/rules.
"""
from __future__ import annotations

from statistics import mean, median
from typing import Any, Callable, Optional

# Confidence bands for the "does the LLM's own confidence track reality"
# breakdown. Half-open [lo, hi); the last band's hi is > 1 so 1.0 lands.
_CONF_BANDS: tuple[tuple[float, float, str], ...] = (
    (0.0, 0.3, "0.0-0.3"),
    (0.3, 0.5, "0.3-0.5"),
    (0.5, 0.7, "0.5-0.7"),
    (0.7, 0.9, "0.7-0.9"),
    (0.9, 1.0001, "0.9-1.0"),
)


def excursion_magnitude(
    mfe_15m: Optional[float],
    mae_15m: Optional[float],
    ret_15m: Optional[float],
) -> Optional[float]:
    """Biggest 15m swing in either direction (percent).

    MFE is the favorable excursion, MAE the adverse one (opposite signs
    for a long); their magnitudes bound how far price travelled up and
    down within the window. We take the larger magnitude — the size of
    the move a directional trade could have caught. Falls back to the net
    ``ret_15m`` magnitude when the excursions are absent (older rows), and
    to None when nothing is knowable (an incomplete label)."""
    mags = [abs(v) for v in (mfe_15m, mae_15m) if v is not None]
    if mags:
        return max(mags)
    if ret_15m is not None:
        return abs(ret_15m)
    return None


def normalise_dataset_row(row: dict[str, Any]) -> dict[str, Any]:
    """Reduce an assembled `/api/dataset` row to just the fields the
    calibration math needs. Pure — no DB, safe on partial rows."""
    return {
        "symbol": row.get("symbol"),
        "recommendation": (str(row.get("recommendation") or "").upper() or None),
        "taken": bool(row.get("trade_taken")),
        "event_type": row.get("event_type") or "UNKNOWN",
        "confidence": row.get("confidence"),
        "sentiment": row.get("sentiment"),
        "abs_excursion": excursion_magnitude(
            row.get("mfe_15m_pct"), row.get("mae_15m_pct"), row.get("ret_15m_pct")
        ),
        "ret_15m_pct": row.get("ret_15m_pct"),
        "label_15m": row.get("label_15m"),
    }


def _conf_band(c: Optional[float]) -> str:
    if c is None:
        return "unknown"
    for lo, hi, label in _CONF_BANDS:
        if lo <= float(c) < hi:
            return label
    return "unknown"


def _empty_bucket(key: str) -> dict[str, Any]:
    return {
        "key": key, "n": 0, "movers": 0, "mover_rate": 0.0,
        "big_movers": 0, "big_mover_rate": 0.0,
        "avg_abs_excursion": None, "median_abs_excursion": None,
        "avg_ret_15m": None, "up": 0, "down": 0, "flat": 0,
    }


def summarise_bucket(
    samples: list[dict[str, Any]],
    *,
    move_threshold_pct: float,
    big_threshold_pct: float,
    key: str = "overall",
) -> dict[str, Any]:
    """Aggregate a group of normalised rows into one calibration bucket.

    Only rows with a known ``abs_excursion`` (a complete 15m label) count;
    the caller is expected to have filtered, but we guard anyway."""
    vals = [s for s in samples if s.get("abs_excursion") is not None]
    n = len(vals)
    if n == 0:
        return _empty_bucket(key)
    exc = [float(s["abs_excursion"]) for s in vals]
    movers = sum(1 for e in exc if e >= move_threshold_pct)
    big = sum(1 for e in exc if e >= big_threshold_pct)
    rets = [float(s["ret_15m_pct"]) for s in vals if s.get("ret_15m_pct") is not None]
    labels = [s.get("label_15m") for s in vals]
    return {
        "key": key,
        "n": n,
        "movers": movers,
        "mover_rate": round(movers / n, 4),
        "big_movers": big,
        "big_mover_rate": round(big / n, 4),
        "avg_abs_excursion": round(mean(exc), 3),
        "median_abs_excursion": round(median(exc), 3),
        "avg_ret_15m": round(mean(rets), 3) if rets else None,
        "up": labels.count("UP"),
        "down": labels.count("DOWN"),
        "flat": labels.count("FLAT"),
    }


def _group(
    samples: list[dict[str, Any]],
    key_fn: Callable[[dict[str, Any]], str],
    *,
    move_threshold_pct: float,
    big_threshold_pct: float,
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for s in samples:
        groups.setdefault(key_fn(s), []).append(s)
    return [
        summarise_bucket(
            v, move_threshold_pct=move_threshold_pct,
            big_threshold_pct=big_threshold_pct, key=k,
        )
        for k, v in groups.items()
    ]


def _verdict(
    declined: dict[str, Any], taken: dict[str, Any], move_threshold_pct: float
) -> str:
    """A one-line, plain-English read of the comparison — so the report
    interprets itself instead of leaving the operator to eyeball rates."""
    if declined["n"] < 30:
        return (
            "Not enough labeled declined filings yet to judge — need ~30+ "
            f"complete rows (have {declined['n']})."
        )
    dr = declined["mover_rate"] * 100.0
    head = (
        f"{dr:.0f}% of declined filings moved ≥{move_threshold_pct:.1f}% "
        "within 15m"
    )
    if taken["n"] < 10:
        return (
            head + " (too few taken trades yet to compare selection quality)."
        )
    tr = taken["mover_rate"] * 100.0
    if taken["mover_rate"] > declined["mover_rate"] * 1.5:
        tail = f" vs {tr:.0f}% of taken — selection is discriminating well."
    elif declined["mover_rate"] >= taken["mover_rate"]:
        tail = (
            f" vs {tr:.0f}% of taken — the bot is passing on as many movers "
            "as it trades; the rules/prompt look too tight."
        )
    else:
        tail = f" vs {tr:.0f}% of taken."
    return head + tail


def compute_calibration(
    rows: list[dict[str, Any]],
    *,
    move_threshold_pct: float = 1.5,
    big_threshold_pct: float = 3.0,
    top_n: int = 10,
) -> dict[str, Any]:
    """The full HOLD-calibration report over normalised dataset rows.

    `rows` come from `normalise_dataset_row`. Rows without a complete 15m
    label (``abs_excursion is None``) are ignored. Breakdowns are computed
    over the HOLD-recommended slice (the LLM's explicit passes); the
    taken-vs-declined comparison uses the whole complete set.
    """
    kw = {
        "move_threshold_pct": float(move_threshold_pct),
        "big_threshold_pct": float(big_threshold_pct),
    }
    complete = [r for r in rows if r.get("abs_excursion") is not None]
    hold = [r for r in complete if r.get("recommendation") == "HOLD"]
    taken = [r for r in complete if r.get("taken")]
    declined = [r for r in complete if not r.get("taken")]

    by_event = _group(hold, lambda r: r.get("event_type") or "UNKNOWN", **kw)
    by_event.sort(key=lambda b: (b["mover_rate"], b["n"]), reverse=True)

    by_conf = _group(hold, lambda r: _conf_band(r.get("confidence")), **kw)
    by_conf.sort(key=lambda b: b["key"])

    by_sent = _group(hold, lambda r: str(r.get("sentiment") or "unknown"), **kw)
    by_sent.sort(key=lambda b: b["n"], reverse=True)

    # Concrete examples: the biggest movers the LLM passed on. These make
    # the report credible and immediately actionable (the operator can
    # eyeball whether these were genuinely tradeable).
    missed = sorted(hold, key=lambda r: r["abs_excursion"], reverse=True)[:top_n]
    top_missed = [
        {
            "symbol": r.get("symbol"),
            "event_type": r.get("event_type"),
            "confidence": r.get("confidence"),
            "sentiment": r.get("sentiment"),
            "abs_excursion": round(float(r["abs_excursion"]), 2),
            "ret_15m_pct": (
                round(float(r["ret_15m_pct"]), 2)
                if r.get("ret_15m_pct") is not None else None
            ),
            "label_15m": r.get("label_15m"),
        }
        for r in missed
    ]

    comparison = {
        "taken": summarise_bucket(taken, key="taken", **kw),
        "declined": summarise_bucket(declined, key="declined", **kw),
    }
    return {
        "thresholds": {
            "move_pct": float(move_threshold_pct),
            "big_pct": float(big_threshold_pct),
        },
        "sample": {
            "total_complete": len(complete),
            "hold": len(hold),
            "taken": len(taken),
            "declined": len(declined),
        },
        "hold_overall": summarise_bucket(hold, key="HOLD", **kw),
        "by_event_type": by_event,
        "by_confidence": by_conf,
        "by_sentiment": by_sent,
        "comparison": comparison,
        "top_missed": top_missed,
        "verdict": _verdict(
            comparison["declined"], comparison["taken"], float(move_threshold_pct)
        ),
    }
