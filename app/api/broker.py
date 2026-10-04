"""`/api/broker/*` — read-only views of the connected Fyers ACCOUNT for the
Trade page's account manager, plus GTT cancel.

  GET  /api/broker/profile      client id / name / email
  GET  /api/broker/funds        the `/funds` fund_limit table + a keyed summary
  GET  /api/broker/holdings     demat holdings with P&L (day P&L from live quotes)
  GET  /api/broker/gtt          the GTT / OCO order book
  POST /api/broker/gtt/cancel   cancel one GTT order (real-money account; audited)

(`GET /api/broker/book` — today's orders + net positions — lives in
app/api/market.py.)

The reads keep the market API's "always 200" contract: when no Fyers
account is connected, or the broker call fails, the response is
`{ok: false, reason, …}` with the same keys emptied, so the panel renders
an inline message. They read the CONNECTED account (`_fyers_backend`) —
like market data, they don't need the account's `enabled` trading switch.

Fyers field names come from the v3 docs and can't be verified live from
here, so every parser reads with `.get()` fallbacks and reports a missing
number as None, never as a made-up 0.
"""
from __future__ import annotations

import asyncio
import math
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.market import _fyers_backend
from app.api.orders import _manager, _require_real_account
from app.db.models import AuditLog
from app.db.session import get_db
from app.execution.fyers_live import FyersBlockedError
from app.logging_config import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["broker"])


# ---- parsing helpers -----------------------------------------------------


def _num(v: Any) -> Optional[float]:
    """float(v), or None when absent / unparseable / not finite."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _int(v: Any, default: int = 0) -> int:
    f = _num(v)
    return int(f) if f is not None else default


def _first(*values: Any) -> Any:
    """The first value that is present (not None / blank)."""
    for v in values:
        if v is not None and v != "":
            return v
    return None


def _reason(prefix: str, e: Exception) -> str:
    return f"{prefix}: {e!s}"[:120]


def _side_text(v: Any) -> Optional[str]:
    """Fyers side (1 buy / -1 sell, or text) → "BUY" / "SELL"; None when
    unrecognised (never guessed)."""
    n = _num(v)
    if n is not None:
        return "BUY" if n > 0 else "SELL" if n < 0 else None
    s = str(v or "").strip().upper()
    return {"BUY": "BUY", "B": "BUY", "SELL": "SELL", "S": "SELL"}.get(s)


# ---- profile -------------------------------------------------------------


@router.get("/api/broker/profile")
async def broker_profile() -> dict[str, Any]:
    """Who the connected Fyers login is: client id, name, email. Always 200."""
    shell: dict[str, Any] = {"client_id": None, "name": None, "email": None}
    backend = _fyers_backend()
    if backend is None or not hasattr(backend, "get_profile"):
        return {"ok": False, "reason": "connect a Fyers account to see the broker profile", **shell}
    try:
        data = await backend.get_profile()
    except Exception as e:  # noqa: BLE001
        log.warning("broker.profile_failed", error=str(e))
        return {"ok": False, "reason": _reason("Fyers profile unavailable", e), **shell}
    data = data if isinstance(data, dict) else {}
    client_id = _first(data.get("fy_id"), data.get("client_id"))
    return {
        "ok": True,
        "client_id": str(client_id) if client_id is not None else None,
        "name": _first(data.get("name"), data.get("display_name")),
        "email": _first(data.get("email_id"), data.get("email")),
    }


# ---- funds ---------------------------------------------------------------


# Fyers `/funds` fund_limit ids → summary keys. The title needles are the
# fallback for a row that arrives without its id.
_FUND_SUMMARY: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (1, "total_balance", ("total balance",)),
    (2, "utilized", ("utilized", "utilised")),
    (3, "clear_balance", ("clear balance",)),
    (4, "realized_pnl", ("realized profit", "realised profit")),
    (5, "collateral", ("collateral",)),
    (6, "fund_transfer", ("fund transfer",)),
    (7, "receivables", ("receivable",)),
    (8, "adhoc_limit", ("adhoc", "ad hoc")),
    (9, "limit_start", ("start of the day",)),
    (10, "available", ("available",)),
)


def _fund_summary(rows: list[dict[str, Any]]) -> dict[str, Optional[float]]:
    """The equity amount of each known fund_limit row, keyed by name:
    matched by Fyers id first, then by title. Missing → None."""
    by_id: dict[int, dict[str, Any]] = {}
    for r in rows:
        rid = _num(r.get("id"))
        if rid is not None:
            by_id.setdefault(int(rid), r)
    out: dict[str, Optional[float]] = {}
    for fid, key, needles in _FUND_SUMMARY:
        row = by_id.get(fid) or next(
            (r for r in rows if any(n in str(r.get("title") or "").lower() for n in needles)),
            None,
        )
        out[key] = _num(row.get("equityAmount")) if row is not None else None
    return out


@router.get("/api/broker/funds")
async def broker_funds() -> dict[str, Any]:
    """The account's fund limits: every `/funds` row (equity + commodity
    amounts, in Fyers' order) and a `summary` of the equity amounts keyed
    by name (total_balance … available). Always 200."""
    shell: dict[str, Any] = {"rows": [], "summary": {key: None for _, key, _ in _FUND_SUMMARY}}
    backend = _fyers_backend()
    if backend is None or not hasattr(backend, "get_fund_rows"):
        return {"ok": False, "reason": "connect a Fyers account to see funds", **shell}
    try:
        raw = await backend.get_fund_rows()
    except Exception as e:  # noqa: BLE001
        log.warning("broker.funds_failed", error=str(e))
        return {"ok": False, "reason": _reason("Fyers funds unavailable", e), **shell}
    rows = [r for r in (raw or []) if isinstance(r, dict)]
    return {
        "ok": True,
        "rows": [
            {
                "id": _int(r.get("id")),
                "title": str(r.get("title") or ""),
                "equity": _num(r.get("equityAmount")),
                "commodity": _num(r.get("commodityAmount")),
            }
            for r in rows
        ],
        "summary": _fund_summary(rows),
    }


# ---- holdings ------------------------------------------------------------


_QUOTE_BATCH = 50  # Fyers /data/quotes takes at most 50 symbols per call


async def _quotes_by_symbol(backend: Any, symbols: list[str]) -> dict[str, Any]:
    """Live quotes keyed by upper-cased symbol, in batches of 50. Never
    raises — a quote failure only leaves the day-P&L columns blank."""
    if not symbols or not hasattr(backend, "get_quote"):
        return {}
    out: dict[str, Any] = {}
    try:
        batches = [symbols[i:i + _QUOTE_BATCH] for i in range(0, len(symbols), _QUOTE_BATCH)]
        results = await asyncio.gather(
            *(backend.get_quote(b) for b in batches), return_exceptions=True
        )
        for res in results:
            if isinstance(res, BaseException):
                log.debug("broker.holdings.quote_failed", error=str(res))
                continue
            for q in res or []:
                sym = str(getattr(q, "symbol", "") or "").upper()
                if sym:
                    out[sym] = q
    except Exception as e:  # noqa: BLE001
        log.debug("broker.holdings.quote_failed", error=str(e))
    return out


def _holding_row(h: dict[str, Any]) -> dict[str, Any]:
    qty = _int(_first(h.get("quantity"), h.get("qty")))
    avg = _num(h.get("costPrice"))
    ltp = _num(h.get("ltp"))
    cost = avg * qty if avg is not None else None
    market_value = _num(h.get("marketVal"))
    if market_value is None and ltp is not None:
        market_value = round(ltp * qty, 2)
    pnl = _num(h.get("pl"))
    if pnl is None and market_value is not None and cost is not None:
        pnl = round(market_value - cost, 2)
    return {
        "symbol": h.get("symbol"),
        "isin": h.get("isin"),
        "qty": qty,
        "t1_qty": _int(h.get("qty_t1")),
        "remaining_qty": _int(h.get("remainingQuantity")),
        "pledged_qty": _int(h.get("remainingPledgeQuantity")),
        "collateral_qty": _int(h.get("collateralQuantity")),
        "avg_price": avg,
        "ltp": ltp,
        "market_value": market_value,
        "cost_value": round(cost, 2) if cost is not None else None,
        "pnl": pnl,
        "pnl_pct": round(pnl / cost * 100, 2) if pnl is not None and cost else None,
        "holding_type": h.get("holdingType"),
        "prev_close": None,
        "day_pnl": None,
    }


def _apply_day_change(row: dict[str, Any], quote: Any) -> None:
    """Fill prev_close / day_pnl from a live quote: the quote's previous
    close, else its last price minus its day change. The holding's own LTP
    (falling back to the quote's) keeps day_pnl consistent with the row."""
    if quote is None:
        return
    live = _num(getattr(quote, "last_price", None))
    prev = _num(getattr(quote, "prev_close", None))
    change = _num(getattr(quote, "change", None))
    if (prev is None or prev <= 0) and change is not None and live:
        prev = live - change
    ltp = row["ltp"] or live
    if prev is None or prev <= 0 or not ltp:
        return
    row["prev_close"] = round(prev, 4)
    row["day_pnl"] = round((ltp - prev) * row["qty"], 2)


def _holdings_overall(ov: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Fyers' `overall` block, falling back to sums over the rows."""
    investment = _num(ov.get("total_investment"))
    if investment is None:
        investment = round(sum(r["cost_value"] or 0.0 for r in rows), 2)
    current = _num(ov.get("total_current_value"))
    if current is None:
        current = round(sum(r["market_value"] or 0.0 for r in rows), 2)
    pnl = _num(ov.get("total_pl"))
    if pnl is None:
        pnl = round(current - investment, 2)
    pnl_pct = _num(ov.get("pnl_perc"))
    if pnl_pct is None and investment:
        pnl_pct = round(pnl / investment * 100, 2)
    days = [r["day_pnl"] for r in rows if r["day_pnl"] is not None]
    return {
        "count": _int(ov.get("count_total"), len(rows)),
        "investment": investment,
        "current_value": current,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "day_pnl": round(sum(days), 2) if days else None,
    }


@router.get("/api/broker/holdings")
async def broker_holdings() -> dict[str, Any]:
    """Demat holdings with overall + per-holding P&L. Day P&L comes from a
    live quote per symbol (previous close); when quotes fail the holdings
    still load with prev_close / day_pnl as None. Always 200."""
    shell: dict[str, Any] = {
        "holdings": [],
        "overall": {
            "count": None, "investment": None, "current_value": None,
            "pnl": None, "pnl_pct": None, "day_pnl": None,
        },
    }
    backend = _fyers_backend()
    if backend is None or not hasattr(backend, "get_holdings"):
        return {"ok": False, "reason": "connect a Fyers account to see holdings", **shell}
    try:
        raw = await backend.get_holdings()
    except Exception as e:  # noqa: BLE001
        log.warning("broker.holdings_failed", error=str(e))
        return {"ok": False, "reason": _reason("Fyers holdings unavailable", e), **shell}
    raw = raw if isinstance(raw, dict) else {}
    rows = [_holding_row(h) for h in (raw.get("holdings") or []) if isinstance(h, dict)]
    symbols = list(dict.fromkeys(str(r["symbol"]).upper() for r in rows if r["symbol"]))
    quotes = await _quotes_by_symbol(backend, symbols)
    for r in rows:
        if r["symbol"]:
            _apply_day_change(r, quotes.get(str(r["symbol"]).upper()))
    overall = raw.get("overall") if isinstance(raw.get("overall"), dict) else {}
    return {"ok": True, "holdings": rows, "overall": _holdings_overall(overall, rows)}


# ---- GTT -----------------------------------------------------------------


def _gtt_status(o: dict[str, Any]) -> Optional[str]:
    """The GTT status as text. Fyers' numeric GTT status codes aren't
    documented anywhere we can check, so a numeric code passes through
    unmapped (as a string) rather than being guessed at."""
    raw = _first(
        o.get("gttStatus"), o.get("status"), o.get("orderStatus"),
        o.get("ord_status"), o.get("gtt_status"),
    )
    if raw is None:
        return None
    s = str(raw).strip()
    return s if _num(s) is not None else s.upper()


def _gtt_row(o: dict[str, Any]) -> dict[str, Any]:
    """One GTT order, normalised. Reads the documented `orderInfo.leg1 /
    leg2` shape ({price, triggerPrice, qty}) and falls back to the flat
    OMS keys (`price_trigger`, `price_limit`, `price2_*`, `qty2`,
    `tran_side`, `product_type`) the raw order book may use instead."""
    info = o.get("orderInfo") if isinstance(o.get("orderInfo"), dict) else {}
    leg1 = info.get("leg1") if isinstance(info.get("leg1"), dict) else {}
    leg2 = info.get("leg2") if isinstance(info.get("leg2"), dict) else {}
    trigger2 = _num(_first(leg2.get("triggerPrice"), o.get("price2_trigger")))
    limit2 = _num(_first(leg2.get("price"), o.get("price2_limit")))
    qty2 = _num(_first(leg2.get("qty"), o.get("qty2")))
    # OCO = a second leg that carries something. A flat row reports a
    # missing leg 2 as zeros, so zeros don't count.
    oco = any((v or 0) > 0 for v in (trigger2, limit2, qty2))
    gtt_id = _first(o.get("id"), o.get("gttId"), o.get("orderNumber"))
    return {
        "id": str(gtt_id) if gtt_id is not None else "",
        "symbol": o.get("symbol"),
        "side": _side_text(_first(o.get("side"), o.get("tran_side"), o.get("transactionType"))),
        "gtt_type": "OCO" if oco else "Single",
        "product": _first(o.get("productType"), o.get("product_type")),
        "status": _gtt_status(o),
        "qty": _int(_first(leg1.get("qty"), o.get("qty"))),
        "trigger": _num(_first(leg1.get("triggerPrice"), o.get("price_trigger"), o.get("triggerPrice"))),
        "limit": _num(_first(leg1.get("price"), o.get("price_limit"), o.get("limitPrice"))),
        "trigger2": trigger2 if oco else None,
        "limit2": limit2 if oco else None,
        "created": _first(
            o.get("orderDateTime"), o.get("createdTime"), o.get("createTime"),
            o.get("create_time"), o.get("created_at"), o.get("time_oms"),
        ),
    }


@router.get("/api/broker/gtt")
async def broker_gtt() -> dict[str, Any]:
    """The account's GTT / OCO orders (Fyers `/gtt/orders`). Always 200."""
    backend = _fyers_backend()
    if backend is None or not hasattr(backend, "get_gtt_orders"):
        return {"ok": False, "reason": "connect a Fyers account to see GTT orders", "orders": []}
    try:
        raw = await backend.get_gtt_orders()
    except Exception as e:  # noqa: BLE001
        log.warning("broker.gtt_failed", error=str(e))
        return {"ok": False, "reason": _reason("Fyers GTT order book unavailable", e), "orders": []}
    return {"ok": True, "orders": [_gtt_row(o) for o in (raw or []) if isinstance(o, dict)]}


@router.post("/api/broker/gtt/cancel")
async def cancel_gtt(
    body: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Cancel one GTT / OCO order.

    Body: `{ "account_id": 2, "id": "25010700000001" }`

    Real-money accounts only (`_require_real_account`), routed through the
    account's live backend like `/api/orders/cancel`, and audited
    (`gtt.manual_cancelled`) whenever the broker answered. Returns
    `{ok, id}`; 503 when the Fyers edge blocks the request, 502 on any
    other broker failure.
    """
    try:
        account_id = int(body["account_id"])
        gtt_id = str(body["id"]).strip()
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(status_code=422, detail=f"missing or bad field: {e}")
    if not gtt_id:
        raise HTTPException(status_code=422, detail="id is required")
    acc = _require_real_account(db, account_id)
    backend = _manager()._manual_backend_for(acc)  # noqa: SLF001 — live backend for the chosen account
    if backend is None:
        raise HTTPException(status_code=400, detail=f"no backend for account {account_id}")
    if not hasattr(backend, "cancel_gtt"):
        raise HTTPException(status_code=400, detail="this broker does not support GTT cancel")
    try:
        ok = await backend.cancel_gtt(gtt_id)
    except FyersBlockedError as e:
        log.warning(
            "manual_gtt.cancel_blocked",
            gtt_id=gtt_id, status_code=e.status_code, reason=e.reason,
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "Fyers edge is blocking the request as a script "
                "(anti-bot / Cloudflare). The GTT order is untouched on "
                "the broker; retry once the server's IP / User-Agent "
                "is accepted. See server logs for the full response."
            ),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("manual_gtt.cancel_failed", gtt_id=gtt_id, error=str(e))
        raise HTTPException(status_code=502, detail=f"broker GTT cancel failed: {e}")
    db.add(AuditLog(
        actor="ui_trade_page",
        action="gtt.manual_cancelled",
        target=f"account:{account_id}",
        before=None,
        after={"gtt_id": gtt_id, "broker_ok": bool(ok)},
    ))
    db.commit()
    return {"ok": bool(ok), "id": gtt_id}
