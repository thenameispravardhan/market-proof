"""Meta-labeling: a secondary model that decides WHETHER and HOW MUCH to
trade a side the primary model (the LLM) has already picked.

Why this framing. The earlier "mover" models asked an unconditional
question (will this filing move?) and found under 1 pp of headroom over
the base rate. The LLM's verbalized confidence measured anti-predictive,
so it cannot gate trades either. Meta-labeling asks the conditional,
P&L-shaped question instead: given the side the primary chose, at this
time, with these stops, does the trade end positive AFTER costs? The label
comes from the cost-aware replay (app/research/replay.py), which applies
the bot's own stop/target/time exits. Those exits are the triple barrier.

Pipeline (all pure Python, so it runs on the 2 GB server without sklearn):

  1. features knowable at entry time only (event type, side, how the side
     was chosen, verbalized confidence as ONE feature among many, the move
     already realised between filing and entry, time of day, price level,
     size tier, optional numeric-surprise and India VIX columns);
  2. chronological split: fit / calibrate / test (no shuffling: overlapping
     windows and regime drift make a random split optimistic);
  3. L2 logistic regression, then isotonic calibration fitted on the LATER
     calibration slice;
  4. evaluation on the test slice: AUC, ECE raw vs calibrated, and the
     trading question: expectancy of the trades the model keeps vs all
     trades, with bootstrap CIs;
  5. fractional-Kelly risk multipliers per calibrated-probability bucket,
     reported only. Sizing on a probability is defensible only once that
     probability is calibrated, so nothing here changes live sizing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Sequence

from app.research import stats
from app.research.replay import ReplayTrade

# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------


def _minute_of_day(iso: str) -> float:
    t = datetime.fromisoformat(iso)
    return float(t.hour * 60 + t.minute)


def trade_features(t: ReplayTrade, extra: Optional[dict[str, float]] = None) -> dict[str, float]:
    """Decision-time features of one replayed trade. Nothing derived from
    the exit, the P&L or any price after entry may appear here."""
    f: dict[str, float] = {
        "side_long": 1.0 if t.side > 0 else 0.0,
        "confidence": float(t.confidence) if t.confidence is not None else float("nan"),
        "pre_move_pct": float(t.pre_move_pct) if t.pre_move_pct is not None else float("nan"),
        "minute_of_day": _minute_of_day(t.entry_at),
        "log_price": math.log(max(t.entry, 0.01)),
        "stop_pct": abs(t.entry - t.stop) / t.entry * 100.0 if t.entry else float("nan"),
        f"event={t.event_type or 'UNKNOWN'}": 1.0,
        f"src={t.direction_source}": 1.0,
    }
    for k, v in (extra or {}).items():
        f[k] = float(v) if v is not None else float("nan")
    return f


LEAK_PREFIXES = ("exit", "net_", "gross", "r_multiple", "post_move", "charges")


@dataclass
class Design:
    names: list[str]
    means: list[float]
    stds: list[float]

    @classmethod
    def fit(cls, rows: Sequence[dict[str, float]]) -> "Design":
        names = sorted({k for r in rows for k in r})
        assert not any(n.startswith(LEAK_PREFIXES) for n in names), "outcome leaked into features"
        means, stds = [], []
        for n in names:
            vals = [r.get(n, 0.0) for r in rows]
            vals = [v for v in vals if not math.isnan(v)]
            m = sum(vals) / len(vals) if vals else 0.0
            sd = math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals)) if len(vals) > 1 else 1.0
            means.append(m)
            stds.append(sd if sd > 1e-9 else 1.0)
        return cls(names, means, stds)

    def matrix(self, rows: Sequence[dict[str, float]]) -> list[list[float]]:
        out = []
        for r in rows:
            row = []
            for n, m, sd in zip(self.names, self.means, self.stds):
                v = r.get(n, 0.0)
                row.append(0.0 if math.isnan(v) else (v - m) / sd)   # missing -> training mean
            out.append(row)
        return out


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


@dataclass
class Logistic:
    weights: list[float] = field(default_factory=list)
    bias: float = 0.0

    def fit(self, X: list[list[float]], y: list[int], *, l2: float = 1.0, lr: float = 0.1,
            epochs: int = 400, class_weight: bool = True) -> "Logistic":
        n, d = len(X), len(X[0]) if X else 0
        self.weights, self.bias = [0.0] * d, 0.0
        pos = sum(y)
        w_pos = (n / (2 * pos)) if class_weight and pos else 1.0
        w_neg = (n / (2 * (n - pos))) if class_weight and n - pos else 1.0
        for _ in range(epochs):
            gw = [0.0] * d
            gb = 0.0
            for xi, yi in zip(X, y):
                p = _sigmoid(self.bias + sum(w * x for w, x in zip(self.weights, xi)))
                err = (p - yi) * (w_pos if yi else w_neg)
                gb += err
                for j in range(d):
                    gw[j] += err * xi[j]
            self.bias -= lr * gb / n
            self.weights = [w - lr * (g / n + l2 * w / n) for w, g in zip(self.weights, gw)]
        return self

    def predict(self, X: list[list[float]]) -> list[float]:
        return [_sigmoid(self.bias + sum(w * x for w, x in zip(self.weights, xi))) for xi in X]


@dataclass
class Isotonic:
    """Pool-adjacent-violators isotonic regression (increasing)."""
    xs: list[float] = field(default_factory=list)
    ys: list[float] = field(default_factory=list)

    def fit(self, p: Sequence[float], y: Sequence[int]) -> "Isotonic":
        pts = sorted(zip(p, y))
        blocks: list[list[float]] = []          # [sum_y, count, min_x, max_x]
        for x, t in pts:
            blocks.append([float(t), 1.0, x, x])
            while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] >= blocks[-1][0] / blocks[-1][1]:
                s, c, lo, hi = blocks.pop()
                blocks[-1][0] += s
                blocks[-1][1] += c
                blocks[-1][3] = hi
        self.xs = [(b[2] + b[3]) / 2 for b in blocks]
        self.ys = [b[0] / b[1] for b in blocks]
        return self

    def predict(self, p: Sequence[float]) -> list[float]:
        out = []
        for x in p:
            if not self.xs:
                out.append(x)
            elif x <= self.xs[0]:
                out.append(self.ys[0])
            elif x >= self.xs[-1]:
                out.append(self.ys[-1])
            else:
                j = next(i for i in range(1, len(self.xs)) if self.xs[i] >= x)
                x0, x1, y0, y1 = self.xs[j - 1], self.xs[j], self.ys[j - 1], self.ys[j]
                out.append(y0 + (y1 - y0) * (x - x0) / (x1 - x0) if x1 > x0 else y1)
        return out


# ---------------------------------------------------------------------------
# Kelly
# ---------------------------------------------------------------------------


def kelly_fraction(p: float, payoff_ratio: float) -> float:
    """Full-Kelly fraction for a binary bet: win prob `p`, win/loss size
    ratio `payoff_ratio` (avg win R / avg loss R). Never negative."""
    if payoff_ratio <= 0:
        return 0.0
    return max(0.0, p - (1 - p) / payoff_ratio)


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


def run(trades: Sequence[ReplayTrade], *, extra: Optional[dict[Any, dict[str, float]]] = None,
        threshold: Optional[float] = None, kelly_scale: float = 0.25,
        min_trades: int = 150) -> dict[str, Any]:
    """Fit, calibrate and evaluate on a chronological 60/20/20 split.

    `extra` maps signal_id -> additional decision-time features (numeric
    surprise from the results parser, India VIX at entry, ...)."""
    ts = sorted(trades, key=lambda t: t.entry_at)
    if len(ts) < min_trades:
        return {"ok": False, "reason": f"need at least {min_trades} replayed trades, have {len(ts)}"}
    n = len(ts)
    a, b = int(n * 0.6), int(n * 0.8)
    fit_t, cal_t, test_t = ts[:a], ts[a:b], ts[b:]
    feats = lambda group: [trade_features(t, (extra or {}).get(t.signal_id)) for t in group]  # noqa: E731
    label = lambda group: [1 if t.r_multiple > 0 else 0 for t in group]  # noqa: E731

    design = Design.fit(feats(fit_t))
    y_fit, y_cal, y_test = label(fit_t), label(cal_t), label(test_t)
    if len(set(y_fit)) < 2 or len(set(y_cal)) < 2 or len(set(y_test)) < 2:
        return {"ok": False, "reason": "a split has only one outcome class"}
    model = Logistic().fit(design.matrix(feats(fit_t)), y_fit)
    iso = Isotonic().fit(model.predict(design.matrix(feats(cal_t))), y_cal)
    raw = model.predict(design.matrix(feats(test_t)))
    cal = iso.predict(raw)

    # The primary model's own confidence as the comparison: does the meta
    # model rank winners better than "trust the LLM's number"?
    conf = [t.confidence if t.confidence is not None else 0.5 for t in test_t]
    auc_cmp = stats.paired_auc_difference(conf, raw, y_test)

    # Threshold: keep the trades whose calibrated P(win) beats the base rate
    # of the CALIBRATION slice unless one is given. Chosen before test.
    thr = threshold if threshold is not None else sum(y_cal) / len(y_cal)
    kept = [t for t, p in zip(test_t, cal) if p >= thr]
    wins = [t.r_multiple for t in fit_t if t.r_multiple > 0]
    losses = [-t.r_multiple for t in fit_t if t.r_multiple <= 0]
    payoff = (sum(wins) / len(wins)) / (sum(losses) / len(losses)) if wins and losses and sum(losses) else 0.0

    # Risk multiplier relative to the base risk: Kelly at the bucket's
    # calibrated probability over Kelly at the base win rate. 1.0 = today's
    # size, 0 = no edge in that bucket, capped at 1.5x.
    k_base = kelly_fraction(sum(y_fit) / len(y_fit), payoff)
    kelly_rows = []
    for lo, hi in ((0.0, 0.3), (0.3, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 1.01)):
        sel = [(t, p) for t, p in zip(test_t, cal) if lo <= p < hi]
        k_mid = kelly_fraction((lo + min(hi, 1.0)) / 2, payoff)
        kelly_rows.append({
            "p_bucket": f"{lo:.1f}-{min(hi, 1.0):.1f}", "n_test": len(sel),
            "observed_win_rate": round(sum(1 for t, _ in sel if t.r_multiple > 0) / len(sel), 4) if sel else None,
            "fractional_kelly": round(kelly_scale * k_mid, 4),
            "risk_multiplier_vs_base": (round(min(1.5, k_mid / k_base), 3) if k_base > 0 else None),
        })

    weights = sorted(zip(design.names, model.weights), key=lambda kv: -abs(kv[1]))[:12]
    return {
        "ok": True,
        "split": {"fit": len(fit_t), "calibrate": len(cal_t), "test": len(test_t),
                  "test_from": test_t[0].entry_at},
        "base_win_rate": {"fit": round(sum(y_fit) / len(y_fit), 4), "test": round(sum(y_test) / len(y_test), 4)},
        "test_auc_meta": auc_cmp["auc_b"],
        "test_auc_llm_confidence": auc_cmp["auc_a"],
        "auc_gain_over_confidence": auc_cmp,
        "ece_raw": stats.ece(raw, y_test)["ece"],
        "ece_calibrated": stats.ece(cal, y_test)["ece"],
        "threshold": round(thr, 4),
        "all_test_trades": stats.summarize_r([t.r_multiple for t in test_t], [t.net_pnl for t in test_t]),
        "kept_by_meta_model": stats.summarize_r([t.r_multiple for t in kept], [t.net_pnl for t in kept]),
        "payoff_ratio": round(payoff, 4),
        "kelly": {"scale": kelly_scale, "by_probability": kelly_rows,
                  "note": "reported only; live sizing is unchanged"},
        "top_weights": [{"feature": k, "weight": round(w, 4)} for k, w in weights],
    }
