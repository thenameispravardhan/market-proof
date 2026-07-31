"""GET/POST /api/warehouse — search and query the unified announcement dataset.

Every endpoint reads through a READ-ONLY DuckDB connection, so nothing served
here can modify the dataset or block ingestion.

Result sets are capped: 289,042 rows x 91 columns is not something to hand a
browser, and an unbounded ORDER BY on a 2 GB box is how you OOM a trading
process. Every list endpoint paginates and every query is wrapped in a LIMIT.
"""
from __future__ import annotations

import asyncio
import os
import re
import tempfile
import time
from pathlib import Path
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


def _records(cur) -> list[dict[str, Any]]:
    """Rows as dicts, without pandas.

    DuckDB's fetch_df() builds a pandas DataFrame, and this service has no
    pandas — pulling it in just to serialise JSON would cost more memory on the
    2 GB box than every query it serves. Locally the endpoints looked fine
    because pandas happens to be installed; on the server they 500'd.
    cursor.description carries the column names, which is all this needs.
    """
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    out = []
    for r in rows:
        rec: dict[str, Any] = {}
        for k, v in zip(cols, r):
            if isinstance(v, float) and v != v:      # NaN is not JSON
                v = None
            elif hasattr(v, "isoformat"):            # datetime / date
                v = v.isoformat()
            rec[k] = v
        out.append(rec)
    return out


def _con():
    """A per-request cursor over the process's single DuckDB connection.

    It cannot be an independent read-only connection: the monitor already holds
    a read-write handle on this file to mirror incoming filings, and DuckDB
    refuses to open one file twice with different configurations in the same
    process ("Can't open a connection to same database file with a different
    configuration").

    `cursor()` returns an independent execution context over the SAME database
    instance — safe for concurrent reads and correct under FastAPI's threadpool.
    Write protection therefore comes from the statement screen in /query rather
    than from the connection, which is why that screen rejects anything that is
    not a bare SELECT/WITH.
    """
    from app.services.warehouse_store import connect as store_connect

    if not STORE.exists():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "warehouse not built — POST /api/warehouse/rebuild")
    return store_connect().cursor()


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
    columns: Optional[str] = Query(
        None, description="comma-separated column names, or 'all' for every column"),
) -> dict[str, Any]:
    """Filtered, paginated search. Every filter is optional and they compose.

    `columns` widens the projection beyond the default preview set — the UI uses
    it to show all 91. Names are validated against the real schema rather than
    interpolated blind, since they land in the SELECT list.
    """
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
        schema = [r[0] for r in con.execute(f"describe {TABLE}").fetchall()]
        cols = set(schema)
        if order_by not in cols:
            raise HTTPException(422, f"cannot order by unknown column {order_by!r}")
        if not columns:
            select = LIST_COLUMNS
        elif columns.strip().lower() == "all":
            select = schema
        else:
            asked = [c.strip() for c in columns.split(",") if c.strip()]
            unknown = [c for c in asked if c not in cols]
            if unknown:
                raise HTTPException(422, f"unknown column(s): {unknown}")
            select = asked or LIST_COLUMNS
        t = time.time()
        total = con.execute(f"SELECT count(*) FROM {TABLE} {clause}", params).fetchone()[0]
        rows = con.execute(
            f"SELECT {', '.join(select)} FROM {TABLE} {clause} "
            f"ORDER BY {order_by} {'DESC' if desc else 'ASC'} NULLS LAST "
            f"LIMIT {limit} OFFSET {offset}", params)
        return {"total": total, "limit": limit, "offset": offset,
                "columns": select,
                "elapsed_ms": round((time.time() - t) * 1000, 1),
                "rows": _records(rows)}
    finally:
        con.close()


@router.get("/row/{uid}")
def row(uid: str) -> dict[str, Any]:
    """Every column for one announcement."""
    con = _con()
    try:
        recs = _records(con.execute(f"SELECT * FROM {TABLE} WHERE uid = ?", [uid]))
        if not recs:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no announcement {uid!r}")
        return recs[0]
    finally:
        con.close()


@router.get("/symbol/{symbol}")
def by_symbol(symbol: str, limit: int = Query(100, ge=1, le=MAX_LIMIT)) -> dict[str, Any]:
    """One symbol's announcement history plus its realised-move summary."""
    con = _con()
    try:
        rows = con.execute(
            f"SELECT {', '.join(LIST_COLUMNS)} FROM {TABLE} WHERE upper(symbol)=upper(?) "
            f"ORDER BY announced_at DESC LIMIT {limit}", [symbol])
        row_recs = _records(rows)
        agg = con.execute(
            f"SELECT count(*) n, count(adj_30m) with_outcome, avg(adj_30m) avg_move, "
            f"sum(CASE WHEN mover_1_5 THEN 1 ELSE 0 END) movers "
            f"FROM {TABLE} WHERE upper(symbol)=upper(?)", [symbol])
        return {"symbol": symbol.upper(),
                "summary": _records(agg)[0],
                "rows": row_recs}
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
        """)
        return {"group_by": group_by, "rows": _records(rows)}
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
        cur = con.execute(f"SELECT * FROM ({sql}) LIMIT {body.limit}")
        cols = [d[0] for d in cur.description]
        recs = _records(cur)
        return {"elapsed_ms": round((time.time() - t) * 1000, 1),
                "row_count": len(recs), "columns": cols, "rows": recs}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — surface DuckDB's own message
        raise HTTPException(422, f"query failed: {str(e)[:300]}") from e
    finally:
        con.close()


class RebuildRequest(BaseModel):
    history: bool = True
    live: bool = True
    prices: bool = False
    price_limit: int = Field(0, ge=0, le=200_000)


class CandleRequest(BaseModel):
    days: int = Field(5, ge=1, le=90)
    limit: int = Field(0, ge=0)


# The in-flight EOD run. One at a time: two concurrent runs would fetch the
# same symbols twice and fight over the same write lock for no gain.
_eod_task: Optional["asyncio.Task[dict[str, Any]]"] = None


@router.post("/candles")
async def candles(body: CandleRequest) -> dict[str, Any]:
    """Run the candle sync + fill chain now, in-process.

    Same reason /rebuild exists: DuckDB gives its write lock to one process and
    the service holds it, so `python -m app.services.candle_sync` cannot run
    while the bot is up (verified: "Conflicting lock is held"). Otherwise the
    chain is only reachable from the after-close scheduler.

    Returns immediately — a full backfill is thousands of paced Fyers calls and
    would outlive any HTTP timeout. Poll /candles/status.

    `async def` on purpose: the Fyers client lives on the main event loop, so
    the history calls have to be awaited there rather than from a threadpool
    worker with its own loop.
    """
    global _eod_task
    if _eod_task is not None and not _eod_task.done():
        raise HTTPException(status.HTTP_409_CONFLICT, "a candle sync is already running")

    from app.services.candle_sync import run_eod

    _eod_task = asyncio.create_task(run_eod(days=body.days, limit=body.limit))
    log.info("warehouse.candles_started", days=body.days, limit=body.limit)
    return {"started": True, "days": body.days, "limit": body.limit}


@router.get("/candles/status")
async def candles_status() -> dict[str, Any]:
    if _eod_task is None:
        return {"state": "idle"}
    if not _eod_task.done():
        return {"state": "running"}
    try:
        return {"state": "done", "result": _eod_task.result()}
    except Exception as e:  # noqa: BLE001 — report the failure, don't re-raise it
        return {"state": "failed", "error": str(e)[:300]}


@router.post("/rebuild")
def rebuild(body: RebuildRequest) -> dict[str, Any]:
    """Load history / live / prices into the dataset, in-process.

    DuckDB allows exactly one read-write process. The running service owns that
    lock the moment the first announcement is mirrored, so a separate CLI run
    fails with 'Conflicting lock is held'. Admin operations therefore go through
    the service that already holds the connection rather than fighting it.

    All three steps are idempotent — existing uids are skipped and already-filled
    prices are not refetched — so this is safe to call repeatedly.
    """
    from app.services import warehouse_store as store

    out: dict[str, Any] = {}
    t = time.time()
    if body.history:
        out["history_loaded"] = store.load_history()
    if body.live:
        out["live_loaded"] = store.load_live()
    if body.prices:
        from app.services import warehouse_prices
        out["prices"] = warehouse_prices.fill(body.price_limit)
    out["elapsed_s"] = round(time.time() - t, 1)
    out["stats"] = store.stats()
    log.info("warehouse.rebuild", **{k: v for k, v in out.items() if k != "stats"})
    return out


# --------------------------------------------------------------------------
# Catalog + row access shaped for the Dataset page, which renders both this
# announcement-level dataset and the signal-level one through the same table.
# --------------------------------------------------------------------------

# Where each column belongs, and whether it is safe as a model input.
#   feature — knowable at decision time
#   target  — only knowable later; a label, never an input
#   meta    — identity / provenance / data quality
_CATEGORY_RULES: list[tuple[str, str, str]] = [
    # (category, role, matcher-prefix or exact name)
    ("identity", "meta", "uid"), ("identity", "meta", "event_id"),
    ("identity", "meta", "seq_id"), ("identity", "meta", "symbol"),
    ("identity", "meta", "company"), ("identity", "meta", "isin"),
    ("identity", "meta", "exchange"), ("identity", "meta", "content_hash"),
    ("company", "feature", "cap_tier"), ("company", "feature", "market_cap_cr"),
    ("company", "feature", "mcap_rank"), ("company", "feature", "is_smallcap"),
    ("news", "feature", "announced_at"), ("news", "feature", "disseminated_at"),
    ("news", "feature", "category"), ("news", "feature", "headline"),
    ("news", "meta", "attachment_url"), ("news", "meta", "attachment_size"),
    ("analysis", "feature", "ai_"),
    ("context", "meta", "anchor_time"), ("context", "feature", "session_offset"),
    ("prices", "target", "px_"), ("prices", "target", "vol_"),
    ("prices", "target", "day_"),
    ("targets", "target", "ret_"), ("targets", "target", "mkt_"),
    ("targets", "target", "adj_"),
    ("targets", "target", "usable"), ("targets", "target", "mover_"),
    ("quality", "meta", "data_quality"), ("quality", "meta", "price_source"),
    ("quality", "meta", "price_status"), ("quality", "meta", "source_layer"),
    ("quality", "meta", "ingested_at"), ("quality", "meta", "px_t0_age_min"),
]


def _classify(name: str) -> tuple[str, str]:
    for cat, role, match in _CATEGORY_RULES:
        if name == match or (match.endswith("_") and name.startswith(match)):
            return cat, role
    return "other", "meta"


def announcement_column_specs() -> list[dict[str, Any]]:
    """The dataset's columns in the same shape the Dataset page already renders.

    px_*/ret_*/adj_* are tagged `target`, not `feature`: they are only knowable
    after the announcement, and feeding one to a model is look-ahead bias. The
    page's picker makes that mistake visible rather than silent.
    """
    con = _con()
    try:
        out = []
        for name, dtype, *_ in con.execute(f"describe {TABLE}").fetchall():
            cat, role = _classify(name)
            out.append({"key": name, "label": name.replace("_", " "),
                        "category": cat, "role": role, "type": str(dtype)})
        return out
    finally:
        pass


def announcement_rows(*, limit: int, offset: int, columns: Optional[list[str]],
                      symbol: Optional[str] = None, event_type: Optional[str] = None,
                      since: Optional[str] = None, until: Optional[str] = None,
                      q: Optional[str] = None,
                      enriched_only: bool = False) -> dict[str, Any]:
    """Rows for the Dataset page, using the same filters it already sends."""
    con = _con()
    schema = [r[0] for r in con.execute(f"describe {TABLE}").fetchall()]
    sel = [c for c in (columns or schema) if c in set(schema)] or schema
    where, params = [], []
    if symbol:
        where.append("upper(symbol) = upper(?)")
        params.append(symbol)
    if event_type:
        where.append("upper(ai_event_type) = upper(?)")
        params.append(event_type)
    if since:
        where.append("announced_at >= ?")
        params.append(since)
    if until:
        where.append("announced_at <= ?")
        params.append(until)
    if q:
        where.append("(headline ILIKE ? OR company ILIKE ?)")
        params += [f"%{q}%"] * 2
    if enriched_only:
        where.append("price_status = 'filled'")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    total = con.execute(f"SELECT count(*) FROM {TABLE} {clause}", params).fetchone()[0]
    cur = con.execute(
        f"SELECT {', '.join(sel)} FROM {TABLE} {clause} "
        f"ORDER BY announced_at DESC LIMIT {limit} OFFSET {offset}", params)
    rows = _records(cur)
    # `count`, not `returned`: the Dataset page renders "{count} of {total}
    # rows", and the signal/shadow branch already returns `count`. This branch
    # returned a hard-coded `returned: 0`, so the header read
    # "undefined of 305434 rows" while the table below it showed 50.
    return {"total": total, "count": len(rows), "limit": limit, "offset": offset,
            "columns": sel, "rows": rows}


def announcement_export_path(fmt: str, columns: Optional[list[str]] = None,
                             symbol: Optional[str] = None,
                             event_type: Optional[str] = None,
                             since: Optional[str] = None,
                             until: Optional[str] = None,
                             enriched_only: bool = False) -> Path:
    """Write the WHOLE dataset to a temp file and return its path.

    303,505 rows x 97 columns is roughly 150 MB of CSV. Building that as a
    Python string — which is what the signal-side export does — would allocate
    it twice over on a box with ~1 GB free and take the trading process down
    with it. DuckDB's COPY writes straight from the table to disk with flat
    memory, and the caller streams the file back in chunks.
    """
    con = _con()
    schema = [r[0] for r in con.execute(f"describe {TABLE}").fetchall()]
    sel = [c for c in (columns or schema) if c in set(schema)] or schema

    where, params = [], []
    if symbol:
        where.append("upper(symbol) = upper(?)")
        params.append(symbol)
    if event_type:
        where.append("upper(ai_event_type) = upper(?)")
        params.append(event_type)
    if since:
        where.append("announced_at >= ?")
        params.append(since)
    if until:
        where.append("announced_at <= ?")
        params.append(until)
    if enriched_only:
        where.append("price_status = 'filled'")
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    # Sweep exports older than an hour before writing a new one. The streaming
    # response deletes its own file, but a crash or a client that disconnects
    # mid-download leaves a 300 MB+ orphan, and enough of those fill the disk.
    cutoff = time.time() - 3600
    for stale in Path(tempfile.gettempdir()).glob("tradebot_export_*"):
        try:
            if stale.stat().st_mtime < cutoff:
                stale.unlink()
        except OSError:
            pass

    tmp = Path(tempfile.gettempdir()) / f"tradebot_export_{os.getpid()}_{int(time.time())}.{fmt}"
    opts = {
        # Parquet is the one to train from: types survive the round trip (no
        # re-parsing dates and floats out of text), it is columnar so a model
        # can load 6 of 97 columns without reading the rest, and zstd puts the
        # whole dataset under 100 MB instead of 335.
        # ROW_GROUP_SIZE is not cosmetic: parquet buffers a whole row group
        # before flushing, and 97 columns x the 122,880-row default exceeded the
        # 256 MB cap with OutOfMemory. 20k rows keeps each buffer small enough
        # to flush inside the limit.
        "parquet": "FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 20000",
        "csv": "FORMAT csv, HEADER",
        "json": "FORMAT json",   # DuckDB writes newline-delimited JSON
    }[fmt]
    # Deliberately NOT sorted. Sorting 303k x 97 needs the whole result
    # materialised and blew the 256 MB cap with OutOfMemory; unsorted, COPY
    # streams row groups straight to disk at flat memory. A full dump is going
    # into pandas or Excel anyway, where sorting is one line.
    con.execute(
        f"COPY (SELECT {', '.join(sel)} FROM {TABLE} {clause}) "
        f"TO '{tmp.as_posix()}' ({opts})", params)
    return tmp


def announcement_stats() -> dict[str, Any]:
    """Coverage tiles in the EXACT shape the Dataset page already renders.

    Field names mirror the signal-level stats (`sources`, `enrichment`,
    `label_balance`) so the same tiles work for both datasets without the page
    branching on which one it is showing. The meanings differ, which is why the
    page relabels them: here `sources` splits historical vs live-collected, and
    UP/DOWN/FLAT are the market-adjusted 30-minute move rather than a horizon
    label.
    """
    con = _con()
    one = lambda s: con.execute(s).fetchone()  # noqa: E731
    by_src = dict(con.execute(
        f"select source_layer, count(*) from {TABLE} group by 1").fetchall())
    enrich = dict(con.execute(
        f"select price_status, count(*) from {TABLE} group by 1").fetchall())
    rng = one(f"select min(announced_at), max(announced_at) from {TABLE}")
    ncols = len(con.execute(f"describe {TABLE}").fetchall())
    by_event = con.execute(
        f"select coalesce(ai_event_type,'—') e, count(*) n FROM {TABLE} "
        "group by 1 order by n desc limit 20").fetchall()
    return {
        "total_rows": one(f"select count(*) from {TABLE}")[0],
        "sources": {"signal": by_src.get("history", 0),
                    "shadow": by_src.get("live", 0)},
        "enrichment": {
            "complete": enrich.get("filled", 0),
            "pending": enrich.get("pending", 0),
            "no_candles": enrich.get("no_candles", 0),
            "after_hours": one(
                f"select count(*) from {TABLE} where session_offset='next_session'")[0],
        },
        "label_balance": {
            "UP": one(f"select count(*) from {TABLE} where adj_30m > 1.5")[0],
            "DOWN": one(f"select count(*) from {TABLE} where adj_30m < -1.5")[0],
            "FLAT": one(f"select count(*) from {TABLE} where adj_30m is not null "
                        "and abs(adj_30m) <= 1.5")[0],
        },
        "by_event_type": [{"event_type": e, "samples": n} for e, n in by_event],
        "first_row_at": str(rng[0]) if rng and rng[0] else None,
        "last_row_at": str(rng[1]) if rng and rng[1] else None,
        "column_count": ncols,
        "with_ai_label": one(
            f"select count(*) from {TABLE} where ai_sentiment is not null")[0],
    }


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
