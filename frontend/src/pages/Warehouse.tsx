// Warehouse — browse, search and query the unified announcement dataset.
//
// 289k rows x 91 columns never reaches the browser: every view is a filtered,
// paginated request against DuckDB, which does the work server-side and returns
// at most a page. The SQL console is read-only on the server (read-only
// connection plus a keyword screen), so nothing here can modify the dataset.
import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";

type Row = Record<string, unknown>;

interface SearchResult {
  total: number;
  limit: number;
  offset: number;
  elapsed_ms: number;
  rows: Row[];
}

interface Stats {
  rows: number;
  size_mb: number;
  date_range: [string, string];
  by_source: Record<string, number>;
  by_exchange: Record<string, number>;
  price_status: Record<string, number>;
  with_ai_label: number;
  movers: number;
  symbols: number;
  top_event_types: Record<string, number>;
}

const PAGE = 50;

function num(v: unknown, dp = 2): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(dp) : String(v);
}

function Tile({ label, value }: { label: string; value: string }) {
  return (
    <div className="widget" style={{ padding: "10px 14px", margin: 0 }}>
      <div className="field-hint" style={{ marginTop: 0 }}>{label}</div>
      <div style={{ fontSize: 20, fontWeight: 600 }}>{value}</div>
    </div>
  );
}

export default function Warehouse() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [tab, setTab] = useState<"search" | "aggregate" | "sql">("search");

  // search state
  const [q, setQ] = useState("");
  const [symbol, setSymbol] = useState("");
  const [eventType, setEventType] = useState("");
  const [sentiment, setSentiment] = useState("");
  const [capTier, setCapTier] = useState("");
  const [moversOnly, setMoversOnly] = useState(false);
  const [minMove, setMinMove] = useState("");
  const [page, setPage] = useState(0);
  const [res, setRes] = useState<SearchResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [detail, setDetail] = useState<Row | null>(null);

  // aggregate + sql state
  const [groupBy, setGroupBy] = useState("ai_event_type");
  const [agg, setAgg] = useState<Row[]>([]);
  const [sql, setSql] = useState(
    "select ai_event_type, count(*) n, round(avg(adj_30m),3) avg_move\nfrom announcements\nwhere usable\ngroup by 1 order by n desc",
  );
  const [sqlRes, setSqlRes] = useState<{ columns: string[]; rows: Row[]; elapsed_ms: number } | null>(null);

  useEffect(() => {
    api.get<Stats>("/api/warehouse/stats").then(setStats).catch(() => {});
  }, []);

  const runSearch = async (nextPage = 0) => {
    setBusy(true);
    setErr(null);
    try {
      const p = new URLSearchParams();
      if (q) p.set("q", q);
      if (symbol) p.set("symbol", symbol);
      if (eventType) p.set("event_type", eventType);
      if (sentiment) p.set("sentiment", sentiment);
      if (capTier) p.set("cap_tier", capTier);
      if (moversOnly) p.set("movers_only", "true");
      if (minMove) p.set("min_move", minMove);
      p.set("limit", String(PAGE));
      p.set("offset", String(nextPage * PAGE));
      setRes(await api.get<SearchResult>(`/api/warehouse/search?${p}`));
      setPage(nextPage);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => { void runSearch(0); /* initial */ }, []); // eslint-disable-line

  const runAggregate = async (col: string) => {
    setBusy(true);
    try {
      const r = await api.get<{ rows: Row[] }>(`/api/warehouse/aggregate?group_by=${col}`);
      setAgg(r.rows);
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  };

  const runSql = async () => {
    setBusy(true);
    setErr(null);
    try {
      setSqlRes(await api.post("/api/warehouse/query", { sql, limit: 200 }));
    } catch (e) { setErr((e as Error).message); setSqlRes(null); } finally { setBusy(false); }
  };

  const pages = res ? Math.ceil(res.total / PAGE) : 0;
  const eventOptions = useMemo(
    () => Object.keys(stats?.top_event_types ?? {}), [stats]);

  return (
    <div>
      <h1 className="page-title">Dataset</h1>

      {stats && (
        <div style={{ display: "grid", gap: 10, gridTemplateColumns: "repeat(auto-fit,minmax(150px,1fr))", marginBottom: 16 }}>
          <Tile label="Announcements" value={stats.rows.toLocaleString()} />
          <Tile label="Symbols" value={stats.symbols.toLocaleString()} />
          <Tile label="With AI label" value={stats.with_ai_label.toLocaleString()} />
          <Tile label="With prices" value={(stats.price_status.filled ?? 0).toLocaleString()} />
          <Tile label="Movers >1.5%" value={stats.movers.toLocaleString()} />
          <Tile label="Size" value={`${stats.size_mb} MB`} />
        </div>
      )}
      {stats && (
        <p className="field-hint" style={{ marginTop: -8, marginBottom: 14 }}>
          {stats.date_range[0]?.slice(0, 10)} → {stats.date_range[1]?.slice(0, 10)} ·{" "}
          {Object.entries(stats.by_exchange).map(([k, v]) => `${k} ${v.toLocaleString()}`).join(" · ")}
        </p>
      )}

      <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
        {(["search", "aggregate", "sql"] as const).map((t) => (
          <button key={t} onClick={() => setTab(t)} aria-pressed={tab === t}
                  className={tab === t ? "primary" : ""} data-testid={`tab-${t}`}>
            {t === "sql" ? "SQL console" : t[0].toUpperCase() + t.slice(1)}
          </button>
        ))}
      </div>

      {err && <p className="pnl-neg">{err}</p>}

      {tab === "search" && (
        <div className="widget">
          <div style={{ display: "grid", gap: 8, gridTemplateColumns: "repeat(auto-fit,minmax(160px,1fr))" }}>
            <div className="field">
              <label htmlFor="w-q">Text</label>
              <input id="w-q" value={q} data-testid="wh-q" placeholder="headline, company, summary"
                     onChange={(e) => setQ(e.target.value)}
                     onKeyDown={(e) => e.key === "Enter" && runSearch(0)} />
            </div>
            <div className="field">
              <label htmlFor="w-sym">Symbol</label>
              <input id="w-sym" value={symbol} onChange={(e) => setSymbol(e.target.value)}
                     onKeyDown={(e) => e.key === "Enter" && runSearch(0)} />
            </div>
            <div className="field">
              <label htmlFor="w-ev">Event type</label>
              <select id="w-ev" value={eventType} onChange={(e) => setEventType(e.target.value)}>
                <option value="">any</option>
                {eventOptions.map((o) => <option key={o} value={o}>{o}</option>)}
              </select>
            </div>
            <div className="field">
              <label htmlFor="w-sent">Sentiment</label>
              <select id="w-sent" value={sentiment} onChange={(e) => setSentiment(e.target.value)}>
                <option value="">any</option>
                <option value="positive">positive</option>
                <option value="neutral">neutral</option>
                <option value="negative">negative</option>
              </select>
            </div>
            <div className="field">
              <label htmlFor="w-cap">Cap tier</label>
              <select id="w-cap" value={capTier} onChange={(e) => setCapTier(e.target.value)}>
                <option value="">any</option>
                <option value="Large">Large</option>
                <option value="Mid">Mid</option>
                <option value="Small">Small</option>
              </select>
            </div>
            <div className="field">
              <label htmlFor="w-move">Min |move| %</label>
              <input id="w-move" type="number" step="0.5" value={minMove}
                     onChange={(e) => setMinMove(e.target.value)} />
            </div>
          </div>
          <div style={{ display: "flex", gap: 12, alignItems: "center", marginTop: 6 }}>
            <label style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <input type="checkbox" checked={moversOnly}
                     onChange={(e) => setMoversOnly(e.target.checked)} /> movers only
            </label>
            <button className="primary" onClick={() => runSearch(0)} disabled={busy}
                    data-testid="wh-search">
              {busy ? "Searching…" : "Search"}
            </button>
            {res && (
              <span className="field-hint">
                {res.total.toLocaleString()} matches · {res.elapsed_ms} ms
              </span>
            )}
          </div>

          {res && (
            <div style={{ overflowX: "auto", marginTop: 10 }}>
              <table>
                <thead>
                  <tr>
                    <th>Symbol</th><th>When</th><th>Headline</th><th>Event</th>
                    <th>Score</th><th>Rec</th><th>adj 30m</th><th></th>
                  </tr>
                </thead>
                <tbody>
                  {res.rows.map((r) => (
                    <tr key={String(r.uid)}>
                      <td>{String(r.symbol ?? "")}</td>
                      <td>{String(r.announced_at ?? "").slice(0, 16).replace("T", " ")}</td>
                      <td title={String(r.headline ?? "")}>
                        {String(r.headline ?? "").slice(0, 62)}
                      </td>
                      <td>{String(r.ai_event_type ?? "—")}</td>
                      <td>{num(r.ai_sentiment_score, 0)}</td>
                      <td>{String(r.ai_recommendation ?? "—")}</td>
                      <td className={Number(r.adj_30m) > 0 ? "pnl-pos" : Number(r.adj_30m) < 0 ? "pnl-neg" : ""}>
                        {num(r.adj_30m)}
                      </td>
                      <td>
                        <button onClick={() => api.get<Row>(`/api/warehouse/row/${r.uid}`).then(setDetail)}>
                          view
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {pages > 1 && (
                <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 8 }}>
                  <button disabled={page === 0 || busy} onClick={() => runSearch(page - 1)}>Prev</button>
                  <span className="field-hint">page {page + 1} of {pages.toLocaleString()}</span>
                  <button disabled={page + 1 >= pages || busy} onClick={() => runSearch(page + 1)}>Next</button>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {tab === "aggregate" && (
        <div className="widget">
          <p className="empty">
            Realised outcomes grouped by any column — this is what answers
            “which events actually move the stock”.
          </p>
          <div style={{ display: "flex", gap: 8, alignItems: "flex-end" }}>
            <div className="field" style={{ flex: 1 }}>
              <label htmlFor="w-grp">Group by</label>
              <select id="w-grp" value={groupBy} onChange={(e) => setGroupBy(e.target.value)}>
                {["ai_event_type", "cap_tier", "ai_sentiment", "ai_recommendation",
                  "category", "exchange", "session_offset", "source_layer"].map((c) => (
                  <option key={c} value={c}>{c}</option>
                ))}
              </select>
            </div>
            <button className="primary" onClick={() => runAggregate(groupBy)} disabled={busy}>
              Run
            </button>
          </div>
          {agg.length > 0 && (
            <div style={{ overflowX: "auto", marginTop: 10 }}>
              <table>
                <thead><tr>
                  <th>{groupBy}</th><th>n</th><th>with outcome</th>
                  <th>avg adj 30m</th><th>median |move|</th><th>% movers</th><th>% up</th>
                </tr></thead>
                <tbody>
                  {agg.map((r, i) => (
                    <tr key={i}>
                      <td>{String(r.grp ?? "—")}</td>
                      <td>{Number(r.n).toLocaleString()}</td>
                      <td>{Number(r.with_outcome).toLocaleString()}</td>
                      <td className={Number(r.avg_adj_30m) > 0 ? "pnl-pos" : "pnl-neg"}>
                        {num(r.avg_adj_30m, 3)}
                      </td>
                      <td>{num(r.median_abs_move, 3)}</td>
                      <td>{num(r.pct_movers, 1)}%</td>
                      <td>{num(r.pct_up, 1)}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {tab === "sql" && (
        <div className="widget">
          <p className="empty">
            Read-only SELECT against <code>announcements</code>. The server uses a
            read-only connection, so nothing typed here can change the dataset.
          </p>
          <textarea rows={6} value={sql} data-testid="wh-sql"
                    style={{ width: "100%", fontFamily: "monospace", fontSize: 12 }}
                    onChange={(e) => setSql(e.target.value)} />
          <button className="primary" onClick={runSql} disabled={busy} data-testid="wh-run-sql">
            {busy ? "Running…" : "Run query"}
          </button>
          {sqlRes && (
            <div style={{ overflowX: "auto", marginTop: 10 }}>
              <span className="field-hint">
                {sqlRes.rows.length} rows · {sqlRes.elapsed_ms} ms
              </span>
              <table>
                <thead><tr>{sqlRes.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
                <tbody>
                  {sqlRes.rows.map((r, i) => (
                    <tr key={i}>
                      {sqlRes.columns.map((c) => (
                        <td key={c}>{r[c] === null ? "—" : String(r[c]).slice(0, 60)}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {detail && (
        <div className="widget" style={{ marginTop: 14 }}>
          <h3>
            {String(detail.symbol)} — {String(detail.announced_at).slice(0, 19).replace("T", " ")}
            <button style={{ float: "right" }} onClick={() => setDetail(null)}>close</button>
          </h3>
          <div style={{ display: "grid", gap: 4, gridTemplateColumns: "repeat(auto-fit,minmax(230px,1fr))" }}>
            {Object.entries(detail)
              .filter(([, v]) => v !== null && v !== undefined && v !== "")
              .map(([k, v]) => (
                <div key={k} style={{ fontSize: 12 }}>
                  <span className="field-hint">{k}</span>{" "}
                  <strong>{String(v).slice(0, 90)}</strong>
                </div>
              ))}
          </div>
        </div>
      )}
    </div>
  );
}
