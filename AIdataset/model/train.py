"""Train and compare movement-prediction models on the NSE announcement corpus.

Target: `mover_1_5` — did the market-adjusted 30-minute move exceed 1.5%?
That is the question the bot actually asks at t0: is this filing worth a trade.

Only features knowable AT the announcement are used. Everything the label is
built from (px_1m..px_60m, ret_*, adj_*, day_*) is banned by assertion below —
one leaked price column turns a 0.63 AUC into a 0.99 AUC and a worthless model.
Outcomes of STRICTLY EARLIER announcements are fair game and live in the
`symbol`/`flow` groups; they are built with cumsum-minus-self so a row can never
see its own label.

Split is by time, never random, for the same reason baseline.py does it: label
windows overlap, so a random split leaks the future into training.

    python AIdataset/model/train.py                     # 6 models, same_session
    python AIdataset/model/train.py --session next_session
    python AIdataset/model/train.py --ablate            # which features earn it

Writes model_comparison.png, metrics.csv and the fitted LightGBM booster here.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
MASTER = ROOT.parent / "labels" / "nse_master.parquet"
TEST_FRACTION = 0.25  # most recent quarter of the timeline is held out
TARGET = "mover_1_5"

# Feature families, kept separate so --ablate can price each one.
GROUPS: dict[str, tuple[list[str], list[str]]] = {
    # What the filing is, who filed it, and when — all on the filing itself.
    "base": (["mcap_rank", "ai_sentiment_score", "ai_confidence", "px_t0_age_min",
              "log_mcap", "log_attachment", "log_px_pre", "log_vol_pre",
              "log_shared_by", "minute_of_day", "weekday", "headline_len"],
             ["category", "ai_event_type", "ai_sentiment", "ai_recommendation",
              "cap_tier", "ai_label_source"]),
    # How this stock has behaved on its OWN past filings. Pure prior, no news.
    "symbol": (["sym_prior_n", "sym_mover_rate", "sym_abs_move", "sym_rel_vol"], []),
    # Context around the filing: clustering, crowding, category track record.
    "flow": (["mins_since_prev", "sym_ann_today", "mkt_ann_bucket",
              "cat_mover_rate"], []),
    # The headline itself, compressed to one number by a TF-IDF linear model.
    "text": (["text_score"], []),
    # How volatile the stock has actually been lately, from its own candles.
    "vol": (["rv20", "range20"], []),
    # The filing's actual body text (fetch_pdfs.py), same treatment as headline.
    "pdf": (["pdf_score"], []),
    # The NUMBERS in that body. Bag-of-words cannot tell a 5-crore order from a
    # 5000-crore one; scale relative to the company is the whole story.
    "amount": (["amt_cr", "amt_pct_mcap", "n_amounts", "max_pct"], []),
    # The same numbers, but picked by DeepSeek instead of by regex — it is asked
    # for the MATERIAL amount, so it skips share capital and registration numbers.
    "llm": (["llm_amt_cr", "llm_pct_mcap", "llm_pct_change", "llm_quantified"],
            ["llm_kind"]),
}
AMOUNTS = ROOT / "amounts.jsonl"
PDF_TEXT = ROOT / "pdf_text.parquet"
ALL = tuple(g for g in GROUPS if g not in ("pdf", "amount", "llm"))  # need --pdf

# Raw columns read from the SAME row. These must be leak-free.
SOURCE_NOW = ["mcap_rank", "ai_sentiment_score", "ai_confidence", "px_t0_age_min",
              "market_cap_cr", "attachment_size", "px_pre", "vol_pre",
              "ai_label_shared_by", "anchor_time", "headline", "symbol",
              "category", "ai_event_type", "ai_sentiment", "ai_recommendation",
              "cap_tier", "ai_label_source"]
# Read only from strictly EARLIER rows (cumsum minus self). Deliberate.
SOURCE_LAGGED = [TARGET, "adj_30m"]


def cols(groups) -> tuple[list[str], list[str]]:
    num = [c for g in groups for c in GROUPS[g][0]]
    cat = [c for g in groups for c in GROUPS[g][1]]
    return num, cat


def leaky(columns) -> set[str]:
    """Columns that are only knowable after the trade would have been placed."""
    post = {"px_t0", "vol_t0", "day_high", "day_low", "day_volume",
            "usable", "mover_1_5", "mover_3", "disseminated_at"}
    for c in columns:
        if c.startswith(("ret_", "mkt_", "adj_")):
            post.add(c)
        if (c.startswith(("px_", "vol_")) and c.endswith("m")
                and c not in ("px_pre", "vol_pre", "px_t0_age_min")):
            post.add(c)
    return post


def attachment_kb(s: pd.Series) -> pd.Series:
    """'3.48 MB' / '118.37 KB' -> KB. A fat PDF is a substantive filing."""
    x = s.astype("string").str.extract(r"([\d.]+)\s*([KMG]?B)")
    return pd.to_numeric(x[0], errors="coerce") * x[1].map(
        {"B": 1 / 1024, "KB": 1.0, "MB": 1024.0, "GB": 1024.0 ** 2})


def prior_mean(values: pd.Series, by: pd.Series, n_prior: pd.Series) -> pd.Series:
    """Mean of `values` over strictly earlier rows in the same group.

    cumsum minus the row's own value: the row cannot see itself, and nothing
    later than it exists in the sum because the frame is time-sorted.
    """
    v = values.astype(float)
    total = v.groupby(by, sort=False).cumsum() - v.fillna(0)
    return (total / n_prior.where(n_prior > 0)).astype(float)


VOL_CACHE = ROOT / "sym_daily_vol.parquet"


def symbol_vol() -> pd.DataFrame:
    """Trailing 20-day realized vol and daily range per (symbol, date).

    The model's top features are all volatility PROXIES — price level, volume,
    market-cap rank. The stock's own candles measure the thing directly. Built
    once from stockdata/ (~30s over 12 GB), then cached.
    """
    if VOL_CACHE.exists():
        return pd.read_parquet(VOL_CACHE)
    out = []
    for f in sorted((ROOT.parent / "stockdata").glob("*.parquet")):
        x = pd.read_parquet(f, columns=["datetime", "high", "low", "close"])
        g = x.groupby(x.datetime.dt.date)
        day = pd.DataFrame({"high": g.high.max(), "low": g.low.min(),
                            "close": g.close.last()})
        out.append(pd.DataFrame({
            "symbol": f.stem, "date": day.index,
            # shift(1) = as of the PREVIOUS close. Without it the window covers
            # the announcement's own day and the feature leaks the reaction.
            "rv20": day.close.pct_change().rolling(20).std().shift(1).to_numpy() * 100,
            "range20": ((day.high - day.low) / day.close)
                       .rolling(20).mean().shift(1).to_numpy() * 100,
        }))
    d = pd.concat(out, ignore_index=True)
    assert d.groupby("symbol").rv20.head(20).isna().all(), "trailing vol is not lagged"
    d.to_parquet(VOL_CACHE, index=False)
    print(f"   built {VOL_CACHE.name}  {len(d):,} symbol-days")
    return d


def build(session: str):
    m = pd.read_parquet(MASTER)
    assert set(SOURCE_NOW).isdisjoint(leaky(m.columns)), "leaked outcome into features"

    d = m[m.usable.fillna(False) & m.anchor_time.notna()
          & (m.session_offset == session)].sort_values("anchor_time")
    print(f"{session}: {len(d):,} usable rows  "
          f"{d.anchor_time.min():%Y-%m-%d} -> {d.anchor_time.max():%Y-%m-%d}  "
          f"base rate {d[TARGET].mean():.2%}")

    at, sym = d.anchor_time, d.symbol
    n_prior = d.groupby(sym, sort=False).cumcount()
    log_vol_pre = np.log1p(d.vol_pre)

    X = pd.DataFrame({
        # --- base ---
        "mcap_rank": d.mcap_rank,
        "ai_sentiment_score": d.ai_sentiment_score,
        "ai_confidence": d.ai_confidence,
        "px_t0_age_min": d.px_t0_age_min,
        "log_mcap": np.log1p(d.market_cap_cr),
        "log_attachment": np.log1p(attachment_kb(d.attachment_size)),
        "log_px_pre": np.log1p(d.px_pre),
        "log_vol_pre": log_vol_pre,
        "log_shared_by": np.log1p(d.ai_label_shared_by),
        "minute_of_day": at.dt.hour * 60 + at.dt.minute,
        "weekday": at.dt.weekday,
        "headline_len": d.headline.fillna("").str.len(),
        # --- symbol: this stock's own track record, strictly past ---
        "sym_prior_n": n_prior,
        "sym_mover_rate": prior_mean(d[TARGET], sym, n_prior),
        "sym_abs_move": prior_mean(d.adj_30m.abs(), sym, n_prior),
        # today's pre-announcement volume against this stock's own norm
        "sym_rel_vol": log_vol_pre - prior_mean(log_vol_pre, sym, n_prior),
        # --- flow: clustering and crowding around the filing ---
        "mins_since_prev": at.groupby(sym, sort=False).diff().dt.total_seconds() / 60,
        "sym_ann_today": d.groupby([sym, at.dt.date], sort=False).cumcount(),
        # filings that already landed in this 15-minute slot, market-wide
        "mkt_ann_bucket": d.groupby(at.dt.floor("15min"), sort=False).cumcount(),
        "cat_mover_rate": prior_mean(
            d[TARGET], d.category, d.groupby(d.category, sort=False).cumcount()),
    }, index=d.index)
    v = pd.DataFrame({"symbol": d.symbol.to_numpy(), "date": at.dt.date.to_numpy()}
                     ).merge(symbol_vol(), on=["symbol", "date"], how="left")
    X["rv20"], X["range20"] = v.rv20.to_numpy(), v.range20.to_numpy()
    for c in GROUPS["base"][1]:
        X[c] = d[c].astype("string").fillna("NA").astype("category")

    # A symbol's first-ever filing has no history; if that is not NaN, the
    # cumsum is including the row itself and every lagged feature is leaking.
    assert X.loc[n_prior == 0, "sym_mover_rate"].isna().all(), "lagged stat sees itself"

    y = d[TARGET].astype(int)
    cut = int(len(d) * (1 - TEST_FRACTION))
    Xtr, Xte = X.iloc[:cut], X.iloc[cut:].copy()
    ytr, yte = y.iloc[:cut], y.iloc[cut:]
    # The whole point of a time split: no training row may postdate a test row.
    assert at.iloc[:cut].max() <= at.iloc[cut:].min(), "time split is not ordered"
    print(f"   train {len(Xtr):,}  ->  test {len(Xte):,} "
          f"(from {at.iloc[cut]:%Y-%m-%d}, {yte.mean():.2%} movers)")
    # Test-set categories unseen in training must not become new levels.
    for c in GROUPS["base"][1]:
        Xte[c] = Xte[c].cat.set_categories(Xtr[c].cat.categories)
    h, e = d.headline.fillna(""), d.event_id
    return (Xtr, Xte, ytr, yte, h.iloc[:cut], h.iloc[cut:],
            e.iloc[:cut], e.iloc[cut:])


def text_score(htr, hte, ytr, label="headline") -> tuple[np.ndarray, np.ndarray]:
    """Headline -> P(mover), as one column a tree can split on.

    Trees cannot use 50k sparse n-gram columns; a linear model can. Its
    out-of-fold prediction on train (so the trees never see a fitted-on-itself
    number) and its full-train prediction on test become the feature.
    """
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import cross_val_predict

    vec = TfidfVectorizer(min_df=20, ngram_range=(1, 2), sublinear_tf=True,
                          strip_accents="unicode", max_features=50_000)
    Ztr, Zte = vec.fit_transform(htr), vec.transform(hte)
    lr = LogisticRegression(max_iter=1000, C=1.0)
    # ponytail: plain 5-fold OOF, not purged/time-series — the inflation stays
    # inside train, the test column is honest. Purge it if train AUC matters.
    oof = cross_val_predict(lr, Ztr, ytr, cv=5, method="predict_proba", n_jobs=-1)
    lr.fit(Ztr, ytr)
    print(f"   {label} TF-IDF: {Ztr.shape[1]:,} n-grams")
    return oof[:, 1], lr.predict_proba(Zte)[:, 1]


# "Rs. 1,234.56 crore" / "₹450 Cr" / "12.5 million". Everything -> crore.
_AMOUNT = re.compile(
    r"(?:rs\.?|inr|₹)?\s*([\d][\d,]*(?:\.\d+)?)\s*"
    r"(crore|crores|cr|lakh|lakhs|lac|million|mn|billion|bn)\b", re.I)
_PCT = re.compile(r"([\d]+(?:\.\d+)?)\s*(?:%|per\s*cent)", re.I)
_TO_CRORE = {"crore": 1.0, "crores": 1.0, "cr": 1.0, "lakh": 0.01, "lakhs": 0.01,
             "lac": 0.01, "million": 0.1, "mn": 0.1, "billion": 100.0, "bn": 100.0}


def pdf_amounts(text: pd.Series, mcap: np.ndarray) -> pd.DataFrame:
    """Largest rupee figure in the filing, and what it is worth as a share of
    the company. An order win is noise; an order worth 18% of market cap is not."""
    amt, n_amt, pct = [], [], []
    for t in text:
        found = [float(v.replace(",", "")) * _TO_CRORE[u.lower()]
                 for v, u in _AMOUNT.findall(t or "")]
        amt.append(max(found) if found else np.nan)
        n_amt.append(len(found))
        pcts = [float(v) for v in _PCT.findall(t or "")]
        # Filings quote "51% stake" but also "18.5% of paid-up capital"; the
        # cap drops page numbers and stray year fragments read as percentages.
        pct.append(max([p for p in pcts if p <= 100], default=np.nan))
    amt = np.array(amt, dtype=float)
    return pd.DataFrame({
        "amt_cr": np.log1p(amt),
        "amt_pct_mcap": np.where(mcap > 0, amt / mcap * 100, np.nan),
        "n_amounts": n_amt,
        "max_pct": pct,
    })


def llm_amounts(eids: pd.Series, mcap: np.ndarray) -> pd.DataFrame:
    """DeepSeek's extraction (extract_amounts.py), aligned to the given rows."""
    r = pd.read_json(AMOUNTS, lines=True).drop_duplicates("event_id", keep="last")
    x = r.set_index("event_id").reindex(eids.to_numpy())
    amt = pd.to_numeric(x.get("amount_cr"), errors="coerce").to_numpy()
    return pd.DataFrame({
        "llm_amt_cr": np.log1p(amt),
        # The thesis in one column: a deal is only big relative to the company.
        "llm_pct_mcap": np.where(mcap > 0, amt / mcap * 100, np.nan),
        "llm_pct_change": pd.to_numeric(x.get("pct_change"), errors="coerce").to_numpy(),
        "llm_quantified": x.get("quantified").fillna(False).astype(float).to_numpy(),
        "llm_kind": x.get("amount_kind").astype("string").fillna("NA").to_numpy(),
    })


def models(num: list[str], cat: list[str]):
    from lightgbm import LGBMClassifier
    from sklearn.compose import ColumnTransformer
    from sklearn.dummy import DummyClassifier
    from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import OneHotEncoder, OrdinalEncoder, StandardScaler
    from xgboost import XGBClassifier

    def prep(cat_encoder):
        return ColumnTransformer([
            ("num", make_pipeline(SimpleImputer(strategy="median"), StandardScaler()), num),
            ("cat", cat_encoder, cat),
        ])

    from sklearn.ensemble import VotingClassifier

    onehot = prep(OneHotEncoder(handle_unknown="ignore", min_frequency=20))
    ordinal = prep(OrdinalEncoder(handle_unknown="use_encoded_value", unknown_value=-1))

    trees = {
        # The floor: predict the base rate for everything. Any model that cannot
        # beat this on AUC has learned nothing, however good its accuracy looks.
        "Baseline (prior)": make_pipeline(ordinal, DummyClassifier(strategy="prior")),
        "Logistic Regression": make_pipeline(onehot, LogisticRegression(max_iter=2000)),
        "Random Forest": make_pipeline(
            ordinal, RandomForestClassifier(n_estimators=300, min_samples_leaf=20,
                                            n_jobs=-1, random_state=0)),
        "HistGradientBoosting": HistGradientBoostingClassifier(
            categorical_features="from_dtype", random_state=0),
        "XGBoost": XGBClassifier(n_estimators=400, learning_rate=0.05, max_depth=6,
                                 subsample=0.8, colsample_bytree=0.8,
                                 enable_categorical=True, tree_method="hist",
                                 eval_metric="logloss", n_jobs=-1, random_state=0),
        "LightGBM": LGBMClassifier(n_estimators=400, learning_rate=0.05,
                                   num_leaves=63, min_child_samples=50,
                                   subsample=0.8, colsample_bytree=0.8,
                                   importance_type="gain",
                                   n_jobs=-1, random_state=0, verbose=-1),
    }
    # Four rankers that make different mistakes; averaging their probabilities
    # is the cheapest gain left on the table.
    return trees | {"Soft-vote ensemble": VotingClassifier(
        [(k, v) for k, v in trees.items() if k not in
         ("Baseline (prior)", "Logistic Regression")], voting="soft")}


def best_threshold(y, p) -> float:
    """The cutoff that maximises accuracy. 0.5 is a convention, not an optimum —
    at a 10% base rate the accuracy-optimal cut sits well below it."""
    from sklearn.metrics import roc_curve
    n_pos, n_neg = y.sum(), len(y) - y.sum()
    fpr, tpr, thr = roc_curve(y, p)
    acc = (tpr * n_pos + (1 - fpr) * n_neg) / len(y)
    return float(thr[acc.argmax()])


def evaluate(name, est, Xtr, Xte, ytr, yte, val: float = 0.2) -> dict:
    from sklearn.metrics import (accuracy_score, average_precision_score,
                                 balanced_accuracy_score, f1_score, roc_auc_score,
                                 roc_curve)
    t = time.time()
    # Tune the cutoff on the tail of TRAIN, never on test — otherwise the
    # accuracy you report is the accuracy you fitted to.
    cut = int(len(Xtr) * (1 - val))
    est.fit(Xtr.iloc[:cut], ytr.iloc[:cut])
    thr = best_threshold(ytr.iloc[cut:], est.predict_proba(Xtr.iloc[cut:])[:, 1])
    est.fit(Xtr, ytr)
    fit_s = time.time() - t
    p = est.predict_proba(Xte)[:, 1]
    pred = (p >= thr).astype(int)
    base = yte.mean()
    # Trading-relevant: of the 10% of filings the model likes most, how many
    # more movers are there than in a random 10%? AUC does not tell you this.
    top = p >= np.quantile(p, 0.9)
    fpr, tpr, _ = roc_curve(yte, p)
    return {
        "model": name, "accuracy": accuracy_score(yte, pred),
        "balanced_accuracy": balanced_accuracy_score(yte, pred),
        "roc_auc": roc_auc_score(yte, p), "pr_auc": average_precision_score(yte, p),
        "f1": f1_score(yte, pred, zero_division=0),
        "top_decile_lift": yte[top].mean() / base if base else np.nan,
        "threshold": thr, "fit_seconds": fit_s, "_roc": (fpr, tpr), "_est": est,
    }


# Light surface + categorical hues, in fixed slot order (dataviz reference palette).
INK, INK2, SURFACE, GRID = "#0b0b0b", "#52514e", "#fcfcfb", "#d8d7d2"
HUES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7"]
ACCENT, MUTED = "#2a78d6", "#b8b7b1"


def style():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
                         "text.color": INK, "axes.labelcolor": INK2,
                         "xtick.color": INK2, "ytick.color": INK2,
                         "axes.edgecolor": GRID, "font.size": 9})
    return plt


def frame(a, names, title):
    a.set_yticks(np.arange(len(names))[::-1], names)
    a.set_title(title, loc="left", weight="bold", pad=8)
    for s in ("top", "right", "left"):
        a.spines[s].set_visible(False)
    a.grid(axis="x", color=GRID, lw=0.6)
    a.set_axisbelow(True)


def plot(res: list[dict], base: float, booster, session: str, out: Path) -> None:
    plt = style()
    fig, ax = plt.subplots(2, 2, figsize=(13, 9))
    fig.suptitle(f"Will this filing move the stock >1.5%?  —  {session}, "
                 f"held-out test set", fontsize=13, weight="bold", y=0.98)
    names = [r["model"] for r in res]
    ypos = np.arange(len(names))[::-1]

    # Accuracy is a trap at a 10% base rate: predicting "no move" for every filing
    # already scores 1 - base, so all six land on top of each other. Plotted as
    # dots, not bars, because the axis is zoomed. Balanced accuracy — plotted on
    # the same axis — is where the models actually separate.
    a = ax[0, 0]
    a.axvline(1 - base, color=INK2, ls="--", lw=1)
    a.text(1 - base, len(names) - 0.15, f" always-'no move'\n = {1-base:.1%}",
           fontsize=8, color=INK2, ha="right", va="top")
    a.axvline(0.5, color=INK2, ls=":", lw=1)
    a.text(0.5, len(names) - 0.15, " coin flip", fontsize=8, color=INK2, va="top")
    for vals, hue, lbl in (([r["accuracy"] for r in res], ACCENT, "accuracy"),
                           ([r["balanced_accuracy"] for r in res], HUES[1],
                            "balanced accuracy")):
        a.scatter(vals, ypos, s=80, color=hue, zorder=3,
                  edgecolor=SURFACE, linewidth=1.5, label=lbl)
    for y, v in zip(ypos, [r["balanced_accuracy"] for r in res]):
        a.text(v, y - 0.32, f"{v:.3f}", ha="center", fontsize=8, color=INK)
    a.set_xlim(0.44, 1.0)
    a.set_ylim(-1.5, len(names) - 0.1)
    a.legend(fontsize=8, frameon=False, loc="lower center", ncol=2)
    frame(a, names, "Accuracy at the accuracy-optimal threshold (tuned on train)")

    a = ax[0, 1]
    vals = [r["roc_auc"] for r in res]
    best = names[int(np.argmax(vals))]
    a.barh(ypos, vals, height=0.62,
           color=[ACCENT if n == best else MUTED for n in names])
    for y, v in zip(ypos, vals):
        a.text(v, y, f"  {v:.3f}", va="center", fontsize=8, color=INK)
    a.axvline(0.5, color=INK2, ls="--", lw=1)
    a.text(0.5, len(names) - 0.4, " chance", fontsize=8, color=INK2)
    a.set_xlim(0, 1.0)
    frame(a, names, "ROC-AUC  (ranking skill — 0.5 is a coin flip)")

    a = ax[1, 0]
    # The prior baseline's ROC *is* the diagonal; drawing it twice adds nothing.
    curves = [r for r in res if r["model"] != "Baseline (prior)"]
    hues = {r["model"]: h for r, h in zip(curves, HUES[1:])} | {"LightGBM": ACCENT}
    for r in curves:
        fpr, tpr = r["_roc"]
        a.plot(fpr, tpr, color=hues[r["model"]], lw=2,
               label=f"{r['model']} ({r['roc_auc']:.3f})")
    a.plot([0, 1], [0, 1], color=INK2, ls="--", lw=1, label="chance / baseline")
    a.set_title("ROC curves", loc="left", weight="bold", pad=8)
    a.set_xlabel("false positive rate"), a.set_ylabel("true positive rate")
    a.legend(fontsize=8, frameon=False, loc="lower right")
    for s in ("top", "right"):
        a.spines[s].set_visible(False)
    a.grid(color=GRID, lw=0.6), a.set_axisbelow(True)

    a = ax[1, 1]
    imp = pd.Series(booster.feature_importances_,
                    index=booster.feature_name_).nlargest(12)[::-1]
    a.barh(np.arange(len(imp)), imp.values, color=ACCENT, height=0.62)
    a.set_yticks(np.arange(len(imp)), imp.index)
    a.set_title("What LightGBM leans on (total split gain)", loc="left",
                weight="bold", pad=8)
    for s in ("top", "right", "left"):
        a.spines[s].set_visible(False)
    a.grid(axis="x", color=GRID, lw=0.6), a.set_axisbelow(True)

    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(out, dpi=150)
    print(f"\nwrote {out}")


def plot_ablation(rows: list[dict], base: float, session: str, out: Path) -> None:
    plt = style()
    fig, ax = plt.subplots(1, 2, figsize=(13, 5))
    fig.suptitle(f"Which features earn their place?  —  {session}, LightGBM, "
                 f"held-out test set", fontsize=13, weight="bold", y=0.99)
    names = [r["model"] for r in rows]
    ypos = np.arange(len(names))[::-1]
    top = names[-1]

    for a, key, title, fmt, ref, reflabel in (
            (ax[0], "roc_auc", "ROC-AUC", "  {:.4f}", 0.5, " chance"),
            (ax[1], "top_decile_lift",
             "Top-decile lift  (movers in the model's favourite 10%)",
             "  {:.2f}x", 1.0, " no better than random")):
        vals = [r[key] for r in rows]
        a.barh(ypos, vals, height=0.62,
               color=[ACCENT if n == top else MUTED for n in names])
        for y, v in zip(ypos, vals):
            a.text(v, y, fmt.format(v), va="center", fontsize=8, color=INK)
        a.axvline(ref, color=INK2, ls="--", lw=1)
        a.text(ref, len(names) - 0.45, reflabel, fontsize=8, color=INK2)
        a.set_xlim(0, max(vals) * 1.2)
        frame(a, names, title)

    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(out, dpi=150)
    print(f"\nwrote {out}")


LADDER = [
    ("symbol history only", ("symbol",)),
    ("base (v1 features)", ("base",)),
    ("+ symbol history", ("base", "symbol")),
    ("+ news flow", ("base", "symbol", "flow")),
    ("+ headline text", ("base", "symbol", "flow", "text")),
    ("+ realized vol", ALL),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="same_session",
                    choices=["same_session", "next_session", "same_day_preopen"])
    ap.add_argument("--ablate", action="store_true",
                    help="price each feature family instead of comparing models")
    ap.add_argument("--pdf", action="store_true",
                    help="restrict to filings whose PDF body was fetched, and A/B it")
    a = ap.parse_args()

    Xtr, Xte, ytr, yte, htr, hte, etr, ete = build(a.session)
    Xtr = Xtr.copy()
    Xtr["text_score"], Xte["text_score"] = text_score(htr, hte, ytr)

    ladder = LADDER
    if a.pdf:
        p = pd.read_parquet(PDF_TEXT)
        body = p[p.ok.fillna(False).astype(bool)].set_index("event_id").text
        # The A/B has to run on identical rows, so drop everything without a
        # body from BOTH arms — otherwise "with PDF" is also "on easier rows".
        mtr, mte = etr.isin(body.index).to_numpy(), ete.isin(body.index).to_numpy()
        Xtr, ytr, Xte, yte = Xtr[mtr], ytr[mtr], Xte[mte].copy(), yte[mte]
        print(f"   PDF bodies: train {mtr.sum():,}  test {mte.sum():,} "
              f"({yte.mean():.2%} movers)")
        btr, bte = body.loc[etr[mtr]], body.loc[ete[mte]]
        Xtr["pdf_score"], Xte["pdf_score"] = text_score(btr, bte, ytr, label="PDF body")
        for X, b in ((Xtr, btr), (Xte, bte)):
            a4 = pdf_amounts(b, np.expm1(X.log_mcap.to_numpy()))
            X[a4.columns] = a4.to_numpy()
        got = Xtr.amt_cr.notna().mean()
        print(f"   rupee figure found in {got:.0%} of filings; "
              f"median size {np.expm1(Xtr.amt_cr).median():,.0f} cr")
        for X, e in ((Xtr, etr[mtr]), (Xte, ete[mte])):
            l5 = llm_amounts(e, np.expm1(X.log_mcap.to_numpy()))
            for c in l5.columns:
                X[c] = l5[c].to_numpy()
        Xtr["llm_kind"] = Xtr.llm_kind.astype("category")
        Xte["llm_kind"] = Xte.llm_kind.astype("category").cat.set_categories(
            Xtr.llm_kind.cat.categories)
        print(f"   LLM found a material amount in {Xtr.llm_amt_cr.notna().mean():.0%} "
              f"of filings (regex: {Xtr.amt_cr.notna().mean():.0%})")
        ladder = [("everything except the PDF", ALL),
                  ("+ PDF body text", ALL + ("pdf",)),
                  ("+ regex numbers", ALL + ("amount",)),
                  ("+ LLM-extracted numbers", ALL + ("llm",)),
                  ("+ LLM numbers & PDF text", ALL + ("pdf", "llm"))]
    print()

    if a.ablate:
        rows = []
        for label, groups in ladder:
            num, cat = cols(groups)
            est = models(num, cat)["LightGBM"]
            r = evaluate(label, est, Xtr[num + cat], Xte[num + cat], ytr, yte)
            rows.append(r)
            print(f"   {label:<22} AUC {r['roc_auc']:.4f}  PR-AUC {r['pr_auc']:.4f}"
                  f"  lift {r['top_decile_lift']:.2f}x  ({len(num+cat)} features)")
        tag = f"{a.session}_pdf" if a.pdf else a.session
        pd.DataFrame(rows).drop(columns=["_roc", "_est"]).to_csv(
            ROOT / f"ablation_{tag}.csv", index=False)
        plot_ablation(rows, yte.mean(), a.session, ROOT / f"ablation_{tag}.png")
        return 0

    num, cat = cols(ALL + ("pdf", "amount", "llm") if a.pdf else ALL)
    res = []
    for name, est in models(num, cat).items():
        r = evaluate(name, est, Xtr[num + cat], Xte[num + cat], ytr, yte)
        res.append(r)
        print(f"   {name:<22} acc {r['accuracy']:.1%}  bal-acc {r['balanced_accuracy']:.3f}"
              f"  AUC {r['roc_auc']:.4f}  PR-AUC {r['pr_auc']:.4f}"
              f"  lift {r['top_decile_lift']:.2f}x  ({r['fit_seconds']:.0f}s)")

    table = pd.DataFrame(res).drop(columns=["_roc", "_est"])
    table.to_csv(ROOT / f"metrics_{a.session}.csv", index=False)
    lgbm = next(r for r in res if r["model"] == "LightGBM")["_est"]
    print(f"\nbest AUC: {table.loc[table.roc_auc.idxmax(), 'model']}"
          f" ({table.roc_auc.max():.4f})")
    imp = pd.Series(lgbm.feature_importances_, index=lgbm.feature_name_).nlargest(6)
    print("LightGBM leans on: " + ", ".join(f"{k} {v/imp.sum():.0%}" for k, v in imp.items()))

    lgbm.booster_.save_model(str(ROOT / f"lgbm_{a.session}.txt"))
    plot(res, yte.mean(), lgbm, a.session, ROOT / f"model_comparison_{a.session}.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
