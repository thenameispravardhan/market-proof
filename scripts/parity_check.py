"""The parity harness -- c++.text §10.

"For any recorded input, the C++ system must produce the same decision as the
Python system, or the difference must be explained and approved in writing."

    # build a corpus from the live DB, then diff both stacks over it
    python scripts/build_corpus.py --out corpus/
    python scripts/parity_check.py corpus/ --replay cpp/build/replay_cpp

Exit code 0 = zero diffs. Anything else means the phase is not done, however
green the unit tests are (§10.6).

Only the Phase 5 surface is compared today: fast track, schema validation,
rules decision and the clock gate. Sizing / stop / target / risk_events join
as Phase 9 lands (§10.3 lists the full contract).
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any

from app.analyzer.fast_track import evaluate_fast_track, evaluate_fast_track_text
from app.analyzer.rules_engine import evaluate as evaluate_rules
from app.analyzer.schemas import AnalysisResponse, analysis_to_dict

# §10.4: float last-digit differences are legitimate; a real logic change is
# not. The tolerance is enforced, not used to hide diffs -- anything outside it
# is reported.
FLOAT_RTOL = 1e-9


def python_decision(case: dict[str, Any]) -> dict[str, Any]:
    """Run one corpus case through the PYTHON stack. This is the reference."""
    headline = case.get("headline") or ""
    extracted = case.get("extracted_text") or ""

    match = evaluate_fast_track(headline)
    if match is None and extracted:
        match = evaluate_fast_track_text(headline, extracted)

    out: dict[str, Any] = {
        "fast_track": None
        if match is None
        else {"pattern": match.pattern, "response": analysis_to_dict(match.response)}
    }

    analysis = None
    if match is not None:
        analysis = match.response
    elif isinstance(case.get("llm_response"), dict):
        try:
            analysis = AnalysisResponse(**case["llm_response"])
        except Exception as e:  # noqa: BLE001 -- the error text is the contract
            out["analysis"] = {"error": str(e)}

    if analysis is not None:
        out["analysis"] = analysis_to_dict(analysis)
    elif "analysis" not in out:
        out["analysis"] = None

    analysis_dict = dict(out["analysis"]) if isinstance(out["analysis"], dict) else {}
    analysis_dict.pop("error", None)
    for k, v in (case.get("context") or {}).items():
        analysis_dict.setdefault(k, v)

    m = evaluate_rules(analysis_dict, case.get("rules") or [])
    out["rule"] = {
        "rule_id": m.rule_id,
        "action": m.action,
        "action_params": m.action_params,
        "rationale": m.rationale,
    }

    if "now_epoch" in case:
        from datetime import datetime, timezone

        from app.risk.market_clock import entry_block_reason, is_market_open, square_off_due

        now = datetime.fromtimestamp(case["now_epoch"], tz=timezone.utc)
        out["entry_block_reason"] = entry_block_reason(now)
        out["is_market_open"] = is_market_open(now)
        out["square_off_due"] = square_off_due(now)
    return out


def diff(want: Any, got: Any, path: str = "") -> list[str]:
    """Structural diff with the §10.4 float tolerance. Returns human-readable
    lines, empty when the two agree."""
    if isinstance(want, bool) or isinstance(got, bool):
        return [] if want is got else [f"{path}: python={want!r} cpp={got!r}"]
    if isinstance(want, (int, float)) and isinstance(got, (int, float)):
        if want == got:
            return []
        if math.isclose(float(want), float(got), rel_tol=FLOAT_RTOL, abs_tol=0.0):
            return []
        return [f"{path}: python={want!r} cpp={got!r}"]
    if isinstance(want, dict) and isinstance(got, dict):
        out: list[str] = []
        for k in sorted(set(want) | set(got)):
            if k not in want:
                out.append(f"{path}.{k}: absent in python, cpp={got[k]!r}")
            elif k not in got:
                out.append(f"{path}.{k}: python={want[k]!r}, absent in cpp")
            else:
                out += diff(want[k], got[k], f"{path}.{k}")
        return out
    if isinstance(want, list) and isinstance(got, list):
        if len(want) != len(got):
            return [f"{path}: length python={len(want)} cpp={len(got)}"]
        out = []
        for i, (a, b) in enumerate(zip(want, got)):
            out += diff(a, b, f"{path}[{i}]")
        return out
    return [] if want == got else [f"{path}: python={want!r} cpp={got!r}"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus", type=Path, help="directory of <id>/case.json")
    ap.add_argument("--replay", type=Path, default=Path("cpp/build/replay_cpp"))
    ap.add_argument("--max-report", type=int, default=20)
    ap.add_argument("--write-expected", action="store_true",
                    help="only regenerate expected.json; do not run the C++")
    args = ap.parse_args()

    cases = sorted(args.corpus.glob("*/case.json"))
    if not cases:
        print(f"no cases under {args.corpus}", file=sys.stderr)
        return 2

    if not args.write_expected and not args.replay.exists():
        print(f"replay binary not found: {args.replay}\n"
              f"build it first:  cmake --build cpp/build --target replay_cpp",
              file=sys.stderr)
        return 2

    total = mismatched = 0
    for case_path in cases:
        case = json.loads(case_path.read_text(encoding="utf-8"))
        expected = python_decision(case)
        (case_path.parent / "expected.json").write_text(
            json.dumps(expected, indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )
        if args.write_expected:
            total += 1
            continue

        proc = subprocess.run(
            [str(args.replay)],
            input=case_path.read_text(encoding="utf-8"),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        total += 1
        if proc.returncode != 0:
            mismatched += 1
            if mismatched <= args.max_report:
                print(f"{case_path.parent.name}: replay_cpp exited {proc.returncode}: "
                      f"{proc.stderr.strip()[:200]}")
            continue
        try:
            actual = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            mismatched += 1
            if mismatched <= args.max_report:
                print(f"{case_path.parent.name}: unparseable cpp output: {e}")
            continue

        (case_path.parent / "actual.json").write_text(
            json.dumps(actual, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8"
        )
        lines = diff(expected, actual)
        if lines:
            mismatched += 1
            if mismatched <= args.max_report:
                print(f"\n{case_path.parent.name}:")
                for line in lines[:10]:
                    print(f"  {line}")

    print(f"\ncases     {total}")
    print(f"diffs     {mismatched}")
    if args.write_expected:
        print("(expected.json regenerated; C++ not run)")
        return 0
    print("PARITY GREEN" if mismatched == 0 else "PARITY RED -- phase is not done")
    return 1 if mismatched else 0


if __name__ == "__main__":
    sys.exit(main())
