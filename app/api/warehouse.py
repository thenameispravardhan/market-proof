"""GET/POST /api/warehouse — search and query the unified announcement dataset.

Every endpoint reads through a READ-ONLY DuckDB connection, so nothing served
here can modify the dataset or block ingestion.

Result sets are capped: 289,042 rows x 91 columns is not something to hand a
browser, and an unbounded ORDER BY on a 2 GB box is how you OOM a trading
process. Every list endpoint paginates and every query is wrapped in a LIMIT.
"""
from __future__ import annotations

import re
import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.logging_config import get_logger
from app.services.warehouse_store import STORE, TABLE

log = get_logger(__name__)
router = APIRouter(prefix="/api/warehouse", tags=["warehouse"])

MAX_LIMIT = 500
QUERY_TIMEOUT_S = 15

# Columns the browser gets by default. The full 91 are available per-row.
LIST_COLUMNS = [
    "uid", "symbol", "company", "cap_tier", "announced_at", "category",
    "headline", "ai_event_type", "ai_sentiment", "ai_sentiment_score",
    "ai_confidence", "ai_recommendation", "adj_30m", "mover_1_5",
    "price_status", "source_layer",
]


def _con():
    """A fresh read-only connection per request.

    Read-only is the whole safety story here: the API physically cannot write to
    the dataset, so a bad query can waste time but never damage data.
    """
    import duckdb

    if not STORE.exists():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "warehouse not built — run app.services.warehouse_store")
    con = duckdb.connect(str(STORE), read_only=True)
    con.execute("SET memory_limit='256MB'; SET threads=2;")
    return con


@router.get("/stats")
def stats() -> dict[str, Any]:
    """Overview of what is in the dataset."""
    con = _con()
    try:
        one = lambda s: con.execute(s).fetchone()  # noqa: E731
        return {
            "rows": one(f"select count(*) from {TABLE}")[0],
            "size_mb": round(STORE.stat().st_size / 1e6, 1),
            "date_range": [str(x) for x in
                           one(f"select min(announced_at), max(announced_at) from {TABLE}")],
            "by_source": dict(con.execute(
                f"select source_layer, count(*) from {TABLE} group by 1").fetchall()),
            "by_exchange": dict(con.execute(
                f"select coalesce(exchange,'?'), count(*) from {TABLE} group by 1").fetchall()),
            "price_status": dict(con.execute(
                f"select price_status, count(*) from {TABLE} group by 1").fetchall()),
            "with_ai_label": one(
                f"select count(*) from {TABLE} where ai_sentiment is not null")[0],
            "movers": one(f"select count(*) from {TABLE} where mover_1_5")[0],
            "symbols": one(f"select count(distinct symbol) from {TABLE}")[0],
            "top_event_types": dict(con.execute(
                f"select ai_event_type, count(*) n from {TABLE} "
                "where ai_event_type is not null group by 1 order by n desc limit 10"
            ).fetchall()),
        }
    finally:
        con.close()


@router.get("/search")
def search(
    q: Optional[str] = Query(None, description="free text over headline / company / summary"),
    symbol: Optional[str] = None,
    exchange: Optional[str] = None,
    event_type: Optional[str] = None,
    sentiment: Optional[str] = None,
    cap_tier: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    movers_only: bool = False,
    min_score: Optional[float] = Query(None, description="min |ai_sentiment_score|"),
    min_move: Optional[float] = Query(None, description="min |adj_30m| %"),
    order_by: str = Query("announced_at", pattern="^[a-z_0-9]+$"),
    desc: bool = True,
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Filtered, paginated search. Every filter is optional and they compose."""
    where: list[str] = []
    params: list[Any] = []

    if q:
        where.append("(headline ILIKE ? OR company ILIKE ? OR ai_summary ILIKE ?)")
        params += [f"%{q}%"] * 3
    for col, val in (("symbol", symbol), ("exchange", exchange),
                     ("ai_event_type", event_type), ("ai_sentiment", sentiment),
                     ("cap_tier", cap_tier)):
        if val:
            where.append(f"upper({col}) = upper(?)")
            params.append(val)
    if date_from:
        where.append("announced_at >= ?")
        params.append(date_from)
    if date_to:
        where.append("announced_at <= ?")
        params.append(date_to)
    if movers_only:
        where.append("mover_1_5")
    if min_score is not None:
        where.append("abs(ai_sentiment_score) >= ?")
        params.append(min_score)
    if min_move is not None:
        where.append("abs(adj_30m) >= ?")
        params.append(min_move)

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    con = _con()
    try:
        cols = {r[0] for r in con.execute(f"describe {TABLE}").fetchall()}
        if order_by not in cols:
            raise HTTPException(422, f"cannot order by unknown column {order_by!r}")
        t = time.time()
        total = con.execute(f"SELECT count(*) FROM {TABLE} {clause}", params).fetchone()[0]
        rows = con.execute(
            f"SELECT {', '.join(LIST_COLUMNS)} FROM {TABLE} {clause} "
            f"ORDER BY {order_by} {'DESC' if desc else 'ASC'} NULLS LAST "
            f"LIMIT {limit} OFFSET {offset}", params).fetch_df()
        return {"total": total, "limit": limit, "offset": offset,
                "elapsed_ms": round((time.time() - t) * 1000, 1),
                "rows": rows.to_dict(orient="records")}
    finally:
        con.close()


@router.get("/row/{uid}")
def row(uid: str) -> dict[str, Any]:
    """Every column for one announcement."""
    con = _con()
    try:
        d = con.execute(f"SELECT * FROM {TABLE} WHERE uid = ?", [uid]).fetch_df()
        if d.empty:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no announcement {uid!r}")
        rec = d.to_dict(orient="records")[0]
        return {k: (None if v != v else v) for k, v in rec.items()}  # NaN -> None
    finally:
        con.close()


@router.get("/symbol/{symbol}")
def by_symbol(symbol: str, limit: int = Query(100, ge=1, le=MAX_LIMIT)) -> dict[str, Any]:
    """One symbol's announcement history plus its realised-move summary."""
    con = _con()
    try:
        rows = con.execute(
            f"SELECT {', '.join(LIST_COLUMNS)} FROM {TABLE} WHERE upper(symbol)=upper(?) "
            f"ORDER BY announced_at DESC LIMIT {limit}", [symbol]).fetch_df()
        agg = con.execute(
            f"SELECT count(*) n, count(adj_30m) with_outcome, avg(adj_30m) avg_move, "
            f"sum(CASE WHEN mover_1_5 THEN 1 ELSE 0 END) movers "
            f"FROM {TABLE} WHERE upper(symbol)=upper(?)", [symbol]).fetch_df()
        return {"symbol": symbol.upper(),
                "summary": agg.to_dict(orient="records")[0],
                "rows": rows.to_dict(orient="records")}
    finally:
        con.close()


@router.get("/aggregate")
def aggregate(
    group_by: str = Query("ai_event_type", pattern="^[a-z_0-9]+$"),
    min_count: int = Query(30, ge=1),
) -> dict[str, Any]:
    """Realised-outcome statistics grouped by any column. This is the endpoint
    that answers 'which event types actually move the stock'."""
    con = _con()
    try:
        cols = {r[0] for r in con.execute(f"describe {TABLE}").fetchall()}
        if group_by not in cols:
            raise HTTPException(422, f"unknown column {group_by!r}")
        rows = con.execute(f"""
            SELECT {group_by} AS grp, count(*) n,
                   count(adj_30m) with_outcome,
                   round(avg(adj_30m), 4) avg_adj_30m,
                   round(median(abs(adj_30m)), 4) median_abs_move,
                   round(100.0 * avg(CASE WHEN mover_1_5 THEN 1 ELSE 0 END), 2) pct_movers,
                   round(100.0 * avg(CASE WHEN adj_30m > 0 THEN 1 ELSE 0 END), 2) pct_up
            FROM {TABLE}
            WHERE {group_by} IS NOT NULL
            GROUP BY 1 HAVING count(*) >= {min_count}
            ORDER BY n DESC LIMIT 100
        """).fetch_df()
        return {"group_by": group_by, "rows": rows.to_dict(orient="records")}
    finally:
        con.close()


class SqlRequest(BaseModel):
    sql: str = Field(..., min_length=8, max_length=4000)
    limit: int = Field(200, ge=1, le=MAX_LIMIT)


# Anything that could write, attach another database, or reach the filesystem.
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|copy|export|import|"
    r"install|load|pragma|set|call|read_csv|read_parquet|read_json)\b", re.I)


@router.post("/query")
def sql_query(body: SqlRequest) -> dict[str, Any]:
    """Run a read-only SELECT against the dataset.

    The connection is already read-only, so this cannot mutate anything. The
    keyword screen is the second layer: it stops ATTACH/COPY/read_parquet, which
    on a read-only connection could still reach other files on the box.
    """
    sql = body.sql.strip().rstrip(";")
    if not re.match(r"^\s*(select|with)\b", sql, re.I):
        raise HTTPException(422, "only SELECT / WITH queries are allowed")
    if _FORBIDDEN.search(sql):
        raise HTTPException(422, "query contains a forbidden keyword")

    con = _con()
    try:
        t = time.time()
        rows = con.execute(f"SELECT * FROM ({sql}) LIMIT {body.limit}").fetch_df()
        return {"elapsed_ms": round((time.time() - t) * 1000, 1),
                "row_count": len(rows),
                "columns": list(rows.columns),
                "rows": rows.to_dict(orient="records")}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — surface DuckDB's own message
        raise HTTPException(422, f"query failed: {str(e)[:300]}") from e
    finally:
        con.close()


@router.get("/columns")
def columns() -> dict[str, Any]:
    """The schema, so the UI can build filters without hard-coding column names."""
    con = _con()
    try:
        d = con.execute(f"describe {TABLE}").fetchall()
        return {"count": len(d),
                "columns": [{"name": r[0], "type": r[1]} for r in d],
                "list_columns": LIST_COLUMNS}
    finally:
        con.close()
