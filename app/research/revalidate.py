"""Nightly re-validation: does each switched-on strategy still clear the bar?

An edge found last quarter can decay. Every evening (after the close, off
by default — Research tab), each strategy that is switched on (paper or
live) gets:

  * the research check again (app/research/loop.py), its rules as they run
    now, over its card's window rolled forward to today — saved as a
    "nightly" card;
  * the trades since its last passing card was made: data that card never
    saw, the only truly out-of-sample test there is.

It is flagged when the check is killed, when it has lost money since its
card over at least the card's minimum trades, or when it trades LIVE without
ever having passed. A flag stays open until a person clears it; the live
desk's auto-pause reads the open ones (`active_flags`). Nothing here touches
an order or a strategy's switch.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select

from app.logging_config import get_logger

log = get_logger(__name__)

ROOT = Path(__file__).resolve().parents[2] / "data" / "research"
IST = 19800
DAY = 86400
DEFAULT = {"enabled": False, "time": "18:30"}
MIN_DAYS, MAX_DAYS = 120, 3 * 366
MARKET = (9 * 60 + 15, 15 * 60 + 30)


def _path(root: Optional[Path]) -> Path:
    return (root or ROOT) / "nightly.json"


def load_config(root: Optional[Path] = None) -> dict[str, Any]:
    try:
        raw = json.loads(_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    return {**DEFAULT, **{k: v for k, v in raw.items() if k in ("enabled", "time", "last_run", "last_day", "last")}}


def save_config(patch: dict[str, Any], root: Optional[Path] = None) -> dict[str, Any]:
    cfg = load_config(root)
    if "enabled" in patch:
        cfg["enabled"] = bool(patch["enabled"])
    if "time" in patch:
        t = str(patch["time"])
        try:
            h, m = (int(x) for x in t.split(":"))
        except ValueError:
            raise ValueError("time is HH:MM (IST)") from None
        if not (0 <= h < 24 and 0 <= m < 60) or MARKET[0] <= h * 60 + m < MARKET[1] + 15:
            raise ValueError("pick a time outside 09:15-15:45 IST — the check is heavy and must not run in market hours")
        cfg["time"] = f"{h:02d}:{m:02d}"
    for k in ("last_run", "last_day", "last"):
        if k in patch:
            cfg[k] = patch[k]
    p = _path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    tmp.replace(p)
    return cfg


def in_market(now: float) -> bool:
    m = int((now + IST) % DAY) // 60
    return MARKET[0] <= m < MARKET[1] and datetime.fromtimestamp(now + IST, timezone.utc).weekday() < 5


def active_flags(db: Any, strategy_id: Optional[int] = None) -> list[Any]:
    """Open flags (newest first) — for the auto-pause and the Research tab."""
    from app.db.models import AlgoResearchFlag

    q = select(AlgoResearchFlag).where(AlgoResearchFlag.cleared_at.is_(None)).order_by(AlgoResearchFlag.id.desc())
    if strategy_id is not None:
        q = q.where(AlgoResearchFlag.strategy_id == strategy_id)
    return list(db.execute(q).scalars())


def _epoch(dt: Optional[datetime]) -> Optional[int]:
    return int(dt.replace(tzinfo=timezone.utc).timestamp()) if dt else None


def judge(res: dict[str, Any], since: Optional[dict[str, Any]], mode: str, has_card: bool) -> list[str]:
    """Why a strategy is flagged, or [] when it still holds."""
    reasons = []
    if mode == "live" and not has_card:
        reasons.append("trades LIVE without ever passing a research check")
    if res["verdict"] == "kill":
        failed = [c["label"] for c in res["checks"] if c["pass"] is False]
        reasons.append("below the bar on the latest data: " + "; ".join(failed))
    if since and since["trades"] >= res["min_trades"] and since["net_pnl"] <= 0:
        reasons.append(f"lost ₹{-since['net_pnl']:,.0f} over {since['trades']} trades since its passing card")
    return reasons


async def revalidate_one(strategy: Any, now: float, heavy: asyncio.Semaphore) -> dict[str, Any]:
    """One strategy: the check on its latest window, the trades since its
    card, a nightly card, and a flag when it no longer holds."""
    from app.algo import data, engine
    from app.db import session as dbs
    from app.db.models import AlgoResearchCard, AlgoResearchFlag
    from app.research import loop

    sid, name, mode = strategy.id, strategy.name, strategy.mode
    out: dict[str, Any] = {"strategy_id": sid, "name": name, "mode": mode}
    with dbs.SessionLocal() as db:
        base = db.execute(select(AlgoResearchCard).where(
            AlgoResearchCard.strategy_id == sid, AlgoResearchCard.kind == "check", AlgoResearchCard.verdict == "pass")
            .order_by(AlgoResearchCard.id.desc()).limit(1)).scalar_one_or_none()
        setup = dict(base.setup) if base else {}
        card_ts = _epoch(base.created_at) if base else None
    try:
        spec = engine.normalize(strategy.spec)
    except ValueError as e:
        return {**out, "status": "error", "error": f"spec: {e}"}
    span = MAX_DAYS
    if setup.get("start") and setup.get("end"):
        from datetime import date

        span = (date.fromisoformat(setup["end"]) - date.fromisoformat(setup["start"])).days
    span = max(MIN_DAYS, min(MAX_DAYS, span))
    end = int(now - (now + IST) % DAY) + DAY - IST       # the next IST midnight
    start = end - span * DAY
    wf = setup.get("walk_forward") or {}
    min_trades = int(setup.get("min_trades") or 10)
    async with heavy:
        try:
            bars, notes, trade_from = await data.assemble(spec, start, end)
        except ValueError as e:
            return {**out, "status": "error", "error": str(e)[:300]}
        if not bars:
            return {**out, "status": "no data", "error": "; ".join(n["note"] for n in notes)[:300]}
        last = max(v["t"][-1] for v in bars.values())
        oos_from = int(last - (last - trade_from) * float(setup.get("oos_pct") or 30) / 100)
        try:
            res = await asyncio.to_thread(
                loop.check, spec, bars, None, oos_from=oos_from, metric=setup.get("metric") or "net_pnl",
                min_trades=min_trades, trade_from=trade_from, windows=int(wf.get("windows") or 4),
                is_mult=float(wf.get("is_mult") or 3), anchored=bool(wf.get("anchored")),
                prior_trials=max(0, int(strategy.trials or 0) - 1), trades=True)
        except (ValueError, KeyError, IndexError, TypeError, ZeroDivisionError) as e:
            return {**out, "status": "error", "error": str(e)[:300]}
        del bars
    trades = res.pop("trades")
    since = None
    if card_ts:
        after = [x for x in trades if x["entry_t"] >= card_ts]
        st = engine.stats(after, res["capital"])
        since = {"from": card_ts, "trades": st["trades"], "net_pnl": st["net_pnl"], "win_rate": st["win_rate"]}
    res["since_card"] = since
    reasons = judge(res, since, mode, base is not None)
    with dbs.SessionLocal() as db:
        day = lambda e: datetime.fromtimestamp(e + IST, timezone.utc).date().isoformat()  # noqa: E731
        c = AlgoResearchCard(strategy_id=sid, version=strategy.version, name=name, spec=spec, kind="nightly",
                             setup={**setup, "start": day(start), "end": day(end - 1), "min_trades": min_trades,
                                    "oos_pct": float(setup.get("oos_pct") or 30), "grid": []},
                             trials=res["trials"], checks=res.pop("checks"), verdict=res.pop("verdict"), result=res,
                             note="; ".join(reasons) or None)
        db.add(c)
        db.flush()
        new_flag = None
        if reasons and not active_flags(db, sid):
            new_flag = AlgoResearchFlag(strategy_id=sid, card_id=c.id, reason="; ".join(reasons)[:300],
                                        detail={"since_card": since, "mode": mode, "notes": notes[:10]})
            db.add(new_flag)
        db.commit()
        out.update(status="flagged" if reasons else "holds", card_id=c.id, verdict=c.verdict, reasons=reasons,
                   new_flag=new_flag.id if new_flag else None, since_card=since)
    return out


async def run_nightly(now: Optional[float] = None, heavy: Optional[asyncio.Semaphore] = None) -> dict[str, Any]:
    """Every switched-on strategy, one at a time. Refuses in market hours."""
    from app.db import session as dbs
    from app.db.models import AlgoStrategy

    now = time.time() if now is None else now
    if in_market(now):
        raise RuntimeError("not during market hours (09:15-15:30 IST) — the check is heavy")
    if heavy is None:
        from app.api.algo import _heavy as heavy           # one heavy job at a time, with the backtests
    with dbs.SessionLocal() as db:
        targets = list(db.execute(select(AlgoStrategy).where(AlgoStrategy.enabled.is_(True))
                                  .order_by(AlgoStrategy.id)).scalars())
        db.expunge_all()
    t0 = time.monotonic()
    results = []
    for s in targets:
        try:
            results.append(await revalidate_one(s, now, heavy))
        except Exception as e:  # noqa: BLE001 — one strategy's failure must not stop the others
            log.exception("research.nightly_failed", strategy_id=s.id)
            results.append({"strategy_id": s.id, "name": s.name, "status": "error", "error": str(e)[:300]})
    flagged = [r for r in results if r.get("new_flag")]
    if flagged:
        from app.services.event_bus import event_bus

        await event_bus.publish("system.error", {
            "where": "research nightly re-validation",
            "error": "Edge no longer holds — " + " | ".join(f"{r['name']}: {'; '.join(r['reasons'])}" for r in flagged)})
    summary = {"at": int(now), "strategies": len(results), "flagged": sum(1 for r in results if r.get("status") == "flagged"),
               "new_flags": len(flagged), "elapsed_s": round(time.monotonic() - t0, 1), "results": results}
    save_config({"last_run": int(now), "last": {k: v for k, v in summary.items() if k != "results"}})
    log.info("research.nightly_done", strategies=len(results), new_flags=len(flagged))
    return summary


async def tick(now: Optional[float] = None) -> Optional[dict[str, Any]]:
    """The scheduler's minute tick: run once a weekday at the configured time."""
    now = time.time() if now is None else now
    cfg = load_config()
    if not cfg["enabled"]:
        return None
    ist = datetime.fromtimestamp(now + IST, timezone.utc)
    h, m = (int(x) for x in cfg["time"].split(":"))
    today = ist.strftime("%Y-%m-%d")
    if ist.weekday() >= 5 or cfg.get("last_day") == today or ist.hour * 60 + ist.minute < h * 60 + m or in_market(now):
        return None
    save_config({"last_day": today})          # set before the run: a failure must not retry-loop
    return await run_nightly(now)
