# Roadmap status: protect, measure, then model

This file tracks the "measure the edge before scaling" roadmap item by item:
what is now in the code, and what still needs a person, a machine or data
that this repository does not have. Every code item ships with tests and runs
in CI (`.github/workflows/ci.yml`).

| # | Item | In the code | Still needs a person or data |
|---|---|---|---|
| 1 | Pre-market Fyers self-test | `app/services/fyers_selftest.py`; the preflight now runs at 08:45 IST; Dashboard readiness banner with Re-check; `GET/POST /api/system/fyers-selftest` | Set `FYERS_WHITELISTED_IPS` to the server's static IP. Do the daily 2FA login (scripting it would defeat the rule). |
| 2 | Offsite backup, cron, secrets | `deploy/backup.sh` uploads to `BACKUP_S3_URI` and writes `status.json`; `setup.sh` installs the cron; the preflight alarms on a stale or failed backup; `docs/SECRET_ROTATION.md` | Create the S3 bucket (or turn on Lightsail snapshots) and install the AWS CLI. Rotate the secrets (plan item O4) by following the runbook. |
| 3 | Commit DGX recipe; fix SLM plan header | `.gitignore` re-includes `train_mover_head.py` | `docs/DGX_SPARK.md`, `run.sh` and the v2 `train_sft.py` live only on the training machine, and `docs/SLM_TRAINING_PLAN.md` is deliberately gitignored (it carries account details). Copy the recipe in, scrub the account details first, and add `!` lines to `.gitignore`. |
| 4 | `MODEL_ENABLED` telemetry; timing read | `MODEL_ENABLED` now defaults on (it can never block) | The market-hours Timing read, then one-day trials of `NSE_RSS_ENABLED` and `NEWS_AGE_FROM_RECEIPT`. These are decisions on live data. |
| 5 | CI and missing tests | GitHub Actions (pytest, tsc, vitest, backup script test); tests for the Algo Lab live-order path, `/api/model` and the audit service. Getting CI green fixed a NIFTY-less warehouse fill that raised, a stale settings test, a stale backup test, and an audit `log_event` that never existed | Nothing. |
| 6 | Rate limiter, marketable limits, gates | Per-app token bucket on place/modify/cancel; entry limit clamped inside a known circuit band; `GATE_SURVEILLANCE_ENABLED`, `GATE_CIRCUIT_PROXIMITY_PCT`, `GATE_FNO_BAN_ENABLED`, `GATE_LPP_ENABLED` (all off by default); postback flood guard | Decide which gates to turn on. The exit chase still uses MARKET (MPP) on purpose: a protective exit must complete, and a resting limit can leave a position open. |
| 7 | Cost-aware event replay | `app/research/replay.py`, `/api/research/replay`, `scripts/event_replay.py` (with `--sweep-delay`), the block-reason funnel, and the confidence-bucket pre/post move test | Run it on the server, where the candle store lives, and read the result. |
| 8 | Purged walk-forward CV | `app/research/cv.py`; `train.py` purged OOF and `--walk-forward` with per-fold isotonic calibration | Re-run on the 12 GB corpus and restate the offline numbers. |
| 9 | Spark: discriminative mover scorer | `AIdataset/model/train_mover_head.py` (classification head, market features, focal or weighted loss, BF16 + SDPA, paired-bootstrap verdict against the 0.7646 baseline) | Export the corpus to the script's input schema and run it on the Spark. |
| 10 | SLM shadow mode, MARKET CONTEXT, ECE | `LLM_SHADOW_ENABLED`, the `shadow_analyses` table, `/api/research/shadow`; the MARKET CONTEXT block is now sent; monthly ECE in `/api/research/calibration` | Check the labels in `slm_adapter.MARKET_CONTEXT_KEYS` against `train_sft.py`, then point `LLM_SLM_ENDPOINT` at a server. |
| 11 | Meta-labeling | `app/research/meta_label.py`, `/api/research/meta-label`, `event_replay.py --meta` | Run it on the replayed history. Any live use of its sizing is a separate, later decision. |
| 12 | Results parser and extractor | `app/research/results_xbrl.py` (YoY/QoQ revenue, EBITDA, PAT, margin change), `/api/research/results-xbrl` | Wire XBRL URLs from the results feed into the dataset. The 4B extractor model and the `pdf-tables` fixes are not in this repository. |
| 13 | Docs drift, preprint | README counts and structure corrected (the deleted backtester and webhooks modules are gone from it) | `PROJECT.txt` and the `docs-readme-local` branch are not in this repository. The arXiv endorser and venue choices are people's work. |
| 14 | Demo build, frozen paper window | Research page (funnel, calibration, replay, shadow, window, audit check, "why" card); hash-chained audit log; pre-registered evaluation windows; the Timing page already holds the latency waterfall | Start a 4-8 week window and leave the configuration alone while it runs. |
