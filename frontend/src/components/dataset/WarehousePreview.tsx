// WarehousePreview — the unified announcement dataset, on the Dataset page.
//
// This is the 276k historical corpus merged with everything the live bot has
// collected: one table, one schema, ~303k rows. Distinct from the table above
// it, which is the per-SIGNAL training set — this is per-ANNOUNCEMENT, and most
// announcements never became a signal.
//
// Every column in the dataset is viewable. They are grouped rather than dumped:
// 91 columns is unreadable as one wall, and the px_1m..px_14m / vol_* block is
// for computation, not for eyeballing. Groups start collapsed except the ones
// worth seeing first, and the row drawer always shows the complete record.
import { useEffect, useMemo, useState } from "react";
import { api } from "../../api/client";

type Row = Record<string, unknown>;

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

interface SearchResult {
  total: number;
  limit: number;
  offset: number;
  columns: string[];
  elapsed_ms: number;
  rows: Row[];
}

const PAGE = 25;

// Column groups mirror the dataset's own structure. "prices" and "volumes" are
// off by default: 42 numeric columns are noise until you actually want them.
const GROUPS: { id: string; label: string; on: boolean; cols: (c: string) => boolean }[] = [
  { id: "identity", label: "Identity", on: true,
    cols: (c) => ["uid", "event_id", "seq_id", "symbol", "company", "isin", "exchange"].includes(c) },
  { id: "company", label: "Company", on: true,
    cols: (c) => ["cap_tier", "market_cap_cr", "mcap_rank", "is_smallcap"].includes(c) },
  { id: "news", label: "News", on: true,
    cols: (c) => ["announced_at", "disseminated_at", "category", "headline",
                  "attachment_url", "attachment_size", "content_hash"].includes(c) },
  { id: "ai", label: "AI label", on: true, cols: (c) => c.startsWith("ai_") },
  { id: "returns", label: "Returns", on: true,
    cols: (c) => /^(ret|mkt|adj)_\d+m$/.test(c) },
  { id: "flags", label: "Flags", on: true,
    cols: (c) => ["usable", "mover_1_5", "mover_3", "session_offset", "anchor_time",
                  "data_quality", "price_source", "price_status", "source_layer",
                  "ingested_at"].includes(c) },
  { id: "prices", label: "Prices (px_*)", on: false, cols: (c) => c.startsWith("px_") },
  { id: "volumes", label: "Volumes (vol_*)", on: false, cols: (c) => c.startsWith("vol_") },
  { id: "day", label: "Day bars", on: false,
    cols: (c) => ["day_high", "day_low", "day_volume"].includes(c) },
];

function fmt(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(4);
  const s = String(v);
  return s.length > 70 ? `${s.slice(0, 70)}…` : s;
}

export function WarehousePreview() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [schema, setSchema] = useState<string[]>([]);
  const [active, setActive] = useState<Set<string>>(
    () => new Set(GROUPS.filter((g) => g.on).map((g) => g.id)));

  const [q, setQ] = useState("");
  const [symbol, setSymbol] = useState("");
  const [eventType, setEventType] = useState("");
  const [sentiment, setSentiment] = useState("");
  const [capTier, setCapTier] = useState("");
  const [moversOnly, setMoversOnly] = useState(false);
  const [page, setPage] = useState(0);

  const [res, setRes] = useState<SearchResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [detail, setDetail] = useState<Row | null>(null);
  const [open, setOpen] = useState(true);

  useEffect(() => {
    api.get<Stats>("/api/warehouse/stats").then(setStats).catch(() => {});
    api.get<{ columns: { name: string }[] }>("/api/warehouse/columns")
      .then((d) => setSchema(d.columns.map((c) => c.name)))
      .catch(() => {});
  }, []);

  const visible = useMemo(() => {
    if (!schema.length) return [];
    const on = GROUPS.filter((g) => active.has(g.id));
    const cols = schema.filter((c) => on.some((g) => g.cols(c)));
    // uid drives the row key and the detail fetch; keep it even if Identity is off.
    return cols.includes("uid") ? cols : ["uid", ...cols];
  }, [schema, active]);

  const run = async (next = 0) => {
    if (!visible.length) return;
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
      p.set("columns", visible.join(","));
      p.set("limit", String(PAGE));
      p.set("offset", String(next * PAGE));
      setRes(await api.get<SearchResult>(`/api/warehouse/search?${p}`));
      setPage(next);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => { if (visible.length) void run(0); /* eslint-disable-line */ },
            [visible.length]);

  const pages = res ? Math.ceil(res.total / PAGE) : 0;

  return (
    <div className="widget" data-testid="warehouse-preview">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <h3 style={{ margin: 0 }}>
          Announcement dataset — preview{" "}
          <span className="field-hint">
            {stats ? `${stats.rows.toLocaleString()} rows · ${schema.length} columns` : ""}
          </span>
        </h3>
        <button onClick={() => setOpen((v) => !v)} data-testid="wh-toggle">
          {open ? "hide" : "show"}
        </button>
      </div>
      <p className="empty" style={{ marginTop: 4 }}>
        Every NSE/BSE announcement — the historical corpus plus everything the bot
        has collected since — with its AI label and realised price reaction. One row
        per <strong>announcement</strong>; the table above is one row per{" "}
        <strong>signal</strong>.
      </p>

      {!open ? null : (
        <>
          {stats && (
            <p className="field-hint" style={{ marginTop: 0 }}>
              {stats.date_range[0]?.slice(0, 10)} → {stats.date_range[1]?.slice(0, 10)} ·{" "}
              {Object.entries(stats.by_exchange).map(([k, v]) => `${k} ${v.toLocaleString()}`).join(" · ")}{" "}
              · {(stats.price_status.filled ?? 0).toLocaleString()} with prices ·{" "}
              {stats.movers.toLocaleString()} movers · {stats.size_mb} MB
            </p>
          )}

          <div style={{ display: "grid", gap: 8, gridTemplateColumns: "repeat(auto-fit,minmax(150px,1fr))" }}>
            <div className="field">
              <label htmlFor="wh-q">Text</label>
              <input id="wh-q" value={q} data-testid="wh-q" placeholder="headline / company"
                     onChange={(e) => setQ(e.target.value)}
                     onKeyDown={(e) => e.key === "Enter" && run(0)} />
            </div>
            <div className="field">
              <label htmlFor="wh-sym">Symbol</label>
              <input id="wh-sym" value={symbol} onChange={(e) => setSymbol(e.target.value)}
                     onKeyDown={(e) => e.key === "Enter" && run(0)} />
            </div>
            <div className="field">
              <label htmlFor="wh-ev">Event type</label>
              <select id="wh-ev" value={eventType} onChange={(e) => setEventType(e.target.value)}>
                <option value="">any</option>
                {Object.keys(stats?.top_event_types ?? {}).map((o) =>
                  <option key={o} value={o}>{o}</option>)}
              </select>
            </div>
            <div className="field">
              <label htmlFor="wh-sent">Sentiment</label>
              <select id="wh-sent" value={sentiment} onChange={(e) => setSentiment(e.target.value)}>
                <option value="">any</option>
                <option value="positive">positive</option>
                <option value="neutral">neutral</option>
                <option value="negative">negative</option>
              </select>
            </div>
            <div className="field">
              <label htmlFor="wh-cap">Cap tier</label>
              <select id="wh-cap" value={capTier} onChange={(e) => setCapTier(e.target.value)}>
                <option value="">any</option>
                <option value="Large">Large</option>
                <option value="Mid">Mid</option>
                <option value="Small">Small</option>
              </select>
            </div>
          </div>

          <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap", margin: "6px 0" }}>
            <label style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <input type="checkbox" checked={moversOnly}
                     onChange={(e) => setMoversOnly(e.target.checked)} /> movers only
            </label>
            <button className="primary" onClick={() => run(0)} disabled={busy} data-testid="wh-search">
              {busy ? "Loading…" : "Search"}
            </button>
            {res && (
              <span className="field-hint">
                {res.total.toLocaleString()} rows · {res.columns.length} columns · {res.elapsed_ms} ms
              </span>
            )}
          </div>

          <div className="settings-group-title">Columns</div>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 8 }}>
            {GROUPS.map((g) => {
              const n = schema.filter(g.cols).length;
              if (!n) return null;
              const on = active.has(g.id);
              return (
                <button key={g.id} aria-pressed={on} data-testid={`wh-grp-${g.id}`}
                        className={on ? "primary" : ""}
                        onClick={() => setActive((prev) => {
                          const s = new Set(prev);
                          s.has(g.id) ? s.delete(g.id) : s.add(g.id);
                          return s;
                        })}>
                  {g.label} ({n})
                </button>
              );
            })}
          </div>

          {err && <p className="pnl-neg">{err}</p>}

          {res && (
            <div style={{ overflowX: "auto", maxHeight: 520, overflowY: "auto" }}>
              <table style={{ fontSize: 12 }}>
                <thead>
                  <tr>
                    {res.columns.map((c) => <th key={c} style={{ whiteSpace: "nowrap" }}>{c}</th>)}
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {res.rows.map((r) => (
                    <tr key={String(r.uid)}>
                      {res.columns.map((c) => (
                        <td key={c} title={String(r[c] ?? "")}
                            className={c === "adj_30m"
                              ? (Number(r[c]) > 0 ? "pnl-pos" : Number(r[c]) < 0 ? "pnl-neg" : "")
                              : ""}
                            style={{ whiteSpace: "nowrap" }}>
                          {fmt(r[c])}
                        </td>
                      ))}
                      <td>
                        <button onClick={() =>
                          api.get<Row>(`/api/warehouse/row/${encodeURIComponent(String(r.uid))}`)
                            .then(setDetail).catch((e) => setErr((e as Error).message))}>
                          all
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {pages > 1 && (
            <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 8 }}>
              <button disabled={page === 0 || busy} onClick={() => run(page - 1)}>Prev</button>
              <span className="field-hint">page {page + 1} of {pages.toLocaleString()}</span>
              <button disabled={page + 1 >= pages || busy} onClick={() => run(page + 1)}>Next</button>
            </div>
          )}

          {detail && (
            <div style={{ marginTop: 12, borderTop: "1px solid var(--border)", paddingTop: 10 }}>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <strong>
                  {String(detail.symbol)} · {String(detail.announced_at).slice(0, 19).replace("T", " ")}
                  <span className="field-hint"> — all {Object.keys(detail).length} columns</span>
                </strong>
                <button onClick={() => setDetail(null)}>close</button>
              </div>
              <div style={{ display: "grid", gap: 3, marginTop: 6,
                            gridTemplateColumns: "repeat(auto-fit,minmax(210px,1fr))" }}>
                {Object.entries(detail).map(([k, v]) => (
                  <div key={k} style={{ fontSize: 11 }}>
                    <span className="field-hint">{k}</span>{" "}
                    <strong>{fmt(v)}</strong>
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
