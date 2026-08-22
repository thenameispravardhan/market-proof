"""Build the replayable corpus -- c++.text §9 PHASE 0, §10.2.

"Without this the rest of the migration is unverifiable."

Backfills one directory per announcement from the live DB, joining each
announcement to its recorded analysis and to the signal rules that were
enabled at the time, so scripts/parity_check.py can replay the pair through
both stacks.

    python scripts/build_corpus.py --out corpus/ --limit 5000

RECONSTRUCTION, NOT A RECORDING (read this before trusting a green parity run):
§10.2 wants `llm_response.json` to be DeepSeek's REPLY, recorded and replayed.
That reply is not stored anywhere. `analyses.raw_response` keeps only
event_type / summary / key_numbers / model / tokens / latency, and the
sentiment, score, confidence, recommendation and rationale live in dedicated
`analyses` columns -- all of them POST-validation. So this backfill composes an
analysis from both halves, and what it exercises is the rules engine, not the
schema validators: the coercions (NEUTRAL->HOLD, key_numbers shapes, the 0..1
score rescale) already ran before these values were written, and replaying them
cannot catch a regression in them. Two consequences:

  1. A stored sentiment_score whose magnitude is <= 1 will be rescaled a SECOND
     time by _normalise_score on replay. Rare, and flagged in cpp/DIFFS.md.
  2. The forward instrumentation Phase 0 still owes must persist the RAW reply
     bytes. Only then does the validator half become verifiable.

MEASURED SHAPE OF THE DATA: `analyses.raw_response` is NOT always an analysis.
15,828 of 28,381 rows carry `model='none'` and an `error` key instead -- the
filing was skipped (too old, HTTP error, timeout), so there is no LLM decision
to replay. Those cases are still emitted (the fast-track and rules paths are
exercisable from the headline alone) but carry no `llm_response`, and
--analysed-only filters the corpus down to the ~3,259 genuinely replayable
analyses.

KNOWN GAP (measured, not assumed): `announcements.body` is NULL for all 28,381
rows and the extracted PDF text is never persisted, so the corpus this builds
covers the HEADLINE path only. The hybrid PDF path (evaluate_fast_track_text)
and everything in §9 PHASE 8 cannot be replayed until the pipeline is
instrumented to dump extracted text at analysis time. That instrumentation is
the rest of Phase 0's work and it must happen going FORWARD -- the past cannot
be recovered. Same for the full day of raw Fyers WS frames Phase 13 needs.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


def load_rules(con: sqlite3.Connection) -> list[dict[str, Any]]:
    """Enabled rules from enabled strategies, priority ASC -- what the live
    analyzer evaluates against (load_rules_for_enabled_strategies)."""
    rows = con.execute(
        """
        SELECT r.id, r.name, r.priority, r.enabled, r.action, r.action_params, r.conditions
          FROM signal_rules r
          JOIN strategies s ON r.strategy_id = s.id
         WHERE r.enabled = 1 AND s.enabled = 1
         ORDER BY r.priority ASC, r.id ASC
        """
    ).fetchall()
    out = []
    for rid, name, priority, enabled, action, params, conds in rows:
        out.append(
            {
                "id": rid,
                "name": name,
                "priority": priority,
                "enabled": bool(enabled),
                "action": action,
                "action_params": json.loads(params) if params else {},
                "conditions": json.loads(conds) if conds else {},
            }
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/trading.db")
    ap.add_argument("--out", type=Path, default=Path("corpus"))
    ap.add_argument("--limit", type=int, default=5000, help="§9 PHASE 0 exit needs >= 5000")
    ap.add_argument("--analysed-only", action="store_true",
                    help="emit only cases with a replayable LLM analysis")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rules = load_rules(con)
    if not rules:
        print("warning: no enabled rules -- every case will hit the HOLD fallback",
              file=sys.stderr)

    args.out.mkdir(parents=True, exist_ok=True)
    rows = con.execute(
        """
        SELECT a.id, a.symbol, a.exchange, a.headline, a.body, an.raw_response,
               an.sentiment, an.sentiment_score, an.confidence, an.recommendation,
               an.rationale, an.model
          FROM announcements a
          LEFT JOIN analyses an ON an.announcement_id = a.id
         WHERE a.headline IS NOT NULL AND a.headline != ''
         ORDER BY a.id DESC
         LIMIT ?
        """,
        (args.limit,),
    )

    written = with_llm = skipped = 0
    for (ann_id, symbol, exchange, headline, body, raw, sentiment, score, confidence,
         recommendation, rationale, model) in rows:
        case: dict[str, Any] = {
            "announcement_id": ann_id,
            "headline": headline,
            "extracted_text": body or "",
            "rules": rules,
            "context": {"symbol": symbol, "exchange": exchange},
        }
        parsed = None
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None  # a malformed recorded response is not worth failing over
        # An `error` key means the pipeline never got an analysis back;
        # replaying it would only compare pydantic's error text.
        usable = (
            isinstance(parsed, dict)
            and "error" not in parsed
            and model not in (None, "none")
            and None not in (sentiment, score, confidence, recommendation, rationale)
        )
        if usable:
            case["llm_response"] = {
                "event_type": parsed.get("event_type"),
                "summary": parsed.get("summary"),
                "sentiment": sentiment,
                "sentiment_score": score,
                "confidence": confidence,
                "recommendation": recommendation,
                "reasoning": rationale,          # to_db_columns() renames this
                "key_numbers": parsed.get("key_numbers") or {},
            }
            case["recorded_model"] = model
            with_llm += 1

        if args.analysed_only and "llm_response" not in case:
            skipped += 1
            continue

        d = args.out / str(ann_id)
        d.mkdir(exist_ok=True)
        (d / "case.json").write_text(
            json.dumps(case, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8"
        )
        written += 1

    print(f"cases            {written}  -> {args.out}")
    print(f"with llm_response{with_llm:>6}")
    print(f"skipped (no llm) {skipped:>6}")
    print(f"with extracted   {0:>6}  (announcements.body is NULL -- see module docstring)")
    print(f"rules loaded     {len(rules)}")
    if written < 5000:
        print(f"\nNOTE: §9 PHASE 0 exit criterion is >= 5000 cases; this build has {written}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
