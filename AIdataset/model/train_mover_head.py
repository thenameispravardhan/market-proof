"""Discriminative `mover` scorer: an LM encoder + market features + a
classification head, trained with a class-weighted (or focal) loss.

Why this and not more SFT. v1 fine-tuned Qwen2.5-1.5B with next-token loss
on JSON targets that were ~70% FLAT; the generator collapsed to FLAT 94.2%
of the time, and its embedding only MATCHED TF-IDF (paired CI spanned
zero). Next-token loss optimises the format, not "does this move the
stock". This script optimises the one target the corpus was shown to be
learnable on (`mover`), directly:

    filing text --LM--> pooled hidden state --+
                                              +--> MLP --> P(mover)
    market features (standardised) -----------+

* one forward pass per filing at inference (no generation), so on the DGX
  Spark it scores in well under a second;
* class weights or focal loss, so the 70/30 imbalance cannot be "solved"
  by predicting the majority;
* chronological splits only, read from the data, never shuffled across time;
* the pass/fail bar is explicit: beat the TF-IDF+SVD+market baseline
  (~0.7646 AUC on the test filings) with a PAIRED bootstrap CI clear of
  zero. If you pass `--baseline-preds`, the script computes that test and
  says which side of the bar the run lands on.

Input: one parquet or JSONL file with columns

    id            unique filing id (joins to --baseline-preds)
    text          what the model reads (filing text or the SFT user prompt)
    mover         0/1 label
    split         train | val | test     (chronological, prepared upstream)
    <market cols> numeric, named with --market-cols (NaN allowed)

Run on the Spark (NGC PyTorch container, BF16, SDPA attention, no
flash-attn wheel needed):

    python AIdataset/model/train_mover_head.py --data mover.parquet \\
        --model Qwen/Qwen2.5-1.5B --market-cols log_mcap,rv20,log_vol_pre,minute_of_day \\
        --epochs 2 --batch 16 --max-len 1024 --loss focal --bf16 \\
        --baseline-preds tfidf_market_test.csv --out runs/mover_head_v1

Smoke test anywhere: `--model tiny` builds a tiny random Qwen2 so the
whole pipeline runs on a laptop CPU in seconds.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from torch import nn

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[1]))  # app.research.stats is shared with the bot
from app.research import stats  # noqa: E402

BASELINE_AUC = 0.7646   # TF-IDF + SVD + market, test filings (SLM plan)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


def load(path: str) -> pd.DataFrame:
    df = pd.read_parquet(path) if path.endswith(".parquet") else pd.read_json(path, lines=True)
    missing = {"id", "text", "mover", "split"} - set(df.columns)
    if missing:
        raise SystemExit(f"input is missing columns: {sorted(missing)}")
    bad = set(df.split.unique()) - {"train", "val", "test"}
    if bad:
        raise SystemExit(f"unknown split values: {sorted(bad)}")
    return df


class MarketScaler:
    """Standardise with TRAIN statistics only; NaN -> 0 (the train mean) plus
    an is-missing indicator per column, so absence is information, not noise."""

    def __init__(self, cols: list[str]) -> None:
        self.cols = cols
        self.mean: dict[str, float] = {}
        self.std: dict[str, float] = {}

    def fit(self, df: pd.DataFrame) -> "MarketScaler":
        for c in self.cols:
            v = pd.to_numeric(df[c], errors="coerce")
            self.mean[c] = float(v.mean()) if v.notna().any() else 0.0
            sd = float(v.std()) if v.notna().sum() > 1 else 1.0
            self.std[c] = sd if sd > 1e-9 else 1.0
        return self

    @property
    def dim(self) -> int:
        return 2 * len(self.cols)

    def transform(self, df: pd.DataFrame) -> np.ndarray:
        out = []
        for c in self.cols:
            v = pd.to_numeric(df[c], errors="coerce").to_numpy(dtype=float)
            miss = np.isnan(v)
            out.append(np.where(miss, 0.0, (v - self.mean[c]) / self.std[c]))
            out.append(miss.astype(float))
        return np.stack(out, axis=1).astype(np.float32) if out else np.zeros((len(df), 0), np.float32)


class Batches(torch.utils.data.Dataset):
    def __init__(self, enc: dict, market: np.ndarray, labels: np.ndarray) -> None:
        self.enc, self.market, self.labels = enc, market, labels

    def __len__(self) -> int:
        return len(self.labels)

    def __getitem__(self, i: int) -> dict:
        item = {k: torch.tensor(v[i]) for k, v in self.enc.items()}
        item["market"] = torch.tensor(self.market[i])
        item["labels"] = torch.tensor(int(self.labels[i]))
        return item


def collate(batch: list[dict], pad_id: int) -> dict:
    width = max(len(b["input_ids"]) for b in batch)
    ids = torch.full((len(batch), width), pad_id, dtype=torch.long)
    mask = torch.zeros((len(batch), width), dtype=torch.long)
    for i, b in enumerate(batch):
        n = len(b["input_ids"])
        ids[i, :n], mask[i, :n] = b["input_ids"], 1    # right padding
    return {"input_ids": ids, "attention_mask": mask,
            "market": torch.stack([b["market"] for b in batch]),
            "labels": torch.stack([b["labels"] for b in batch])}


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class MoverHead(nn.Module):
    def __init__(self, encoder: nn.Module, hidden: int, market_dim: int, *, loss: str,
                 pos_weight: float, focal_gamma: float = 2.0) -> None:
        super().__init__()
        self.encoder = encoder
        self.head = nn.Sequential(
            nn.Linear(hidden + market_dim, 256), nn.GELU(), nn.Dropout(0.1), nn.Linear(256, 1))
        self.loss_kind, self.pos_weight, self.gamma = loss, pos_weight, focal_gamma

    def forward(self, input_ids, attention_mask, market, labels=None):
        out = self.encoder(input_ids=input_ids, attention_mask=attention_mask)
        h = out.last_hidden_state
        # Mean-pool over real tokens: robust for decoder LMs whose last token
        # is padding-position dependent.
        m = attention_mask.unsqueeze(-1).to(h.dtype)
        pooled = (h * m).sum(1) / m.sum(1).clamp(min=1.0)
        logits = self.head(torch.cat([pooled.float(), market.float()], dim=-1)).squeeze(-1)
        loss = None
        if labels is not None:
            y = labels.float()
            if self.loss_kind == "focal":
                p = torch.sigmoid(logits)
                pt = torch.where(y > 0.5, p, 1 - p)
                alpha = torch.where(y > 0.5, torch.full_like(y, self.pos_weight / (1 + self.pos_weight)),
                                    torch.full_like(y, 1 / (1 + self.pos_weight)))
                bce = nn.functional.binary_cross_entropy_with_logits(logits, y, reduction="none")
                loss = (alpha * (1 - pt) ** self.gamma * bce).mean()
            else:
                loss = nn.functional.binary_cross_entropy_with_logits(
                    logits, y, pos_weight=torch.tensor(self.pos_weight, device=logits.device))
        return {"loss": loss, "logits": logits}


def build_encoder(name: str, bf16: bool, grad_ckpt: bool, lora: int):
    from transformers import AutoConfig, AutoModel, AutoTokenizer

    if name == "tiny":
        from transformers import Qwen2Config, Qwen2Model

        cfg = Qwen2Config(vocab_size=512, hidden_size=64, intermediate_size=128, num_hidden_layers=2,
                          num_attention_heads=4, num_key_value_heads=2, max_position_embeddings=512)
        enc = Qwen2Model(cfg)
        tok = _byte_tokenizer(cfg.vocab_size)
        return enc, tok, cfg.hidden_size
    tok = AutoTokenizer.from_pretrained(name)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    kwargs = {"attn_implementation": "sdpa"}
    if bf16:
        kwargs["torch_dtype"] = torch.bfloat16
    enc = AutoModel.from_pretrained(name, **kwargs)
    if grad_ckpt:
        enc.gradient_checkpointing_enable()
    if lora:
        from peft import LoraConfig, get_peft_model

        enc = get_peft_model(enc, LoraConfig(r=lora, lora_alpha=2 * lora, lora_dropout=0.05,
                                             target_modules=["q_proj", "k_proj", "v_proj", "o_proj"]))
    return enc, tok, AutoConfig.from_pretrained(name).hidden_size


class _byte_tokenizer:
    """Stand-in tokenizer for the smoke test: UTF-8 bytes mod vocab."""

    def __init__(self, vocab: int) -> None:
        self.vocab, self.pad_token_id = vocab, 0

    def __call__(self, texts, truncation=True, max_length=512, **_):
        return {"input_ids": [[1 + (b % (self.vocab - 1)) for b in t.encode()[:max_length]] or [1]
                              for t in texts]}


# ---------------------------------------------------------------------------
# Train / evaluate
# ---------------------------------------------------------------------------


@torch.no_grad()
def predict(model: MoverHead, ds: Batches, pad_id: int, batch: int, device) -> np.ndarray:
    model.eval()
    loader = torch.utils.data.DataLoader(ds, batch_size=batch, collate_fn=lambda b: collate(b, pad_id))
    out = []
    for b in loader:
        b = {k: v.to(device) for k, v in b.items() if k != "labels"}
        out.append(torch.sigmoid(model(**b)["logits"]).float().cpu().numpy())
    model.train()
    return np.concatenate(out) if out else np.zeros(0)


def metrics(y: np.ndarray, p: np.ndarray) -> dict:
    auc = stats.roc_auc(p.tolist(), y.astype(int).tolist())
    order = np.argsort(-p)
    top = order[: max(1, len(p) // 10)]
    base = y.mean() if len(y) else float("nan")
    return {"n": int(len(y)), "base_rate": float(base), "roc_auc": None if auc is None else round(auc, 4),
            "top_decile_lift": round(float(y[top].mean() / base), 3) if base else None,
            "ece": stats.ece(p.tolist(), y.astype(int).tolist())["ece"],
            "share_predicted_positive": round(float((p >= 0.5).mean()), 4)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True)
    ap.add_argument("--model", default="Qwen/Qwen2.5-1.5B", help="HF id, or 'tiny' for a smoke test")
    ap.add_argument("--market-cols", default="", help="comma-separated numeric columns")
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--grad-accum", type=int, default=1)
    ap.add_argument("--lr", type=float, default=2e-5)
    ap.add_argument("--head-lr", type=float, default=1e-3)
    ap.add_argument("--max-len", type=int, default=1024)
    ap.add_argument("--loss", choices=["weighted", "focal"], default="weighted")
    ap.add_argument("--bf16", action="store_true")
    ap.add_argument("--grad-ckpt", action="store_true")
    ap.add_argument("--lora", type=int, default=0, help="LoRA rank (0 = full fine-tune)")
    ap.add_argument("--baseline-preds", default="", help="CSV with id,prob for the TF-IDF+market baseline")
    ap.add_argument("--out", default=str(ROOT / "runs" / "mover_head"))
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args(argv)

    torch.manual_seed(a.seed)
    np.random.seed(a.seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    df = load(a.data)
    cols = [c for c in a.market_cols.split(",") if c.strip()]
    tr, va, te = (df[df.split == s].reset_index(drop=True) for s in ("train", "val", "test"))
    scaler = MarketScaler(cols).fit(tr)
    pos = float(tr.mover.mean())
    pos_weight = (1 - pos) / pos if 0 < pos < 1 else 1.0
    print(f"train {len(tr):,}  val {len(va):,}  test {len(te):,}  mover rate {pos:.1%}  "
          f"pos_weight {pos_weight:.2f}  loss {a.loss}  device {device}")

    encoder, tok, hidden = build_encoder(a.model, a.bf16, a.grad_ckpt, a.lora)
    pad_id = int(getattr(tok, "pad_token_id", 0) or 0)
    model = MoverHead(encoder, hidden, scaler.dim, loss=a.loss, pos_weight=pos_weight).to(device)

    def ds(frame: pd.DataFrame) -> Batches:
        enc = tok(frame.text.fillna("").tolist(), truncation=True, max_length=a.max_len)
        return Batches({"input_ids": enc["input_ids"]}, scaler.transform(frame), frame.mover.to_numpy())

    d_tr, d_va, d_te = ds(tr), ds(va), ds(te)
    loader = torch.utils.data.DataLoader(d_tr, batch_size=a.batch, shuffle=True,
                                         collate_fn=lambda b: collate(b, pad_id))
    opt = torch.optim.AdamW([
        {"params": [p for p in model.encoder.parameters() if p.requires_grad], "lr": a.lr},
        {"params": model.head.parameters(), "lr": a.head_lr},
    ], weight_decay=0.01)
    steps = max(1, math.ceil(len(loader) * a.epochs / a.grad_accum))
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=[a.lr, a.head_lr], total_steps=steps,
                                                pct_start=0.05)
    autocast = torch.autocast(device.type, dtype=torch.bfloat16, enabled=a.bf16)

    best_auc, step, history = -1.0, 0, []
    total_batches = int(len(loader) * a.epochs)
    seen = 0
    while seen < total_batches:
        for b in loader:
            if seen >= total_batches:
                break
            b = {k: v.to(device) for k, v in b.items()}
            with autocast:
                loss = model(**b)["loss"] / a.grad_accum
            loss.backward()
            seen += 1
            if seen % a.grad_accum == 0:
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                opt.step()
                sched.step()
                opt.zero_grad()
                step += 1
            if seen % max(1, len(loader) // 2) == 0 or seen == total_batches:
                m = metrics(va.mover.to_numpy(), predict(model, d_va, pad_id, a.batch * 2, device))
                history.append({"batches": seen, **m})
                print(f"   batch {seen:>6}/{total_batches}  val AUC {m['roc_auc']}  "
                      f"lift {m['top_decile_lift']}  pred+ {m['share_predicted_positive']}")
                if (m["roc_auc"] or 0) > best_auc:
                    best_auc = m["roc_auc"] or 0
                    torch.save(model.head.state_dict(), out / "head.pt")
                    if a.model != "tiny":
                        model.encoder.save_pretrained(out / "encoder")

    # Evaluate the best checkpoint's head on test (encoder from the last
    # step when not saved separately — tiny smoke runs only).
    model.head.load_state_dict(torch.load(out / "head.pt"))
    p_te = predict(model, d_te, pad_id, a.batch * 2, device)
    report = {"args": vars(a), "val_history": history, "test": metrics(te.mover.to_numpy(), p_te),
              "baseline_auc_reference": BASELINE_AUC, "scaler": {"mean": scaler.mean, "std": scaler.std}}
    pd.DataFrame({"id": te.id, "prob": p_te, "mover": te.mover}).to_csv(out / "test_preds.csv", index=False)

    if a.baseline_preds:
        base = pd.read_csv(a.baseline_preds).set_index("id").prob
        joined = te.assign(prob=p_te).join(base.rename("base_prob"), on="id").dropna(subset=["base_prob"])
        cmp = stats.paired_auc_difference(joined.base_prob.tolist(), joined.prob.tolist(),
                                          joined.mover.astype(int).tolist())
        report["vs_baseline"] = cmp
        verdict = ("BEATS the baseline (paired CI clear of zero)" if cmp.get("ci_excludes_zero") and cmp["diff"] > 0
                   else "does NOT beat the baseline — keep TF-IDF+market")
        print(f"\nvs baseline: AUC {cmp['auc_b']} vs {cmp['auc_a']}, diff {cmp['diff']} "
              f"CI {cmp['ci95']} -> {verdict}")
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str))
    print(f"test AUC {report['test']['roc_auc']}  (bar: {BASELINE_AUC} with a paired CI clear of zero)")
    print(out / "report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
