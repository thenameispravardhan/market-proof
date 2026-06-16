"""Backtest engine.

Replays historical announcements through the existing pipeline:

  announcement  →  analyzer (DeepSeek stub for speed)
                →  rules engine (existing SignalRule.evaluate)
                →  risk engine (existing RiskEngine.evaluate)
                →  paper execution (existing PaperBackend.place_order)
                →  equity mark-to-market (existing MarketDataBus)

For each announcement in [start_date, end_date] we:

  1. Synthesise a price (synthetic-return model) for `symbol` on
     the announcement's `filed_at` date. The model is pluggable
     via `PriceModel` so the operator can swap in a real Fyers
     historical loader later.
  2. Build a deterministic `AnalysisResponse` based on keyword
     heuristics (so we don't hit DeepSeek — the backtest is
     supposed to be a "what would the rule+RISK engine have
     done with perfect analysis" simulation).
  3. Persist the analysis + signal, run rules, run risk, place
     the order via PaperBackend, mark-to-market.

Every step is recorded in `BacktestResult.decisions` (a list of
dicts) and the final `metrics` come from `metrics.summarise`.

Isolation: a backtest must never leak into the live trading view.
The analyzer / rules / risk / paper code all read and write the
shared `analyses`, `signals`, `trades`, and `positions` tables —
so running them against the live DB would pollute the Dashboard,
Trade History, and Positions panels (and, worse, leave fake
`PAPER-` positions that the live risk engine counts against its
concurrency / sector limits). To prevent that, each run builds a
throwaway in-memory SQLite **sandbox** seeded with copies of the
strategy, its rules, and the broker account. Every simulated
analysis / signal / trade / position is written there and thrown
away when the run ends. The live DB is touched only to (a) read
the source announcements + config and (b) write the final
`backtest_runs.results` blob. That blob is what the Backtest page
renders — backtest results live on that page only.

Concurrency: the engine is single-threaded (one announcement at
a time, in chronological order). For 1 year of NSE/BSE filings
at ~50-100 filings/day, that's ~30k announcements — linear in N,
target <2 min.

Public surface:

  BacktestConfig        — name, strategy_id, broker_account_id,
                          start_date, end_date, initial_capital,
                          price_model, run_id
  BacktestEngine        — orchestrator (one BacktestConfig -> one
                          BacktestResult)
  BacktestResult        — final equity curve, trades, metrics
  run_backtest(config)  — convenience wrapper
"""
from __future__ import annotations

import asyncio
import json
import random
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional, Protocol

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.analyzer.rules_engine import (
    RuleMatch,
    evaluate as rules_evaluate,
    get_or_create_default_strategy,
    load_rules_for_strategy,
)
from app.analyzer.schemas import (
    AnalysisResponse,
    KeyNumbers,
    Recommendation,
    Sentiment,
)
from app.backtest.metrics import (
    daily_returns,
    max_drawdown,
    profit_factor,
    sharpe_ratio,
    total_return_pct,
    win_rate,
)
from app.config import get_settings
from app.db.models import (
    Analysis,
    Announcement,
    BacktestRun,
    BrokerAccount,
    Position as PositionRow,
    Signal,
    SignalRule,
    Strategy,
    Trade as TradeRow,
)
from app.execution.base import OrderSide, OrderType
from app.execution.market_data import MarketDataBus
from app.execution.paper import PaperBackend
from app.logging_config import get_logger
from app.risk.engine import RiskEngine

log = get_logger(__name__)


# -------------------------------------------------------------------------
# Price model — pluggable so the operator can swap in a Fyers historical
# loader later. Default is a deterministic seeded random walk.
# -------------------------------------------------------------------------


class PriceModel(Protocol):
    """Strategy for producing a `last_price` for (symbol, when).

    Implementations are synchronous; the engine calls them inline
    on the executor thread. Return None when the symbol isn't
    known (the engine treats that as a "no entry price" risk
    block, same as a missing quote in live trading).
    """

    def price(self, symbol: str, when: datetime) -> Optional[float]: ...


# Deterministic per-symbol base price (used when no other anchor is
# available). Picked to be in the ballpark of real NSE prices.
_DEFAULT_BASE_PRICES: dict[str, float] = {
    "RELIANCE": 2500.0,
    "TCS": 4000.0,
    "INFY": 1500.0,
    "HDFCBANK": 1600.0,
    "ICICIBANK": 1000.0,
    "SBIN": 600.0,
    "BHARTIARTL": 1200.0,
    "ITC": 450.0,
    "HINDUNILVR": 2400.0,
    "LT": 3500.0,
    "AXISBANK": 1100.0,
    "KOTAKBANK": 1800.0,
    "MARUTI": 11000.0,
    "SUNPHARMA": 1700.0,
    "TATAMOTORS": 800.0,
    "WIPRO": 500.0,
    "HCLTECH": 1500.0,
    "TITAN": 3200.0,
    "BAJFINANCE": 7500.0,
    "ASIANPAINT": 2800.0,
}


@dataclass
class SyntheticReturnPriceModel:
    """Deterministic per-(symbol,date) price generator.

    For each (symbol, when):
      base = _DEFAULT_BASE_PRICES.get(symbol, 1000.0)
      seed = hash(symbol) ^ hash(date(when))
      rng  = random.Random(seed)
      # Daily log-return around 0, std 0.02 (2% daily)
      cum  = exp(sum(rng.gauss(0, 0.02) for _ in range(days_since_epoch)))
      price = base * cum

    The deterministic seed means a 30-day backtest always
    produces the SAME price series — making tests reproducible.
    `daily_volatility` and `drift` are configurable so an
    operator can stress-test the engine.

    Limitations (documented in deliverable.md):
      - The drift is per-day, not per-trade, so the engine's
        equity curve reflects mark-to-market at the announcement
        timestamp, not at the close.
      - Symbol-uncorrelated — every stock has the same vol. In
        reality, sector moves are correlated.
      - No corporate actions (splits, dividends).
    """

    daily_volatility: float = 0.02
    drift: float = 0.0002
    base_prices: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Merge operator-supplied base prices on top of defaults.
        merged = dict(_DEFAULT_BASE_PRICES)
        merged.update(self.base_prices)
        self.base_prices = merged

    def price(self, symbol: str, when: datetime) -> Optional[float]:
        base = float(self.base_prices.get(symbol.upper(), 1000.0))
        # Number of days from a fixed epoch to `when`'s UTC date.
        # Using (2020, 1, 1) keeps the integer small.
        epoch = datetime(2020, 1, 1, tzinfo=timezone.utc)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        n_days = max(0, (when.date() - epoch.date()).days)
        # Seed: a stable combination of symbol + date.
        seed_str = f"{symbol.upper()}|{when.date().isoformat()}"
        rng = random.Random(seed_str)
        # Sum of N log-returns with drift.
        log_ret = 0.0
        for _ in range(n_days):
            log_ret += rng.gauss(self.drift, self.daily_volatility)
        import math

        price = base * math.exp(log_ret)
        return float(price)


# -------------------------------------------------------------------------
# Configuration + result types
# -------------------------------------------------------------------------


@dataclass
class BacktestConfig:
    """All the inputs the engine needs.

    `name`              — free-form label; surfaced in the API.
    `strategy_id`       — `strategies.id`; uses default if None.
    `broker_account_id` — `broker_accounts.id`; uses first enabled
                          paper account if None.
    `start_date`        — inclusive. Announcements before this
                          are skipped.
    `end_date`          — inclusive. Announcements after this
                          are skipped.
    `initial_capital`   — starting cash. Used as the portfolio
                          value for risk sizing.
    `price_model`       — pluggable. Default is the synthetic
                          random walk.
    `run_id`            — the `backtest_runs.id` we'll update;
                          if None, no DB persistence happens.
    """

    name: str = "default"
    strategy_id: Optional[int] = None
    broker_account_id: Optional[int] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    initial_capital: float = 100_000.0
    price_model: PriceModel = field(default_factory=SyntheticReturnPriceModel)
    run_id: Optional[int] = None


@dataclass
class BacktestProgress:
    """Reported live during the run (so a long run can log progress)."""

    announcements_total: int = 0
    announcements_processed: int = 0
    signals_generated: int = 0
    trades_filled: int = 0
    trades_blocked: int = 0
    last_decision_at: Optional[datetime] = None


@dataclass
class BacktestResult:
    """What the engine produces. The API returns this (after
    re-reading from DB rows, but the in-process version carries
    the full data)."""

    run_id: Optional[int] = None
    config: Optional[BacktestConfig] = None
    equity_curve: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    signals_generated: int = 0
    blocked_trades: int = 0
    announcements_processed: int = 0
    finished_at: Optional[datetime] = None


# -------------------------------------------------------------------------
# The engine
# -------------------------------------------------------------------------


class BacktestEngine:
    """Orchestrates a single backtest run.

    Construction is cheap — pass a `BacktestConfig` and a session
    factory. The engine does NOT start any background tasks; you
    call `await engine.run()` once and get a `BacktestResult`.

    The engine reuses the existing analyzer / rules / risk /
    paper code by calling them directly (not via the event bus).
    This avoids the latency of inter-task queueing and keeps the
    backtest linear in N.
    """

    def __init__(
        self,
        config: BacktestConfig,
        *,
        session_factory: Optional[Callable[[], Session]] = None,
    ) -> None:
        self._config = config
        from app.db.session import SessionLocal

        # The LIVE session factory. Used read-only (load source data)
        # and to write the final `backtest_runs.results` blob — never
        # for simulated trades/positions/signals/analyses.
        self._session_factory: Callable[[], Session] = session_factory or SessionLocal
        self._md = MarketDataBus()
        # The sandbox (in-memory SQLite) factory + engine. Built in
        # `_run_sync` once the run's strategy/rules/account are known;
        # every simulated row is written here and discarded at the end.
        self._sim_engine: Optional[Engine] = None
        self._sim_factory: Optional[Callable[[], Session]] = None
        # PaperBackend + RiskEngine are constructed in `_run_sync` and
        # bound to the sandbox factory (so their DB reads/writes stay
        # isolated from the live tables).
        self._paper: Optional[PaperBackend] = None
        self._risk: Optional[RiskEngine] = None
        # Pre-pick the strategy + account for routing. Falls back
        # to the default strategy / first enabled paper account.
        self._strategy_id: int = 0
        self._broker_account_id: int = 0

    # -- public --------------------------------------------------------

    async def run(self) -> BacktestResult:
        """Execute the backtest. Returns a populated `BacktestResult`.

        The result is also persisted to `backtest_runs.results`
        when `config.run_id` is set.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._run_sync)

    def run_sync(self) -> BacktestResult:
        """Synchronous entry point — useful in tests that don't
        want to drive the event loop."""
        return self._run_sync()

    # -- internals ------------------------------------------------------

    def _run_sync(self) -> BacktestResult:
        cfg = self._config
        # Validate date range.
        if cfg.start_date is None or cfg.end_date is None:
            raise ValueError("start_date and end_date are required")
        if cfg.start_date > cfg.end_date:
            raise ValueError("start_date must be <= end_date")
        if cfg.initial_capital <= 0:
            raise ValueError("initial_capital must be > 0")

        # Step 1: load context + announcements from the LIVE DB. This
        # is the ONLY time the run reads the live tables, and the only
        # write is lazily creating the shared 'default' strategy when
        # the caller didn't pick one (a config row, not a backtest
        # artefact). Strategy / rules / account are captured as plain
        # snapshots so we can reseed them into the sandbox; the
        # announcements stay as detached ORM rows (expire_on_commit is
        # False, so their loaded attributes survive the session close).
        with self._session_factory() as session:
            if cfg.strategy_id is None:
                strat = get_or_create_default_strategy(session)
                session.commit()
                strategy_id = int(strat.id)
            else:
                strategy_id = int(cfg.strategy_id)
                strat = session.get(Strategy, strategy_id)
                if strat is None:
                    raise ValueError(f"strategy {strategy_id} not found")
            strategy_data = {
                "id": int(strat.id),
                "name": strat.name,
                "description": strat.description,
                "enabled": bool(strat.enabled),
                "config": dict(strat.config) if isinstance(strat.config, dict) else {},
            }
            rules_data = [
                {
                    "id": int(r.id),
                    "strategy_id": int(r.strategy_id),
                    "name": r.name,
                    "priority": int(r.priority),
                    "conditions": r.conditions,
                    "action": r.action,
                    "action_params": r.action_params,
                    "enabled": bool(r.enabled),
                }
                for r in load_rules_for_strategy(session, strategy_id)
            ]
            if cfg.broker_account_id is not None:
                acc = session.get(BrokerAccount, int(cfg.broker_account_id))
                if acc is None:
                    raise ValueError(
                        f"broker_account {cfg.broker_account_id} not found"
                    )
            else:
                acc = (
                    session.query(BrokerAccount)
                    .filter(
                        BrokerAccount.enabled.is_(True),
                        BrokerAccount.paper_mode.is_(True),
                    )
                    .order_by(BrokerAccount.id.asc())
                    .first()
                )
            if acc is not None:
                account_data = {
                    "id": int(acc.id),
                    "name": acc.name,
                    "broker": acc.broker,
                    "paper_mode": bool(acc.paper_mode),
                    "enabled": bool(acc.enabled),
                }
            else:
                # No paper account configured — synthesise one for the
                # sandbox ONLY (never written to the live DB, so a
                # backtest on a clean install doesn't create junk
                # broker accounts).
                account_data = {
                    "id": 1_000_000,
                    "name": f"backtest-paper-{uuid.uuid4().hex[:8]}",
                    "broker": "paper",
                    "paper_mode": True,
                    "enabled": True,
                }
            self._strategy_id = strategy_id
            self._broker_account_id = int(account_data["id"])
            announcements: list[Announcement] = list(
                session.execute(
                    select(Announcement)
                    .where(
                        Announcement.filed_at >= datetime.combine(
                            cfg.start_date, datetime.min.time(), tzinfo=timezone.utc
                        ),
                        Announcement.filed_at <= datetime.combine(
                            cfg.end_date, datetime.max.time(), tzinfo=timezone.utc
                        ),
                    )
                    .order_by(Announcement.filed_at.asc(), Announcement.id.asc())
                )
                .scalars()
                .all()
            )
            log.info(
                "backtest.loaded",
                announcements=len(announcements),
                strategy_id=strategy_id,
                account_id=self._broker_account_id,
                start_date=cfg.start_date.isoformat(),
                end_date=cfg.end_date.isoformat(),
            )

        # Step 2: build the isolated sandbox DB and wire the paper +
        # risk engines (and every replay-phase DB read/write) to it.
        # Nothing past this point touches the live tables until we
        # persist the results blob.
        self._sim_engine, self._sim_factory = _build_sim_session_factory(
            strategy_data=strategy_data,
            rules_data=rules_data,
            account_data=account_data,
        )
        self._paper = PaperBackend(
            market_data=self._md,
            session_factory=self._sim_factory,
            pending_timeout_s=5.0,
        )
        self._risk = RiskEngine(
            market_data=self._md,
            portfolio_value=cfg.initial_capital,
            session_factory=self._sim_factory,
        )
        with self._sim_factory() as sim:
            strategy = sim.get(Strategy, strategy_id)
            account = sim.get(BrokerAccount, int(account_data["id"]))

        try:
            # Step 3: replay loop. For each announcement (in chronological
            # order), synthesise a price, build a deterministic analysis,
            # run rules, run risk, place the paper order, mark-to-market.
            result = BacktestResult(
                run_id=cfg.run_id,
                config=cfg,
                equity_curve=[
                    {
                        "date": cfg.start_date.isoformat(),
                        "equity": float(cfg.initial_capital),
                    }
                ],
                finished_at=datetime.now(timezone.utc),
            )
            result.signals_generated = 0
            result.blocked_trades = 0
            result.announcements_processed = 0

            for ann in announcements:
                try:
                    self._replay_one(ann, strategy, account, result)
                except Exception as e:  # noqa: BLE001
                    # We never want a single bad announcement to abort
                    # the whole run. Log and continue.
                    log.exception(
                        "backtest.replay_failed",
                        announcement_id=ann.id,
                        symbol=ann.symbol,
                    )
                    result.decisions.append(
                        {
                            "announcement_id": ann.id,
                            "symbol": ann.symbol,
                            "filed_at": ann.filed_at.isoformat() if ann.filed_at else None,
                            "error": str(e),
                            "outcome": "error",
                        }
                    )
                result.announcements_processed += 1
                # Mark-to-market at end of each day boundary (cheap
                # because the paper backend caches the last quote).
                self._snapshot_equity(result, ann.filed_at)

            # Step 4: final equity point at end_date, then compute metrics.
            result.equity_curve.append(
                {
                    "date": cfg.end_date.isoformat(),
                    "equity": self._compute_equity(cfg.initial_capital),
                }
            )
            eq_values = [float(p["equity"]) for p in result.equity_curve]
            result.metrics = _build_metrics(
                initial_capital=cfg.initial_capital,
                equity=eq_values,
                trades=result.trades,
                signals_generated=result.signals_generated,
                blocked_trades=result.blocked_trades,
            )

            # Step 5: persist into backtest_runs.results (live DB) if we
            # have a run id. This is the ONLY backtest output that
            # reaches the live DB — the Backtest page reads it back.
            if cfg.run_id is not None:
                self._persist_result(result, status="done")

            log.info(
                "backtest.done",
                announcements=result.announcements_processed,
                signals=result.signals_generated,
                trades=len(result.trades),
                final_equity=eq_values[-1] if eq_values else cfg.initial_capital,
            )
            return result
        finally:
            # Discard the sandbox DB — its analyses / signals / trades /
            # positions exist only for the duration of this run.
            self._dispose_sim()

    # -- per-announcement replay --------------------------------------

    def _replay_one(
        self,
        ann: Announcement,
        strategy: Strategy,
        account: BrokerAccount,
        result: BacktestResult,
    ) -> None:
        cfg = self._config
        # Synthesise a price.
        price = cfg.price_model.price(ann.symbol, ann.filed_at or datetime.now(timezone.utc))
        if price is None or float(price) <= 0:
            result.blocked_trades += 1
            result.decisions.append(
                {
                    "announcement_id": ann.id,
                    "symbol": ann.symbol,
                    "filed_at": ann.filed_at.isoformat() if ann.filed_at else None,
                    "outcome": "blocked",
                    "reason": "no_price_available",
                }
            )
            return
        # Push the price to the market data bus so the paper
        # backend can fill the order at it.
        self._md.set_quote_sync(ann.symbol, float(price))

        # Build a deterministic analysis. (We don't call DeepSeek
        # — the backtest's "what-if" is for the rules + risk + paper
        # path, not the LLM.)
        analysis_dict = _synthesise_analysis(ann, price)
        # Write a minimal analyses row into the SANDBOX so the rules
        # engine has something to evaluate. It is discarded with the
        # sandbox at the end of the run — it never reaches the live
        # `analyses` table the dashboard reads.
        with self._sim_factory() as session:
            a = Analysis(
                announcement_id=ann.id,
                model="backtest-stub",
                sentiment=analysis_dict["sentiment"],
                sentiment_score=analysis_dict["sentiment_score"],
                confidence=analysis_dict["confidence"],
                recommendation=analysis_dict["recommendation"],
                rationale=analysis_dict["rationale"],
                deal_value_inr_crore=analysis_dict.get("deal_value_inr_crore"),
                raw_response={"backtest": True, "synthesised_price": price},
            )
            session.add(a)
            session.flush()
            analysis_id = int(a.id)
            rules = load_rules_for_strategy(session, strategy.id)
            eval_ctx = dict(analysis_dict)
            eval_ctx["symbol"] = ann.symbol
            eval_ctx["exchange"] = ann.exchange
            match: RuleMatch = rules_evaluate(eval_ctx, rules)
            # Compute trade levels from the deterministic stub
            # (same formula T3's service uses).
            levels = _compute_trade_levels(analysis_dict, price)
            # Position size from action_params, defaulting to the
            # global MAX_SINGLE_POSITION_PCT.
            settings = get_settings()
            position_size_pct = float(
                (match.action_params or {}).get("position_size_pct")
                or settings.MAX_SINGLE_POSITION_PCT
            )

            signal = Signal(
                analysis_id=analysis_id,
                strategy_id=strategy.id,
                rule_id=match.rule_id,
                symbol=ann.symbol,
                action=match.action,
                confidence=analysis_dict["confidence"],
                position_size_pct=position_size_pct if match.action in {"BUY", "SELL"} else 0.0,
                rationale=(
                    f"backtest: {match.rationale or ''} "
                    f"entry={levels['entry']:.2f} sl={levels['stop_loss']:.2f} "
                    f"target={levels['target']:.2f} rr={levels['rr']:.2f}"
                ).strip(),
                status="pending",
            )
            session.add(signal)
            session.flush()
            signal_id = int(signal.id)
            result.signals_generated += 1

        # If the rule produced HOLD/BLOCK, don't run risk — record
        # and move on. The T3 service also treats HOLD as "not approved".
        if match.action not in {"BUY", "SELL"}:
            result.blocked_trades += 1
            with self._sim_factory() as session:
                s = session.get(Signal, signal_id)
                if s is not None:
                    s.status = "blocked"
                    session.add(s)
                    session.commit()
            result.decisions.append(
                {
                    "announcement_id": ann.id,
                    "signal_id": signal_id,
                    "symbol": ann.symbol,
                    "filed_at": ann.filed_at.isoformat() if ann.filed_at else None,
                    "outcome": "blocked",
                    "reason": f"rule_action={match.action}",
                    "rule_id": match.rule_id,
                    "synthesised_price": price,
                }
            )
            return

        # Build a transient Signal-shaped object for the risk
        # engine + paper backend (they read attributes, not the
        # DB row's transient state).
        signal_for_risk = _SignalProxy(
            id=signal_id,
            symbol=ann.symbol,
            action=match.action,
            confidence=analysis_dict["confidence"],
            strategy_id=strategy.id,
        )

        # Run the risk engine.
        decision = self._run_risk(
            signal=signal_for_risk,
            strategy=strategy,
            account=account,
            entry=levels["entry"],
            stop_loss=levels["stop_loss"],
            target=levels["target"],
        )
        if not decision.approved:
            result.blocked_trades += 1
            with self._sim_factory() as session:
                s = session.get(Signal, signal_id)
                if s is not None:
                    s.status = "blocked"
                    session.add(s)
                    session.commit()
            result.decisions.append(
                {
                    "announcement_id": ann.id,
                    "signal_id": signal_id,
                    "symbol": ann.symbol,
                    "filed_at": ann.filed_at.isoformat() if ann.filed_at else None,
                    "outcome": "blocked",
                    "reason": decision.codes[0] if decision.codes else "RISK_BLOCKED",
                    "rule_id": match.rule_id,
                    "synthesised_price": price,
                    "sizing": (
                        {
                            "qty": decision.sizing.qty,
                            "risk_amount": decision.sizing.risk_amount,
                        }
                        if decision.sizing is not None
                        else None
                    ),
                }
            )
            return

        # Risk approved — place the paper order.
        side = OrderSide.BUY if match.action == "BUY" else OrderSide.SELL
        # The paper backend runs an asyncio loop; we run the
        # coroutine inline. We're already in an executor, but
        # paper's place_order is short-lived and uses its own
        # loop only to schedule DB writes.
        order = asyncio.run(
            self._paper.place_order(
                signal=signal_for_risk,
                symbol=ann.symbol,
                side=side,
                quantity=int(decision.sizing.qty),
                order_type=OrderType.MARKET,
            )
        )
        # Read the Trade row the paper backend just wrote to the
        # sandbox so we can record the realised PnL in the result blob.
        with self._sim_factory() as session:
            t = (
                session.query(TradeRow)
                .filter_by(broker_order_id=order.broker_order_id)
                .one_or_none()
            )
            trade_dict: dict[str, Any] = {
                "broker_order_id": order.broker_order_id,
                "symbol": order.symbol,
                "side": order.side.value,
                "quantity": int(order.filled_quantity or order.quantity),
                "price": float(order.average_price or price),
                "order_type": order.order_type.value,
                "status": order.state.value,
                "pnl": float(t.pnl) if t is not None and t.pnl is not None else None,
                "executed_at": (
                    t.executed_at.isoformat() if t is not None and t.executed_at else None
                ),
                "signal_id": signal_id,
                "announcement_id": ann.id,
                "filed_at": ann.filed_at.isoformat() if ann.filed_at else None,
                "synthesised_price": price,
            }
            result.trades.append(trade_dict)
            s = session.get(Signal, signal_id)
            if s is not None:
                s.status = "filled" if order.state.value == "FILLED" else "pending"
                session.add(s)
                session.commit()
        result.decisions.append(
            {
                "announcement_id": ann.id,
                "signal_id": signal_id,
                "symbol": ann.symbol,
                "filed_at": ann.filed_at.isoformat() if ann.filed_at else None,
                "outcome": "filled" if order.state.value == "FILLED" else "pending",
                "rule_id": match.rule_id,
                "synthesised_price": price,
                "fill_price": float(order.average_price or price),
                "quantity": int(order.filled_quantity or order.quantity),
                "pnl": trade_dict["pnl"],
            }
        )

    # -- helpers -------------------------------------------------------

    def _snapshot_equity(self, result: BacktestResult, when: Optional[datetime]) -> None:
        """Append an equity point at `when` (date only) if it's a new day.

        Cheap: we just compare date strings.
        """
        if when is None:
            return
        d = when.date().isoformat()
        if result.equity_curve and result.equity_curve[-1].get("date") == d:
            return
        result.equity_curve.append(
            {"date": d, "equity": self._compute_equity(self._config.initial_capital)}
        )

    def _compute_equity(self, initial_capital: float) -> float:
        """cash + sum(qty * last_price) for every open position.

        v1 has no separate cash tracking; we treat
        `initial_capital + sum(realised PnL)` as cash and add
        mark-to-market on top. Both the realised PnL and the open
        positions are read from the sandbox, which holds only this
        run's simulated trades — so the figure can't be contaminated
        by (or contaminate) the live book.
        """
        cash = float(initial_capital)
        with self._sim_factory() as session:
            # Sum realised PnL across the sandbox's trades.
            from sqlalchemy import func

            realised = float(
                session.execute(
                    select(func.coalesce(func.sum(TradeRow.pnl), 0.0)).where(
                        TradeRow.pnl.is_not(None),
                    )
                ).scalar_one()
                or 0.0
            )
            cash += realised
            positions = session.query(PositionRow).all()
            for p in positions:
                if int(p.quantity) == 0:
                    continue
                # Use the cached quote; fall back to average_price.
                quote = self._md._quotes.get(p.symbol)
                price = (
                    float(quote.last_price)
                    if quote is not None
                    else float(p.average_price or 0.0)
                )
                cash += int(p.quantity) * price
        return float(cash)

    def _run_risk(self, *, signal, strategy, account, entry, stop_loss, target):
        """Run the risk engine synchronously (we're already in an executor)."""
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                self._risk.evaluate(
                    signal=signal,
                    strategy=strategy,
                    account=account,
                    entry=entry,
                    stop_loss=stop_loss,
                    target=target,
                )
            )
        finally:
            loop.close()

    def _persist_result(self, result: BacktestResult, *, status: str) -> None:
        # Writes the LIVE DB (`self._session_factory`), not the sandbox:
        # the `backtest_runs` row lives in the real database and this
        # blob is what the Backtest page renders.
        with self._session_factory() as session:
            run = session.get(BacktestRun, result.run_id)
            if run is None:
                return
            run.status = status
            run.finished_at = result.finished_at
            run.results = {
                "metrics": result.metrics,
                "equity_curve": result.equity_curve,
                "trades": result.trades,
                "decisions": result.decisions,
                "signals_generated": result.signals_generated,
                "blocked_trades": result.blocked_trades,
                "announcements_processed": result.announcements_processed,
            }
            session.add(run)
            session.commit()

    def _dispose_sim(self) -> None:
        """Tear down the per-run sandbox DB (drops the in-memory data)."""
        eng, self._sim_engine = self._sim_engine, None
        self._sim_factory = None
        self._paper = None
        self._risk = None
        if eng is not None:
            try:
                eng.dispose()
            except Exception:  # noqa: BLE001
                log.exception("backtest.sim_dispose_failed")


# -------------------------------------------------------------------------
# Helpers — outside the class so they're easy to test in isolation.
# -------------------------------------------------------------------------


@dataclass
class _SignalProxy:
    """A transient duck-typed Signal for the risk engine + paper
    backend. Mirrors the attributes they read off the ORM row.
    """

    id: int
    symbol: str
    action: str
    confidence: Optional[float]
    strategy_id: Optional[int] = None


def _build_sim_session_factory(
    *,
    strategy_data: dict[str, Any],
    rules_data: list[dict[str, Any]],
    account_data: dict[str, Any],
) -> tuple[Engine, Callable[[], Session]]:
    """Create an isolated in-memory SQLite **sandbox** for one run.

    Every simulated analysis / signal / trade / position the engine
    produces is written here — never the live DB. The sandbox is
    seeded with copies of the strategy, its rules, and the broker
    account (same ids as the live rows) so the rules + risk engines
    see exactly the configuration the live system would, but in a
    throwaway database. This is what keeps backtest results confined
    to the Backtest page.

    Foreign keys are intentionally left OFF (we don't run the SQLite
    `PRAGMA foreign_keys=ON` here): the sandbox holds copies of the
    config rows but NOT the announcements, so a synthesised `analyses`
    row references an announcement id that exists only in the live DB.
    With FK enforcement off, those orphan-parent rows are fine.
    """
    from app.db.session import Base
    import app.db.models  # noqa: F401 — ensure every table is registered

    eng = create_engine(
        "sqlite://",  # in-memory
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(eng)
    factory = sessionmaker(
        bind=eng,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        class_=Session,
    )
    with factory() as s:
        s.add(
            Strategy(
                id=strategy_data["id"],
                name=strategy_data["name"],
                description=strategy_data.get("description"),
                enabled=bool(strategy_data.get("enabled", True)),
                config=strategy_data.get("config") or {},
            )
        )
        for r in rules_data:
            s.add(
                SignalRule(
                    id=r["id"],
                    strategy_id=r["strategy_id"],
                    name=r["name"],
                    priority=r["priority"],
                    conditions=r["conditions"],
                    action=r["action"],
                    action_params=r.get("action_params"),
                    enabled=bool(r.get("enabled", True)),
                )
            )
        s.add(
            BrokerAccount(
                id=account_data["id"],
                name=account_data["name"],
                broker=account_data.get("broker", "paper"),
                paper_mode=bool(account_data.get("paper_mode", True)),
                enabled=bool(account_data.get("enabled", True)),
            )
        )
        s.commit()
    return eng, factory


def _synthesise_analysis(ann: Announcement, price: float) -> dict[str, Any]:
    """Build a deterministic analysis dict for the backtest.

    The heuristics are intentionally simple — the backtest's
    "what-if" is about the rules + risk + paper path, not the
    LLM. The headlines we synthesise mirror what DeepSeek would
    produce for a real filing with the same metadata.

    The headline's keywords drive sentiment + recommendation; the
    `event_type` is set to the announcement's stored event_type
    (or 'OTHER' if absent).
    """
    title = (ann.headline or "").lower()
    # Defaults — "neutral, hold".
    sentiment = "neutral"
    sentiment_score = 0.0
    confidence = 0.6
    recommendation = "HOLD"
    rationale = f"Backtest synthesised analysis for {ann.symbol}."
    # Heuristics — order matters (first match wins). We use
    # looser keywords than T3's analyzer so a single headline
    # like "RELIANCE wins a Rs 5000 crore order" matches the
    # "positive / BUY" bucket.
    if (
        "order win" in title
        or "wins order" in title
        or "wins a rs" in title
        or "secures order" in title
        or "wins a " in title
        or "new order" in title
    ):
        sentiment = "positive"
        sentiment_score = 75.0
        confidence = 0.85
        recommendation = "BUY"
        rationale = "Material order win; synthesised positive view."
    elif "dividend" in title or "interim dividend" in title:
        sentiment = "positive"
        sentiment_score = 30.0
        confidence = 0.7
        recommendation = "BUY"
        rationale = "Dividend announcement; synthesised positive view."
    elif "acquisition" in title or "acquires" in title or "to acquire" in title:
        sentiment = "positive"
        sentiment_score = 50.0
        confidence = 0.75
        recommendation = "BUY"
        rationale = "Acquisition; synthesised positive view."
    elif (
        "loss" in title
        or "downgrade" in title
        or "penalty" in title
        or "down " in title
        or "miss" in title
    ):
        sentiment = "negative"
        sentiment_score = -50.0
        confidence = 0.7
        recommendation = "SELL"
        rationale = "Negative news; synthesised negative view."
    elif "buyback" in title or "share buyback" in title:
        sentiment = "positive"
        sentiment_score = 60.0
        confidence = 0.8
        recommendation = "BUY"
        rationale = "Buyback; synthesised positive view."

    # Synthesise a deal_value_inr_crore that scales with the
    # synthesised price (gives the rules engine something
    # interesting to filter on).
    deal_value_inr_crore = max(1.0, round(price / 100.0, 2))
    return {
        "event_type": ann.event_type or "OTHER",
        "sentiment": sentiment,
        "sentiment_score": sentiment_score,
        "confidence": confidence,
        "recommendation": recommendation,
        "rationale": rationale,
        "deal_value_inr_crore": deal_value_inr_crore,
        "stake_change_pct": None,
        "dividend_per_share": None,
        "buyback_value_inr_crore": None,
    }


def _compute_trade_levels(
    analysis_dict: dict[str, Any], price: float
) -> dict[str, float]:
    """Mirror T3's deterministic stub.

    SL = 5% on the wrong side, target = 15% on the right side,
    RR = 3.0. These constants are intentional (deterministic
    across the backtest) — the engine reports the values so
    the operator can verify the math.
    """
    sl_dist = 0.05
    target_dist = 0.15
    rec = analysis_dict.get("recommendation", "BUY")
    if rec == "BUY":
        sl = float(price) * (1.0 - sl_dist)
        target = float(price) * (1.0 + target_dist)
    elif rec == "SELL":
        sl = float(price) * (1.0 + sl_dist)
        target = float(price) * (1.0 - target_dist)
    else:
        sl = float(price) * 0.95
        target = float(price) * 1.15
    return {
        "entry": float(price),
        "stop_loss": sl,
        "target": target,
        "rr": target_dist / sl_dist,
    }


def _build_metrics(
    *,
    initial_capital: float,
    equity: list[float],
    trades: list[dict[str, Any]],
    signals_generated: int,
    blocked_trades: int,
) -> dict[str, Any]:
    """Compute the metrics for the result. Thin wrapper around
    `metrics.summarise`."""
    from app.backtest.metrics import summarise

    return summarise(
        initial_capital=initial_capital,
        equity_curve=equity,
        trades=trades,
        signals_generated=signals_generated,
        blocked_trades=blocked_trades,
    )


# -------------------------------------------------------------------------
# Convenience
# -------------------------------------------------------------------------


async def run_backtest(config: BacktestConfig) -> BacktestResult:
    """One-liner: `await run_backtest(cfg)`."""
    engine = BacktestEngine(config)
    return await engine.run()
