#!/usr/bin/env python3
"""Prove the SLM toggle end to end: real prompt -> endpoint -> valid signal.

    python scripts/smoke_slm.py                       # uses Settings
    python scripts/smoke_slm.py --endpoint http://127.0.0.1:8001/v1/chat/completions
    python scripts/smoke_slm.py --provider deepseek   # same check, hosted model

Runs the ACTUAL analyzer path — the same DeepSeekClient, the same
`AnalysisResponse` schema the live pipeline validates against — so a pass here
means a real filing would produce a real signal. Exit 0 = the toggle works.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FILING = (
    "Sky Gold and Diamonds Limited has informed the Exchange that the Company "
    "has received an order worth Rs. 412 crore from the Ministry of Railways "
    "for supply and commissioning, to be executed over 18 months. This is new "
    "business and not a renewal of any existing contract."
)

HEADLINE = "Sky Gold and Diamonds Limited - Receipt of Order worth Rs. 412 crore"

DEEPSEEK_SYSTEM = (
    "You are a financial filing analyst for Indian equity markets. Read the "
    "filing and reply with ONLY a JSON object with these keys: event_type, "
    "summary, sentiment (positive|neutral|negative), sentiment_score "
    "(-100..100), confidence (0..1), recommendation (BUY|SELL|HOLD), "
    "reasoning, key_numbers (object). No prose outside the JSON."
)


async def run(args) -> int:
    # Set BEFORE get_settings() is first called — the analyzer reads a cached
    # Settings object, so an env write after that point would be ignored.
    if args.endpoint:
        os.environ["LLM_SLM_ENDPOINT"] = args.endpoint
    if args.model:
        os.environ["LLM_SLM_MODEL"] = args.model
    os.environ["LLM_PROVIDER"] = args.provider

    from app.analyzer.deepseek_client import DeepSeekError
    from app.analyzer.schemas import AnalysisResponse
    from app.analyzer.service import Service
    from app.analyzer.slm_adapter import build_prompt as slm_build_prompt
    from app.analyzer.slm_adapter import to_analysis as slm_to_analysis
    from app.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    svc = Service()
    try:
        client, model = await svc._llm_client(settings)
        # The live analyzer caps this at LLM_TIMEOUT_SECONDS (12s) — correct
        # for a news pipeline, useless for proving a CPU model works at all.
        client._timeout_s = args.timeout or (900.0 if args.provider == "slm" else 30.0)
        target = model or "deepseek-v4-flash"
        where = getattr(client, "_endpoint", "?")
        if args.provider == "slm" and model is None:
            print("FAIL: provider=slm but the analyzer fell back to DeepSeek.")
            print("      LLM_SLM_ENDPOINT is empty — set it in Settings or pass --endpoint.")
            return 1
        print(f"provider={args.provider}  model={target}\nendpoint={where}\n")

        is_slm = args.provider == "slm"
        if is_slm:
            # The exact prompt the analyzer sends — training format.
            system, user = slm_build_prompt(
                symbol="SKYGOLD", filed_at="2026-09-04 10:12:03",
                headline=HEADLINE, filing_text=FILING,
            )
        else:
            system, user = DEEPSEEK_SYSTEM, f"Filing:\n{FILING}"

        t0 = time.perf_counter()
        try:
            result = await client.complete(
                system=system,
                user=user,
                model=target,
                temperature=0.2,
                max_tokens=400,
                # DeepSeek-only body fields; the analyzer strips them for the
                # SLM too, so this mirrors the live call exactly.
                reasoning_effort=None if is_slm else "medium",
                thinking=True,
                stream=False,
            )
        except DeepSeekError as e:
            print(f"FAIL: endpoint rejected the call: {e}")
            return 1
        took = time.perf_counter() - t0

        print(f"--- raw ({took:.1f}s, {result.completion_tokens} completion tokens) ---")
        print(result.content.strip()[:1200])

        try:
            if is_slm:
                payload, _raw = slm_to_analysis(result.content, headline=HEADLINE)
                print("\n--- mapped onto AnalysisResponse ---")
                print(json.dumps(payload, indent=2)[:900])
            else:
                payload = json.loads(_strip_fence(result.content))
        except json.JSONDecodeError as e:
            print(f"\nFAIL: response is not JSON: {e}")
            return 1
        except Exception as e:  # noqa: BLE001
            print(f"\nFAIL: could not map the reply: {e}")
            return 1
        try:
            parsed = AnalysisResponse.model_validate(payload)
        except Exception as e:  # noqa: BLE001
            print(f"\nFAIL: JSON does not satisfy AnalysisResponse: {e}")
            return 1

        print("\n--- validated ---")
        print(f"event_type     {parsed.event_type}")
        print(f"recommendation {parsed.recommendation}")
        print(f"confidence     {parsed.confidence}")
        print(f"sentiment      {parsed.sentiment} ({parsed.sentiment_score})")
        print(f"\nPASS — this response would reach the rules engine.")
        return 0
    finally:
        await svc.aclose()


def _strip_fence(text: str) -> str:
    """Small models like to wrap JSON in ```json fences. The live analyzer's
    parser already tolerates this; mirror it so the smoke test is not stricter
    than production."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        t = t.rsplit("```", 1)[0]
    return t.strip()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="slm", choices=["slm", "deepseek"])
    ap.add_argument("--endpoint", default=None)
    ap.add_argument("--model", default=None)
    # A 1.5B on CPU runs at ~2 tok/s — minutes, not the live 12s budget.
    ap.add_argument("--timeout", type=float, default=None,
                    help="client timeout in seconds (default 900 for slm, 30 for deepseek)")
    raise SystemExit(asyncio.run(run(ap.parse_args())))
