"""Risk engine: gates every signal before it reaches a broker.

The engine is a pure-function checker — no network, no caching, no
side effects. The manager routes every signal through it; the
verifier tries 10+ bypass methods and the engine has to fail closed
on every one of them.

The hard rules and their codes are documented in `engine.py`. The
short version:

  R1. action not BUY/SELL          -> RISK_ACTION_NOT_TRADABLE
  R2. confidence <= 0              -> RISK_CONFIDENCE_NONPOSITIVE
  R3. sized qty < 1                -> RISK_QTY_BELOW_1
  R4. risk > max_capital_risk_pct  -> RISK_MAX_CAPITAL_RISK_PCT
  R5. daily loss >= cap            -> RISK_DAILY_MAX_LOSS
  R6. concurrent positions >= cap  -> RISK_MAX_CONCURRENT_POSITIONS
  R7. position > max_single_pos_%  -> RISK_MAX_SINGLE_POSITION_PCT
  R8. ADV < min_liquidity_crore    -> RISK_MIN_LIQUIDITY
  R9. sector > 30% (default)       -> RISK_SECTOR_CONCENTRATION
  R10. symbol in blocklist         -> RISK_SYMBOL_BLOCKLISTED
"""
from app.risk.engine import (
    DEFAULT_SECTOR_CAP_PCT,
    DEFAULT_SECTOR_MAP,
    RiskDecision,
    RiskEngine,
    SizingResult,
    StrategyOverrides,
    load_sector_map,
)

__all__ = [
    "DEFAULT_SECTOR_CAP_PCT",
    "DEFAULT_SECTOR_MAP",
    "RiskDecision",
    "RiskEngine",
    "SizingResult",
    "StrategyOverrides",
    "load_sector_map",
]
