"""Mover model — offline-trained P(this filing moves the stock), scored live.

The artifact is `AIdataset/model/live_model.json`, written by
`AIdataset/model/export_live.py`: standardisation stats, logistic-regression
coefficients for several named variants, and a frozen table of per-symbol /
per-category history. Scoring is a dot product, so this module imports
NOTHING outside the standard library — no numpy, no lightgbm, no wheels on a
1.9 GB server that has already been OOM-killed once.

READ THIS BEFORE TRUSTING THE NUMBER
------------------------------------
Phase 5 (plan.txt) killed the pre-filter classifier on measurement, and
nothing here overturns that: pooled accuracy is pinned by the base rate, and
a volatility-only feature set recovers most of the AUC. What the model does
carry is RANKING information — the top-scoring decile of same-session
filings moves ~3.9x as often as the average one. So the honest use is
ordering and sizing, and the gate defaults to OFF and to advisory-only.

Design, matching the rest of the risk path:
  - PURE + READ-ONLY. No DB, no I/O after the one-time artifact load.
  - FAIL-OPEN. A missing artifact, an unknown variant or a bad feature never
    raises into the caller — it returns None, meaning "no opinion", and the
    trade proceeds exactly as it would without this module.
  - MISSING FEATURES ARE NEUTRAL, NOT ZERO. An absent input is scored at the
    training mean (a standardized 0), contributing nothing to the logit
    rather than pretending the value was 0. `coverage` reports how much of
    the model actually got real data, so the Model page can show it.
"""
from __future__ import annotations

import json
import math
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from app.logging_config import get_logger

log = get_logger(__name__)

ARTIFACT = (Path(__file__).resolve().parents[2]
            / "AIdataset" / "model" / "live_model.json")

# Loaded once, guarded because monitors and the API can both touch it.
_lock = threading.Lock()
_artifact: Optional[dict[str, Any]] = None
_load_error: Optional[str] = None
_loaded_from: Optional[str] = None


@dataclass(frozen=True)
class MoverScore:
    """One filing's model verdict. `probability` is P(mover) on the model's
    own target — a >1.5% market-adjusted 30-minute move."""
    variant: str
    probability: float
    percentile: Optional[float]     # where this score sits on the holdout, 0-100
    coverage: float                 # fraction of features fed real live data
    n_features: int
    missing: list[str]              # live-unavailable inputs, for the UI

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load(force: bool = False) -> Optional[dict[str, Any]]:
    """Read (and cache) the artifact. None when it is absent or unreadable."""
    global _artifact, _load_error, _loaded_from
    with _lock:
        if _artifact is not None and not force:
            return _artifact
        try:
            _artifact = json.loads(ARTIFACT.read_text(encoding="utf-8"))
            _load_error = None
            _loaded_from = str(ARTIFACT)
            log.info("mover_model.loaded", variants=len(_artifact.get("variants", {})),
                     built_at=_artifact.get("built_at"))
        except Exception as e:  # noqa: BLE001 — absent artifact is a normal state
            _artifact, _load_error = None, str(e)
            log.warning("mover_model.load_failed", error=str(e), path=str(ARTIFACT))
        return _artifact


def variants() -> list[dict[str, Any]]:
    """Every selectable variant with its holdout metrics — the Model page's
    picker, and the only place the trade-offs are visible side by side."""
    art = load()
    if not art:
        return []
    out = []
    for key, v in art.get("variants", {}).items():
        out.append({
            "key": key,
            "label": v.get("label", key),
            "session": v.get("session"),
            "n_features": len(v.get("feature_names", [])),
            "metrics": v.get("metrics", {}),
            "percentiles": v.get("percentiles", {}),
        })
    return sorted(out, key=lambda r: r["key"])


def status() -> dict[str, Any]:
    """Everything the Model page needs to render without a second call."""
    art = load()
    return {
        "available": art is not None,
        "error": _load_error,
        "path": str(ARTIFACT),
        "loaded_from": _loaded_from,
        "built_at": (art or {}).get("built_at"),
        "target": (art or {}).get("target"),
        "default_variant": (art or {}).get("default_variant"),
        "n_symbols": len((art or {}).get("priors", {}).get("symbol", {})),
        "n_categories": len((art or {}).get("priors", {}).get("category", {})),
        "variants": variants(),
    }


def _sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-35.0, min(35.0, z))))


def _percentile_of(v: dict[str, Any], p: float) -> Optional[float]:
    """Approximate holdout percentile for a probability, from the stored
    quantiles. Linear between the known points — precise enough for a UI
    badge, and far more legible to an operator than the raw probability."""
    pcts = v.get("percentiles") or {}
    pts = sorted((int(k[1:]), float(x)) for k, x in pcts.items() if k.startswith("p"))
    if not pts:
        return None
    if p <= pts[0][1]:
        return float(pts[0][0])
    for (q0, v0), (q1, v1) in zip(pts, pts[1:]):
        if p <= v1:
            span = (v1 - v0) or 1e-12
            return round(q0 + (q1 - q0) * (p - v0) / span, 1)
    return 100.0


def build_features(
    *,
    symbol: str,
    headline: Optional[str] = None,
    event_type: Optional[str] = None,
    category: Optional[str] = None,
    sentiment: Optional[str] = None,
    sentiment_score: Optional[float] = None,
    confidence: Optional[float] = None,
    recommendation: Optional[str] = None,
    filed_at: Optional[datetime] = None,
    last_price: Optional[float] = None,
    market_cap_cr: Optional[float] = None,
    news_age_seconds: Optional[float] = None,
) -> dict[str, Any]:
    """Live inputs -> the model's feature dict. Absent inputs stay absent.

    The lagged history features (`sym_mover_rate`, `cat_mover_rate`, `rv20`,
    …) come from the artifact's frozen prior table rather than a live query:
    they are 18-month averages that do not move on one filing, and a DB
    round-trip per announcement in the hot path is exactly the latency the
    early-entry design refuses to spend.
    """
    art = load() or {}
    priors = art.get("priors", {})
    sym_priors = (priors.get("symbol") or {}).get((symbol or "").upper(), {})
    defaults = priors.get("defaults") or {}
    at = filed_at

    f: dict[str, Any] = {
        "ai_sentiment_score": sentiment_score,
        "ai_confidence": confidence,
        "headline_len": len(headline) if headline is not None else None,
        # px_t0_age_min is "how stale was the price anchor" in training; the
        # live equivalent is how old the news is when we score it.
        "px_t0_age_min": (news_age_seconds / 60.0) if news_age_seconds is not None else None,
        "log_px_pre": math.log1p(last_price) if last_price else None,
        "log_mcap": math.log1p(market_cap_cr) if market_cap_cr else None,
        # Frozen per-symbol history.
        "sym_prior_n": sym_priors.get("n"),
        "sym_mover_rate": sym_priors.get("mover_rate", defaults.get("mover_rate")),
        "sym_abs_move": sym_priors.get("abs_move", defaults.get("abs_move")),
        "rv20": sym_priors.get("rv20", defaults.get("rv20")),
        "range20": sym_priors.get("range20", defaults.get("range20")),
        "log_vol_pre": sym_priors.get("log_vol_pre", defaults.get("log_vol_pre")),
        # Per-category base rate, keyed on whichever label we have.
        "cat_mover_rate": (priors.get("category") or {}).get(
            str(category or event_type or ""), defaults.get("mover_rate")),
        # Categoricals — the runtime uses the same string levels as training.
        "category": category or event_type,
        "ai_event_type": event_type,
        "ai_sentiment": sentiment,
        "ai_recommendation": recommendation,
        "cap_tier": _cap_tier(market_cap_cr),
        "ai_label_source": "live",
    }
    if at is not None:
        f["minute_of_day"] = at.hour * 60 + at.minute
        f["weekday"] = at.weekday()
    return {k: v for k, v in f.items() if v is not None}


def _cap_tier(market_cap_cr: Optional[float]) -> Optional[str]:
    """AMFI-style banding, matching build_master.py's tiers."""
    if not market_cap_cr:
        return None
    if market_cap_cr >= 50_000:
        return "Large"
    if market_cap_cr >= 15_000:
        return "Mid"
    return "Small"


def score(features: dict[str, Any], variant: Optional[str] = None) -> Optional[MoverScore]:
    """P(mover) for one filing, or None when the model has no opinion.

    Never raises: every failure path — no artifact, unknown variant, a
    feature that will not coerce to a float — degrades to None or to the
    training mean for that one column."""
    art = load()
    if not art:
        return None
    key = variant or art.get("default_variant") or ""
    v = (art.get("variants") or {}).get(key)
    if v is None:
        log.warning("mover_model.unknown_variant", variant=key)
        return None

    try:
        names = v["feature_names"]
        mean, std, coef = v["mean"], v["std"], v["coef"]
        z = float(v["intercept"])
        seen = 0
        missing: list[str] = []
        for i, name in enumerate(names):
            if "=" in name:
                col, level = name.split("=", 1)
                raw = features.get(col)
                if raw is None:
                    # An absent categorical is absent from EVERY one-hot column;
                    # only count it once, on the first column of its block.
                    if col not in missing:
                        missing.append(col)
                    continue
                seen += 1
                x = 1.0 if str(raw) == level else 0.0
            else:
                raw = features.get(name)
                if raw is None:
                    missing.append(name)
                    x = mean[i]        # neutral: standardises to exactly 0
                else:
                    try:
                        x = float(raw)
                    except (TypeError, ValueError):
                        missing.append(name)
                        x = mean[i]
                    else:
                        seen += 1
            z += coef[i] * ((x - mean[i]) / (std[i] or 1.0))
        p = _sigmoid(z)
    except Exception as e:  # noqa: BLE001 — advisory only; never break a trade
        log.warning("mover_model.score_failed", error=str(e), variant=key)
        return None

    total = len(names)
    return MoverScore(
        variant=key,
        probability=round(p, 6),
        percentile=_percentile_of(v, p),
        coverage=round(seen / total, 4) if total else 0.0,
        n_features=total,
        missing=missing[:20],
    )


def verdict(
    s: Optional[MoverScore],
    *,
    min_probability: float,
    min_coverage: float = 0.0,
) -> tuple[str, str]:
    """("allow" | "block" | "insufficient", human reason).

    FAIL-OPEN in both directions that matter: no score, or a score built on
    too little real data, is "insufficient" and never blocks. Only a real
    score below the operator's threshold blocks."""
    if s is None:
        return "insufficient", "no model score (artifact missing or unreadable)"
    if s.coverage < min_coverage:
        return ("insufficient",
                f"only {s.coverage:.0%} of features had live data "
                f"(needs {min_coverage:.0%})")
    if s.probability < min_probability:
        return ("block",
                f"P(mover)={s.probability:.1%} is below the {min_probability:.1%} "
                f"floor (holdout percentile {s.percentile})")
    return "allow", f"P(mover)={s.probability:.1%}"


def _self_check() -> None:
    """Smallest thing that fails if the scoring math breaks."""
    art = load()
    assert art, f"no artifact at {ARTIFACT}"
    key = art["default_variant"]
    v = art["variants"][key]

    # 1. All-missing input must land exactly on the intercept: every feature
    #    sits at its own mean, so every standardized term is 0.
    empty = score({}, key)
    assert empty is not None
    # 1e-6, not 0: `probability` is rounded to 6 dp for the wire format.
    assert abs(empty.probability - _sigmoid(v["intercept"])) < 1e-6, empty.probability
    assert empty.coverage == 0.0

    # 2. A real feature must move the score off the intercept.
    idx = v["feature_names"].index("sym_mover_rate")
    hot = score({"sym_mover_rate": v["mean"][idx] + 5 * v["std"][idx]}, key)
    assert hot is not None and hot.probability != empty.probability
    assert hot.coverage > 0

    # 3. Fail-open: unknown variant and a junk feature both refuse to raise.
    assert score({}, "does-not-exist") is None
    assert score({"sym_mover_rate": "not-a-number"}, key) is not None

    # 4. The gate never blocks without a score.
    assert verdict(None, min_probability=0.9)[0] == "insufficient"
    assert verdict(empty, min_probability=1.1)[0] == "block"
    assert verdict(empty, min_probability=0.0)[0] == "allow"
    assert verdict(empty, min_probability=0.0, min_coverage=0.5)[0] == "insufficient"

    print(f"ok — {key}: intercept p={empty.probability:.4f}, "
          f"{empty.n_features} features, {len(variants())} variants")


if __name__ == "__main__":
    _self_check()
