---
license: apache-2.0
base_model: Qwen/Qwen2.5-1.5B-Instruct
language:
  - en
library_name: transformers
pipeline_tag: text-generation
tags:
  - finance
  - india
  - nse
  - bse
  - document-understanding
---

# tradebot-slm-v1

A 1.5B model that reads an Indian corporate filing (NSE/BSE) and emits
structured JSON — event type, materiality, extracted amounts, and a prediction
of whether and how the stock moves:

```json
{"event_type": "ORDER_WIN", "materiality": "HIGH", "surprise": "MEDIUM",
 "facts": {"amount_inr_cr": 412.0, "amount_basis": "NEW_ORDER",
           "amount_to_mcap": 0.041},
 "direction": "UP", "mover": true, "shape": "IMMEDIATE",
 "price_path":  [3, 8, 14, 17, 19, 21, 22, 22, 23, 24, 24, 25, 25, 26, 26, 31, 28],
 "volume_path": [9.1, 6.4, 4.8, 3.9, 3.3, 2.9, 2.6, 2.4, 2.2, 2.1, 2.0, 1.9, 1.8, 1.8, 1.7, 1.4, 1.1]}
```

`price_path` is the market-adjusted return in **tenths of a percent** at
t1…t15, t30, t60. `volume_path` is the multiple of that symbol's pre-news
1-minute volume norm. Vocabularies: `event_type` ∈ {ORDER_WIN, RESULTS, MA,
DIVIDEND, BUYBACK, BOARD_MEETING, MGMT_CHANGE, FUNDRAISE, REGULATORY, PRESS,
INVESTOR_MEET, RATING, SHAREHOLDER, TRADING_WINDOW, OTHER}; `materiality` ∈
{HIGH, MEDIUM, LOW, UNKNOWN}; `surprise` ∈ {HIGH, MEDIUM, LOW, NONE};
`direction` ∈ {UP, DOWN, FLAT}; `shape` ∈ {IMMEDIATE, DELAYED, FADE, FLAT};
`amount_basis` ∈ {NEW_ORDER, RENEWAL, OTHER, NONE}.

There is deliberately **no confidence field** — the targets are measured
outcomes, and nothing in the corpus supervises a calibrated self-estimate.

**What makes it unusual:** it is supervised by **measured market reaction**,
not by another LLM's sentiment labels. FinBERT, FinGPT and FinMA are all
trained on human- or LLM-assigned sentiment. This one's targets are what the
price actually did in the 15 minutes after the filing was published,
market-adjusted against NIFTY 50.

## Results, stated honestly

Head-to-head against DeepSeek on the same 600 filings, neither model having
seen them:

| | tradebot-slm-v1 | DeepSeek | always-predict-FLAT |
|---|---|---|---|
| direction accuracy | 0.5733 | 0.5617 | **0.5950** |
| balanced accuracy | **0.3317** | 0.3236 | 0.3333 |
| mover ROC-AUC | **0.5071** | 0.4234 | 0.500 |

Two things are true at once and both belong in the summary:

1. **It beats DeepSeek on every metric**, and DeepSeek's confidence is
   *anti*-predictive (0.4234, below the 0.500 coin flip) — independently
   reproducing an inversion measured on 17,298 filings in production.
2. **Both lose to a constant.** They fail the same way: DeepSeek answers HOLD
   94.0% of the time, this model answers FLAT 94.2%. Beating a model that is
   worse than a coin flip proves very little.

### As a feature extractor

Mean-pooled hidden states, fed to the same gradient-boosting head, on the
frozen chronological test split (n=17,723):

| representation | alone | + market features | PR-AUC |
|---|---|---|---|
| TF-IDF + SVD(256) | 0.7014 | **0.7649** | 0.3956 |
| this model's embedding | 0.6578 | 0.7621 | **0.3977** |
| market features only | — | 0.7513 | 0.3718 |

Paired bootstrap, 2,000 resamples:

| comparison | Δ ROC-AUC | 95% CI | verdict |
|---|---|---|---|
| fine-tune + market vs market | +0.0108 | [+0.0074, +0.0145] | significant |
| TF-IDF + market vs market | +0.0136 | [+0.0095, +0.0177] | significant |
| TF-IDF vs fine-tune | +0.0028 | [−0.0001, +0.0057] | **spans 0 — indistinguishable** |

**A $33 fine-tune matched TF-IDF + SVD, which costs ten minutes of laptop
CPU.** Not beaten, not lost — matched. Text does carry signal beyond market
state (both clear the baseline with intervals well clear of zero), but this
model is not a better way to extract it than a bag of words.

Why, in hindsight: the training objective is next-token prediction over a JSON
schema. That optimises format compliance — eval loss fell 74% — and nothing in
it asks the hidden states to encode *what makes a filing move*.

## What is untested

The **generative** output — event classification, amount extraction, evidence
spans — has not been evaluated. That is the part with no cheap substitute, and
the honest next measurement.

## Do not quote raw accuracy

Direction at ±3% reads 0.9638. **That is the base rate.** 96.4% of filings do
not move 3%; a model that says FLAT every time scores 0.9643. Balanced
accuracy is the honest column and it says 0.3333 — chance. Direction is not
learnable at ±1/2/3% on this data; `mover` is the only learnable target.

## Training

| | |
|---|---|
| base | `Qwen/Qwen2.5-1.5B-Instruct` (Apache 2.0) |
| method | full fine-tune, no LoRA |
| data | 146,500 NSE filings, chronological split (train ≤ 2026-04-04) |
| epochs / steps | 1 / 2,290 |
| lr, schedule | 1e-5, cosine, 3% warmup |
| batch | 1 × 64 accumulation, seq 2,048 |
| optimizer | 8-bit AdamW |
| hardware | 1× A10G (g5.2xlarge), ~19 h, ~$33 |
| eval loss / ppl | 0.4102 / 1.507 (eval below train throughout) |
| checkpoint | 2,000 — picked on validation, not on being last |

Steps 1,000 → 2,250 improved eval loss by 0.0018 for ~11 hours and ~$19. If
you retrain, budget one epoch and expect the useful learning in the first
third.

The corpus was built from 243,533 filings with measured outcomes, extracted
from source PDFs at 98.0% coverage (OCR pass included). Near-duplicates were
removed per (template, symbol) — 9.3% of Indian filings are near-copies of
each other, and training on the same cover letter repeatedly teaches the
cover letter.

## Usage

**The prompt format is part of the weights.** All 146,500 training examples
used the exact system prompt and user layout below. Improvise on it and the
model is off-distribution — you get worse output and the fine-tune is wasted.

```python
from transformers import AutoModelForCausalLM, AutoTokenizer

tok = AutoTokenizer.from_pretrained("pravz/slm_v1")
model = AutoModelForCausalLM.from_pretrained("pravz/slm_v1", dtype="auto")

SYSTEM = (
    "You are a financial analyst specialised in Indian NSE/BSE corporate "
    "filings. Read the filing, extract the material facts, and reason about "
    "the likely market reaction. Return only the requested JSON. Never use "
    "information from after the filing timestamp."
)

user = f"""SYMBOL: {symbol}
FILED: {filed_at}
HEADLINE: {headline}

FILING:
{filing_text}"""

ids = tok.apply_chat_template(
    [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
    return_tensors="pt", add_generation_prompt=True,
)
print(tok.decode(model.generate(ids, max_new_tokens=400)[0][ids.shape[-1]:],
                 skip_special_tokens=True))
```

Training prompts also carried a `MARKET CONTEXT` block (last trade before the
filing, pre-news volume ratio, market-cap tier, session). It is optional —
omit the block entirely rather than filling it with placeholder numbers.

Or serve it behind an OpenAI-compatible endpoint: `vllm serve pravz/slm_v1`
on Linux+GPU, or the CPU-friendly, stdlib-only `serve_slm.py` in the repo
below, which runs anywhere `transformers` runs.

**Speed:** ~4 tok/s on a CPU laptop in bf16 (~36 s for one filing). Use a GPU
for anything interactive.

## Intended use and limits

Research and education. **Not investment advice.** It was trained on
2025–2026 Indian filings and will not transfer to other markets, other
document types, or other time periods without re-measurement. Do not put it
in front of money — the results above say it does not beat a bag of words at
the one task it was trained for.

Repository, dataset construction, baselines and evaluation code:
<https://github.com/thenameispravardhan/market-proof>
