"""The Algo Lab guide: what the builder and backtest can do, in one text a
person — or an AI asked to write a strategy — can follow.

guide.md is the hand-written part (concepts, the JSON spec, worked examples);
`text()` appends a reference generated from the running engine — every
indicator with its settings and outputs, and every allowed value — so the
guide can't drift from what the importer accepts.
"""
from __future__ import annotations

from pathlib import Path

from app.algo import engine
from app.algo import indicators as ind

GUIDE = Path(__file__).with_name("guide.md")


def _params(p: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in p.items()) or "—"


def reference() -> str:
    tf = ", ".join(str(t) for t in engine.TIMEFRAMES)
    lines = [
        "---", "",
        "## Reference (generated from the app)", "",
        f"* **Strategy timeframes** (minutes): {tf} — 1440 = daily, 10080 = weekly. A daily / weekly "
        "strategy is checked on the session's close and fills at the next session's open; its positions carry "
        "(no entry window or square-off). Conditions may also use `tf: 1440` / `10080` (above the strategy's own) "
        "or, intraday, any of these that is a multiple of the strategy's.",
        f"* **Symbols**: up to {engine.MAX_SYMBOLS} per strategy.",
        f"* **Operators**: {', '.join(f'`{o}`' for o in engine.OPS)}.",
        f"* **Transforms** (`xf`, `xn` 1-{engine.MAX_LOOKBACK}): {', '.join(f'`{x}`' for x in engine.TRANSFORMS)}.",
        f"* **Combined values** (`math`): {', '.join(f'`{m}`' for m in engine.MATH)}.",
        f"* **Price sources** (`source` param): {', '.join(f'`{x}`' for x in ind.SOURCES)}.",
        f"* **Instrument types**: {', '.join(f'`{x}`' for x in engine.INSTRUMENTS)}; **leg strikes**: "
        f"{', '.join(f'`{x}`' for x in engine.STRIKE_MODES)}; **leg rights**: `CE`, `PE`, `FUT`; "
        "**actions**: `BUY`, `SELL`.",
        f"* **on_leg_stop**: {', '.join(f'`{x}`' for x in engine.LEG_STOP_ACTIONS)}; **margin models**: "
        f"{', '.join(f'`{x}`' for x in engine.MARGIN_MODELS)}; **IV sources**: `auto`, `vix`, `hv`, `fixed`.",
        "* **Level types**: stop_loss / trailing / breakeven `pct`, `points`, `atr`; target also `rr`; "
        f"scale-out {', '.join(f'`{x}`' for x in engine.SCALE_KINDS)}.",
        f"* **Sizing modes**: {', '.join(f'`{x}`' for x in engine.SIZING)}.",
        "* **Entry orders**: `market`, `pullback`, `breakout`.",
        f"* **Position-rule metrics**: {', '.join(f'`{x}`' for x in engine.POSITION_METRICS)}; ops "
        f"{', '.join(f'`{x}`' for x in engine.RULE_OPS)}.",
        f"* **Optimiser metrics**: {', '.join(f'`{x}`' for x in engine.METRICS)} (≤ {engine.MAX_COMBOS} "
        "combinations).",
        "* **Limits**: 50 symbols; groups nest 4 deep, combined values 3 deep; for_bars / within_bars ≤ 500; "
        "5 scale-out steps; 10 position rules per list; candles per day 5-1000; leg delta 0.01-0.99.",
        "",
        "### Indicators",
        "",
        "Use `ind` exactly as written. `params` you leave out take the default shown; `field` picks an output "
        "(the first is the default). Period-like params are whole numbers ≥ 1.",
        "",
        "| ind | group | params (defaults) | fields | what it is |",
        "|---|---|---|---|---|",
    ]
    for c in ind.catalog():
        lines.append(f"| `{c['name']}` | {c['group']} | {_params(c['params'])} | "
                     f"{', '.join(f'`{o}`' for o in c['outputs'])} | {c['about']} |")
    return "\n".join(lines) + "\n"


def text() -> str:
    return GUIDE.read_text(encoding="utf-8").rstrip() + "\n\n" + reference()
