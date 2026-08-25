"""Export a LIVE-SCOREABLE mover model to a single stdlib-readable JSON file.

Why not just ship the LightGBM booster train.py already writes?
------------------------------------------------------------------
Two hard blockers, both discovered when wiring it into the bot:

  1. `lgbm_*.txt` needs `text_score` — the output of a TF-IDF + linear model
     that train.py fits IN MEMORY and never persists. One of its 29 inputs
     simply does not exist outside that training run, so the booster cannot
     be scored anywhere else. Ever.
  2. Scoring it live means lightgbm + numpy + scipy on the server. That box
     is 1.9 GB and was OOM-killed on 2026-08-15; `requirements.txt` keeps
     duckdb precisely because it streams instead of loading. Adding ~250 MB
     of resident wheels to run 400 trees per filing is the wrong trade.

So this exporter trains a LOGISTIC REGRESSION on the live-available feature
set and writes plain coefficients. The measured cost is small — metrics.csv
puts LogReg at 0.8016 AUC / 3.85x top-decile lift against LightGBM's 0.8210
/ 4.09x on the same split — and the benefit is that the artifact is a 100 KB
JSON file scored by `app/services/mover_model.py` with nothing but the
standard library. numpy is used HERE, offline, never at runtime.

Read the README verdict before trusting any of this: the pooled headroom
over the base rate is under 1pp (plan.txt, Phase 5). The honest use of this
model is ranking and sizing, not a hard pre-filter.

    python AIdataset/model/export_live.py            # all variants
    python AIdataset/model/export_live.py --quick    # skip next_session

Writes AIdataset/model/live_model.json.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import train  # noqa: E402  — reuse its feature builder verbatim, never re-derive it

OUT = ROOT / "live_model.json"
# Categorical levels kept per column; everything rarer collapses to "OTHER".
# `category` alone has 100+ levels and the long tail is single-digit counts —
# one-hotting all of them buys noise and a bigger artifact.
MAX_LEVELS = 25
# Features the bot can actually populate at signal time. The rest of train.py's
# numeric set stays in the model but is fed the training mean when absent (that
# is a standardized 0 — a neutral contribution, not a guess).
NUMERIC = [
    "mcap_rank", "ai_sentiment_score", "ai_confidence", "px_t0_age_min",
    "log_mcap", "log_attachment", "log_px_pre", "log_vol_pre", "log_shared_by",
    "minute_of_day", "weekday", "headline_len",
    "sym_prior_n", "sym_mover_rate", "sym_abs_move", "sym_rel_vol",
    "mins_since_prev", "sym_ann_today", "mkt_ann_bucket", "cat_mover_rate",
    "rv20", "range20",
]
CATEGORICAL = ["category", "ai_event_type", "ai_sentiment", "ai_recommendation",
               "cap_tier", "ai_label_source"]
# Named feature subsets the operator can switch between on the Model page.
# "vol_only" exists to make Phase 5's finding visible rather than arguable:
# four volatility features with zero news content recover most of the AUC.
VARIANT_FEATURES: dict[str, list[str]] = {
    "full": NUMERIC,
    "no_news": ["sym_prior_n", "sym_mover_rate", "sym_abs_move", "sym_rel_vol",
                "rv20", "range20", "log_px_pre", "log_vol_pre", "log_mcap",
                "mcap_rank", "minute_of_day"],
    "vol_only": ["rv20", "range20", "sym_abs_move", "sym_rel_vol"],
}


def fit_logistic(X: np.ndarray, y: np.ndarray, l2: float = 1.0,
                 iters: int = 30) -> tuple[np.ndarray, float]:
    """IRLS (Newton) logistic regression with ridge. Returns (coef, intercept).

    Newton converges in ~10 steps on this shape, so there is no learning rate
    to tune and no "did it converge?" question. The ridge term is not for
    accuracy — it keeps the Hessian invertible when a one-hot level is nearly
    constant on the training slice, which happens with rare categories.
    """
    n, p = X.shape
    A = np.hstack([np.ones((n, 1)), X])          # column 0 = intercept
    w = np.zeros(p + 1)
    penalty = np.eye(p + 1) * l2
    penalty[0, 0] = 0.0                          # never penalise the intercept
    for _ in range(iters):
        z = A @ w
        mu = 1.0 / (1.0 + np.exp(-np.clip(z, -35, 35)))
        s = np.clip(mu * (1 - mu), 1e-6, None)
        # Newton step on the penalised log-likelihood.
        H = A.T @ (A * s[:, None]) + penalty
        g = A.T @ (y - mu) - penalty @ w
        try:
            step = np.linalg.solve(H, g)
        except np.linalg.LinAlgError:
            step = np.linalg.lstsq(H, g, rcond=None)[0]
        w = w + step
        if np.abs(step).max() < 1e-8:
            break
    return w[1:], float(w[0])


def auc(y: np.ndarray, p: np.ndarray) -> float:
    """ROC-AUC via the rank identity — no sklearn, and ties handled correctly."""
    order = np.argsort(p, kind="mergesort")
    ranks = np.empty(len(p), dtype=float)
    ranks[order] = np.arange(1, len(p) + 1)
    # Average the ranks inside each tie group, or identical scores inflate AUC.
    sp = p[order]
    i = 0
    while i < len(sp):
        j = i
        while j + 1 < len(sp) and sp[j + 1] == sp[i]:
            j += 1
        if j > i:
            ranks[order[i:j + 1]] = (i + 1 + j + 1) / 2.0
        i = j + 1
    pos, neg = y.sum(), len(y) - y.sum()
    if pos == 0 or neg == 0:
        return float("nan")
    return float((ranks[y == 1].sum() - pos * (pos + 1) / 2) / (pos * neg))


def top_decile_lift(y: np.ndarray, p: np.ndarray) -> float:
    """Mover rate in the highest-scoring 10% over the base rate. The number
    that decides whether ranking by this model is worth anything."""
    k = max(1, len(p) // 10)
    top = np.argsort(-p, kind="mergesort")[:k]
    base = y.mean()
    return float(y[top].mean() / base) if base > 0 else float("nan")


def levels(s: pd.Series) -> list[str]:
    counts = s.astype("string").fillna("NA").value_counts()
    return sorted(counts.index[:MAX_LEVELS].tolist())


def design(X: pd.DataFrame, numeric: list[str],
           cat_levels: dict[str, list[str]]) -> tuple[np.ndarray, list[str]]:
    """Numeric columns then one-hot blocks, in a FIXED order the runtime
    reproduces. NaN -> 0 after standardisation happens in the caller; here a
    NaN is left in place so the caller's mean/std see the real distribution."""
    cols, names = [], []
    for c in numeric:
        cols.append(pd.to_numeric(X[c], errors="coerce").to_numpy(dtype=float))
        names.append(c)
    for c, lv in cat_levels.items():
        raw = X[c].astype("string").fillna("NA")
        for level in lv:
            cols.append((raw == level).to_numpy(dtype=float))
            names.append(f"{c}={level}")
    return np.column_stack(cols), names


def build_variant(Xtr, ytr, Xte, yte, numeric, cat_levels) -> dict:
    Atr, names = design(Xtr, numeric, cat_levels)
    Ate, _ = design(Xte, numeric, cat_levels)
    # Standardise on TRAIN only. A missing value becomes the mean, i.e. a
    # standardized 0, which contributes nothing — the deliberate behaviour for
    # the live path where several inputs are simply unavailable.
    mean = np.nanmean(Atr, axis=0)
    std = np.nanstd(Atr, axis=0)
    std[~np.isfinite(std) | (std < 1e-9)] = 1.0
    mean = np.where(np.isfinite(mean), mean, 0.0)
    Ztr = np.nan_to_num((Atr - mean) / std, nan=0.0, posinf=0.0, neginf=0.0)
    Zte = np.nan_to_num((Ate - mean) / std, nan=0.0, posinf=0.0, neginf=0.0)

    coef, intercept = fit_logistic(Ztr, ytr.to_numpy(dtype=float))
    pte = 1.0 / (1.0 + np.exp(-np.clip(Zte @ coef + intercept, -35, 35)))
    yv = yte.to_numpy(dtype=int)
    return {
        "feature_names": names,
        "numeric": numeric,
        "categoricals": cat_levels,
        "mean": [round(float(v), 8) for v in mean],
        "std": [round(float(v), 8) for v in std],
        "coef": [round(float(v), 8) for v in coef],
        "intercept": round(float(intercept), 8),
        "metrics": {
            "n_train": int(len(ytr)),
            "n_test": int(len(yte)),
            "base_rate": round(float(yv.mean()), 6),
            "roc_auc": round(auc(yv, pte), 6),
            "top_decile_lift": round(top_decile_lift(yv, pte), 4),
        },
        # Score percentiles on the holdout. These are what the threshold slider
        # on the Model page is calibrated against — "top 5%" is a decision an
        # operator can reason about; "p > 0.23" is not.
        "percentiles": {
            f"p{q}": round(float(np.percentile(pte, q)), 6)
            for q in (50, 75, 90, 95, 99)
        },
    }


def build_priors(session: str) -> dict:
    """Per-symbol and per-category history the runtime cannot compute itself.

    These are the lagged features (`sym_mover_rate`, `cat_mover_rate`, `rv20`,
    …) frozen at export time. They drift slowly — a symbol's mover rate over
    18 months does not move on one filing — so a re-export when the corpus is
    refreshed is the maintenance, not a live recompute.
    """
    m = pd.read_parquet(train.MASTER)
    d = m[m.usable.fillna(False) & m.anchor_time.notna()].sort_values("anchor_time")
    t = train.TARGET
    g = d.groupby("symbol", sort=False)
    sym = pd.DataFrame({
        "n": g[t].size(),
        "mover_rate": g[t].mean(),
        "abs_move": g.adj_30m.apply(lambda s: s.abs().mean()),
        "log_vol_pre": g.vol_pre.apply(lambda s: float(np.log1p(s).mean())),
    })
    vol = train.symbol_vol()
    latest = vol.sort_values("date").groupby("symbol").last()[["rv20", "range20"]]
    sym = sym.join(latest, how="left")

    def clean(v):
        return None if v is None or not np.isfinite(v) else round(float(v), 6)

    symbols = {
        str(s): {k: clean(row[k]) for k in
                 ("n", "mover_rate", "abs_move", "log_vol_pre", "rv20", "range20")}
        for s, row in sym.iterrows()
    }
    for s in symbols:
        symbols[s]["n"] = int(symbols[s]["n"] or 0)

    cat = d.groupby("category")[t].mean()
    return {
        "symbol": symbols,
        "category": {str(k): round(float(v), 6) for k, v in cat.items()
                     if np.isfinite(v)},
        "defaults": {
            "mover_rate": clean(d[t].mean()),
            "abs_move": clean(d.adj_30m.abs().mean()),
            "rv20": clean(vol.rv20.median()),
            "range20": clean(vol.range20.median()),
            "log_vol_pre": clean(float(np.log1p(d.vol_pre).mean())),
        },
        "session": session,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true",
                    help="same_session only — skip the next_session variants")
    args = ap.parse_args()

    sessions = ["same_session"] if args.quick else ["same_session", "next_session"]
    out: dict = {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "target": train.TARGET,
        "variants": {},
    }
    for session in sessions:
        Xtr, Xte, ytr, yte, *_ = train.build(session)
        cat_levels = {c: levels(Xtr[c]) for c in CATEGORICAL}
        for name, numeric in VARIANT_FEATURES.items():
            key = f"{session}:{name}"
            v = build_variant(Xtr, ytr, Xte, yte, numeric,
                              cat_levels if name == "full" else {})
            v["session"] = session
            v["label"] = f"{session.replace('_', ' ')} · {name}"
            out["variants"][key] = v
            m = v["metrics"]
            print(f"  {key:<28} AUC {m['roc_auc']:.4f}  "
                  f"lift {m['top_decile_lift']:.2f}x  base {m['base_rate']:.2%}")

    out["priors"] = build_priors(sessions[0])
    out["default_variant"] = f"{sessions[0]}:full"
    OUT.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    print(f"\nwrote {OUT}  {OUT.stat().st_size / 1024:.0f} KB  "
          f"{len(out['variants'])} variants  "
          f"{len(out['priors']['symbol']):,} symbols")


if __name__ == "__main__":
    main()
