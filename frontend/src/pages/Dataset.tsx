// Dataset — the customizable ML training-set builder.
//
// One wide table joining everything the pipeline knows about each
// signal: the news, the AI verdict, the decision, planned levels,
// execution facts, the coarse outcome probes, and the 1-minute-candle
// reaction features + horizon targets from the DatasetBuilder.
//
// The column catalog tags every column with its ML role:
//   FEATURE — knowable at (or within 5 min of) decision time; safe input
//   TARGET  — only knowable at the horizon; training label ONLY
//   META    — identity / data-quality
// Using a TARGET as a model input is look-ahead bias — the picker makes
// that mistake visually impossible to make silently.

import { useEffect, useMemo, useState } from "react";
import {
  datasetQueryString,
  useDatasetBackfill,
  useDatasetBackfillStatus,
  useDatasetCalibration,
  useDatasetColumns,
  useDatasetHealth,
  useDatasetRows,
  useDatasetStats,
  type CalibrationBucket,
  type DatasetColumn,
  type DatasetSource,
  type DatasetFilters,
} from "../hooks/useApi";

const COLUMNS_KEY = "tradebot.dataset.columns.v1";

const CATEGORY_LABELS: Record<string, string> = {
  identity: "Identity",
  news: "News",
  context: "Session / market context",
  analysis: "AI analysis",
  signal: "Signal",
  levels: "Planned levels",
  execution: "Execution",
  outcome_samples: "Outcome probes",
  reaction: "Market reaction (1-min candles)",
  trajectory: "Minute-by-minute trajectory (T+6…T+14)",
  targets: "Horizon targets (labels)",
  quality: "Data quality",
};

// Numeric label columns offered as the health panel's correlation target.
const HEALTH_TARGETS = [
  "ret_15m_pct",
  "high_15m",
  "mfe_15m_pct",
  "mae_15m_pct",
  "time_to_peak_min",
  "retrace_from_peak_pct",
  "move_30m_pct",
  "r_multiple",
];

function agoText(iso: string | null | undefined): string {
  if (!iso) return "never";
  const t = new Date(iso.endsWith("Z") ? iso : iso + "Z").getTime();
  if (Number.isNaN(t)) return "—";
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

function inText(iso: string | null | undefined): string {
  if (!iso) return "—";
  const t = new Date(iso.endsWith("Z") ? iso : iso + "Z").getTime();
  if (Number.isNaN(t)) return "—";
  const s = Math.round((t - Date.now()) / 1000);
  if (s <= 0) return "due now";
  if (s < 60) return `in ${s}s`;
  return `in ${Math.round(s / 60)}m`;
}

function loadSavedColumns(): string[] | null {
  try {
    const raw = localStorage.getItem(COLUMNS_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function RoleBadge({ role }: { role: DatasetColumn["role"] }) {
  const cls = role === "feature" ? "pnl-pos" : role === "target" ? "pnl-neg" : "";
  return (
    <span
      className={`mono ${cls}`}
      style={{ fontSize: 9, textTransform: "uppercase", opacity: role === "meta" ? 0.6 : 1 }}
    >
      {role}
    </span>
  );
}

function fmtCell(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "boolean") return v ? "✓" : "✗";
  if (typeof v === "number") {
    if (Number.isInteger(v)) return String(v);
    return v.toFixed(Math.abs(v) < 1 ? 4 : 2);
  }
  const s = String(v);
  return s.length > 48 ? s.slice(0, 45) + "…" : s;
}

const SOURCE_KEY = "tradebot.dataset.source.v1";

export default function Dataset() {
  // Which dataset the page is showing. `announcements` is the merged corpus
  // (historical + everything collected live, ~300k rows); `signals` is the
  // per-signal training set. Different grains, so switching also swaps the
  // column catalog, the stats and the export.
  const [source, setSource] = useState<DatasetSource>(
    () => (localStorage.getItem(SOURCE_KEY) as DatasetSource) || "announcements");
  useEffect(() => { localStorage.setItem(SOURCE_KEY, source); }, [source]);

  const { data: catalog } = useDatasetColumns(source);
  const { data: stats } = useDatasetStats(source);
  const backfill = useDatasetBackfill();
  const { data: backfillStatus } = useDatasetBackfillStatus();
  const fullRunning = backfillStatus?.progress?.running ?? false;
  const remaining = backfillStatus?.remaining;
  const remainingTotal = remaining ? remaining.signal + remaining.shadow : null;

  const allKeys = useMemo(
    () => (catalog?.columns ?? []).map((c) => c.key),
    [catalog]
  );
  const [selected, setSelected] = useState<string[]>(() => loadSavedColumns() ?? []);
  // The two catalogs share almost no column names, so a selection saved for one
  // dataset selects nothing in the other. Reset to "all" whenever the source
  // changes rather than rendering an empty table.
  useEffect(() => {
    if (allKeys.length > 0) setSelected(allKeys);
  }, [source, allKeys.length]);  // eslint-disable-line react-hooks/exhaustive-deps
  // First load with no saved selection → select everything.
  useEffect(() => {
    if (selected.length === 0 && allKeys.length > 0 && loadSavedColumns() === null) {
      setSelected(allKeys);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allKeys]);
  useEffect(() => {
    try {
      if (selected.length > 0) localStorage.setItem(COLUMNS_KEY, JSON.stringify(selected));
    } catch {
      /* ignore */
    }
  }, [selected]);

  const [filters, setFilters] = useState<DatasetFilters>({});
  const [limit, setLimit] = useState(50);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [dedup, setDedup] = useState(true);
  const [healthOpen, setHealthOpen] = useState(false);
  const [healthTarget, setHealthTarget] = useState("ret_15m_pct");
  const { data: health } = useDatasetHealth(healthTarget, healthOpen);
  const [calibOpen, setCalibOpen] = useState(true);
  const [calibMove, setCalibMove] = useState(1.5);
  const { data: calib } = useDatasetCalibration(calibMove, calibOpen);

  // Keep the preview's column ORDER canonical (catalog order).
  const orderedSelection = useMemo(
    () => allKeys.filter((k) => selected.includes(k)),
    [allKeys, selected]
  );
  const { data: rowsResp, isLoading, error } = useDatasetRows(
    { ...filters, source },
    orderedSelection.length > 0 ? orderedSelection : undefined,
    limit
  );

  const byCategory = useMemo(() => {
    const groups: Record<string, DatasetColumn[]> = {};
    for (const c of catalog?.columns ?? []) {
      (groups[c.category] ??= []).push(c);
    }
    return groups;
  }, [catalog]);

  const toggle = (key: string) =>
    setSelected((cur) =>
      cur.includes(key) ? cur.filter((k) => k !== key) : [...cur, key]
    );
  const toggleGroup = (keys: string[], on: boolean) =>
    setSelected((cur) => {
      const set = new Set(cur);
      keys.forEach((k) => (on ? set.add(k) : set.delete(k)));
      return allKeys.filter((k) => set.has(k));
    });
  const applyPreset = (keys: string[]) =>
    setSelected(allKeys.filter((k) => keys.includes(k)));

  // Exports are fetched rather than linked. The server spends ~9s building a
  // 9 MB CSV, and a plain <a download> gives no feedback for that whole time —
  // indistinguishable from a dead button, which is exactly how it was reported.
  const [downloading, setDownloading] = useState<string | null>(null);
  const [exportErr, setExportErr] = useState<string | null>(null);
  const doExport = async (url: string, label: string) => {
    setDownloading(label);
    setExportErr(null);
    try {
      const res = await fetch(url, { credentials: "same-origin" });
      if (!res.ok) throw new Error(`${res.status} ${(await res.text()).slice(0, 160)}`);
      const blob = await res.blob();
      if (blob.size === 0) throw new Error("the server returned an empty file");
      const name = /filename=([^;]+)/.exec(
        res.headers.get("content-disposition") ?? "")?.[1]?.trim();
      const href = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = href;
      a.download = name || `dataset.${url.includes("jsonl") ? "jsonl" : "csv"}`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(href);
    } catch (e) {
      setExportErr(`${label} failed — ${(e as Error).message}`);
    } finally {
      setDownloading(null);
    }
  };

  const exportExtra: Record<string, string | number> = { limit: 20000, source };
  if (dedup) exportExtra.dedup = "true";
  const exportQs = datasetQueryString(filters, orderedSelection, exportExtra);
  const splitQs = (split: "train" | "val") =>
    datasetQueryString(filters, orderedSelection, {
      ...exportExtra,
      split,
      split_ratio: 0.8,
    });
  const enrich = stats?.enrichment ?? {};
  const complete = enrich["complete"] ?? 0;
  const coveragePct =
    stats && stats.total_rows > 0
      ? Math.round((complete / stats.total_rows) * 100)
      : null;

  return (
    <div>
      {exportErr && (
        <p className="pnl-neg" data-testid="dataset-export-error">{exportErr}</p>
      )}
      <div className="dashboard-head">
        <h1 className="page-title">
          Dataset
          <span style={{ display: "inline-flex", gap: 6, marginLeft: 14, verticalAlign: "middle" }}>
            {([
              ["announcements", "Announcements"],
              ["signals", "Signals"],
            ] as const).map(([key, label]) => (
              <button
                key={key}
                className={source === key ? "primary" : "chart-btn"}
                style={source === key ? undefined : { border: "1px solid var(--border)" }}
                onClick={() => setSource(key)}
                aria-pressed={source === key}
                data-testid={`dataset-source-${key}`}
                title={key === "announcements"
                  ? "Every NSE/BSE announcement — the historical corpus merged with everything collected live. One row per announcement."
                  : "The per-signal training set built from the trading database. One row per signal, with planned levels, execution and horizon targets."}
              >
                {label}
              </button>
            ))}
          </span>
        </h1>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <button
            className="chart-btn"
            style={{ border: "1px solid var(--border)" }}
            onClick={() => backfill.mutate({ limit: 200 })}
            disabled={backfill.isPending || fullRunning}
            data-testid="dataset-backfill"
            title="Fetch 1-min candles and compute features for one batch of pending rows (needs Fyers)"
          >
            {backfill.isPending ? "Enriching…" : "⟳ Enrich now"}
          </button>
          <button
            className="chart-btn"
            style={{ border: "1px solid var(--border)" }}
            onClick={() => backfill.mutate({ full: true, retryFailed: true })}
            disabled={backfill.isPending || fullRunning}
            data-testid="dataset-backfill-full"
            title="Keep running batches in the background until EVERY pending row (signals + shadow) is enriched. Rows stuck at no-candles/partial get fresh attempts. One history call per symbol; after-hours filings settled without any call."
          >
            {fullRunning ? "Filling history…" : "⛁ Fill entire history"}
          </button>
          <button
            className="chart-btn"
            style={{ border: "1px solid var(--border)" }}
            onClick={() => backfill.mutate({ rebuild: true })}
            disabled={backfill.isPending || fullRunning}
            data-testid="dataset-backfill-rebuild"
            title="Re-enrich already-complete rows whose feature schema is out of date — use after a new feature set ships (e.g. the full minute-by-minute trajectory) to roll it over your existing history."
          >
            ↻ Rebuild schema
          </button>
          <button
            className="chart-btn"
            style={{ border: "1px solid var(--border)" }}
            disabled={downloading !== null}
            onClick={() => doExport(`/api/dataset/export?format=csv&${exportQs}`, "Export CSV")}
          >
            {downloading === "Export CSV" ? "Preparing…" : "Export CSV"}
          </button>
          <button
            className="chart-btn"
            style={{ border: "1px solid var(--border)" }}
            disabled={downloading !== null}
            onClick={() => doExport(`/api/dataset/export?format=jsonl&${exportQs}`, "Export JSONL")}
          >
            {downloading === "Export JSONL" ? "Preparing…" : "Export JSONL"}
          </button>
          <button
            className="chart-btn"
            style={{ border: "1px solid var(--border)" }}
            disabled={downloading !== null}
            title="Oldest 80% of the filtered set, chronological — train on this"
            onClick={() => doExport(`/api/dataset/export?format=csv&${splitQs("train")}`, "Train CSV")}
          >
            {downloading === "Train CSV" ? "Preparing…" : "Train CSV"}
          </button>
          <button
            className="chart-btn"
            style={{ border: "1px solid var(--border)" }}
            disabled={downloading !== null}
            title="Newest 20% — validate on this (never random-split news data)"
            onClick={() => doExport(`/api/dataset/export?format=csv&${splitQs("val")}`, "Val CSV")}
          >
            {downloading === "Val CSV" ? "Preparing…" : "Val CSV"}
          </button>
          <label
            className="meta"
            style={{ cursor: "pointer", whiteSpace: "nowrap" }}
            title="Collapse NSE+BSE copies of the same news to one row — twins split across train/val fake your accuracy"
          >
            <input
              type="checkbox"
              checked={dedup}
              onChange={(e) => setDedup(e.target.checked)}
              style={{ marginRight: 4 }}
            />
            dedup cross-listings
          </label>
        </div>
      </div>

      {/* Live collector heartbeat — proof the continuous 2-min stream is on */}
      {(() => {
        const c = backfillStatus?.collector;
        const on = c?.running ?? false;
        return (
          <div
            className="widget widget-wide"
            style={{
              marginBottom: 12,
              padding: "8px 14px",
              display: "flex",
              alignItems: "center",
              gap: 12,
              flexWrap: "wrap",
            }}
            data-testid="dataset-collector"
          >
            <span
              className={`ws-status ${on ? "connected" : "disconnected"}`}
              title={on ? "Auto-collector is running" : "Auto-collector is stopped"}
              style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
            >
              <span className="dot" />
              <span className="mono" style={{ fontSize: 12 }}>
                LIVE COLLECTOR {on ? "ACTIVE" : "OFF"}
              </span>
            </span>
            <span className="meta">
              {on
                ? `every ${Math.round(c?.interval_s ?? 120)}s — new signals auto-fill with all fields ~16 min after firing`
                : "not running (restart the bot to start the continuous stream)"}
            </span>
            {c && (
              <span className="meta" style={{ marginLeft: "auto" }}>
                last run {agoText(c.last_run_at)}
                {on && c.next_run_at ? ` · next ${inText(c.next_run_at)}` : ""} ·{" "}
                {c.runs} runs · +{c.enriched_session} enriched this session
              </span>
            )}
          </div>
        );
      })()}

      {/* Full-backfill live progress / pending line */}
      {(fullRunning || (backfillStatus?.progress?.finished_at && (backfillStatus.progress.processed ?? 0) > 0)) && (
        <p className="meta" style={{ marginBottom: 8 }}>
          {fullRunning ? "⏳ Filling history: " : "History backfill done: "}
          batch {backfillStatus?.progress?.batches ?? 0} —{" "}
          {backfillStatus?.progress?.processed ?? 0} processed (
          {backfillStatus?.progress?.complete ?? 0} complete,{" "}
          {backfillStatus?.progress?.partial ?? 0} partial,{" "}
          {backfillStatus?.progress?.after_hours ?? 0} after-hours,{" "}
          {backfillStatus?.progress?.no_candles ?? 0} no candles)
          {fullRunning && remainingTotal !== null && (
            <> — ~{remainingTotal} rows remaining</>
          )}
        </p>
      )}
      {!fullRunning && remainingTotal !== null && remainingTotal > 0 && (
        <p className="meta" style={{ marginBottom: 8 }}>
          {remainingTotal} rows await enrichment ({remaining!.signal} signal +{" "}
          {remaining!.shadow} shadow) — "Fill entire history" processes them
          all in the background.
        </p>
      )}
      {backfill.data && !backfill.data.full && (
        <p className="meta" style={{ marginBottom: 8 }}>
          Enrichment batch: {backfill.data.processed} processed (
          {backfill.data.shadow ?? 0} shadow) — {backfill.data.complete}{" "}
          complete, {backfill.data.partial} partial,{" "}
          {backfill.data.no_candles} without candles
          {backfill.data.no_candles > 0 && " (is Fyers connected?)"},{" "}
          {backfill.data.after_hours ?? 0} after-hours,{" "}
          {backfill.data.too_old} too old, {backfill.data.errors} errors.
        </p>
      )}

      {/* Coverage tiles */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))",
          gap: 8,
          marginBottom: 12,
        }}
      >
        {[
          {
            label: "Rows (signal + shadow)",
            value: stats
              ? `${stats.total_rows} (${stats.sources?.signal ?? 0}+${
                  stats.sources?.shadow ?? 0
                })`
              : "—",
          },
          {
            label: "Enriched",
            value:
              coveragePct !== null ? `${complete} (${coveragePct}%)` : complete,
          },
          { label: "Partial", value: enrich["partial"] ?? 0 },
          { label: "After hours", value: enrich["after_hours"] ?? 0 },
          {
            label: "Awaiting candles",
            value: (enrich["pending"] ?? 0) + (enrich["no_candles"] ?? 0),
          },
          {
            label: "Labels UP / DOWN / FLAT",
            value: `${stats?.label_balance?.["UP"] ?? 0} / ${
              stats?.label_balance?.["DOWN"] ?? 0
            } / ${stats?.label_balance?.["FLAT"] ?? 0}`,
          },
          { label: "Columns", value: stats?.column_count ?? "—" },
        ].map((t) => (
          <div key={t.label} className="widget" style={{ padding: "10px 12px" }}>
            <div className="meta" style={{ fontSize: 10 }}>{t.label}</div>
            <div className="mono" style={{ fontSize: 18 }}>{t.value}</div>
          </div>
        ))}
      </div>

      {/* HOLD calibration — is the bot passing on movers? */}
      <div className="widget widget-wide" style={{ marginBottom: 12 }}>
        <h3 style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <button
            className="chart-btn"
            style={{ border: "1px solid var(--border)" }}
            onClick={() => setCalibOpen((v) => !v)}
            data-testid="dataset-calibration-toggle"
          >
            {calibOpen ? "▾" : "▸"} HOLD calibration
          </button>
          <span className="meta">
            of the filings the bot DECLINED, how many actually moved?
          </span>
          {calibOpen && (
            <>
              <span style={{ flex: 1 }} />
              <span className="meta">“moved” ≥</span>
              <select
                value={calibMove}
                onChange={(e) => setCalibMove(Number(e.target.value))}
                style={{ width: "auto", padding: "2px 6px" }}
              >
                {[1.0, 1.5, 2.0, 3.0].map((n) => (
                  <option key={n} value={n}>{n.toFixed(1)}% in 15m</option>
                ))}
              </select>
            </>
          )}
        </h3>
        {calibOpen &&
          (!calib ? (
            <p className="empty">Loading… (needs enriched rows — run “Fill entire history” first)</p>
          ) : calib.sample.total_complete === 0 ? (
            <p className="empty">
              No enriched rows yet. Calibration reads the real 15-minute
              forward reaction the DatasetBuilder labels — click “Fill entire
              history” to backfill, then check back.
            </p>
          ) : (
            <div style={{ padding: "0 12px 12px" }}>
              <p
                className="mono"
                data-testid="calibration-verdict"
                style={{ fontSize: 13, marginBottom: 10 }}
              >
                {calib.verdict}
              </p>

              {/* taken vs declined comparison */}
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))",
                  gap: 8,
                  marginBottom: 12,
                }}
              >
                {[
                  { label: "Declined — moved", b: calib.comparison.declined, warn: true },
                  { label: "Taken — moved", b: calib.comparison.taken, warn: false },
                  {
                    label: "HOLD filings labeled",
                    plain: `${calib.sample.hold}`,
                    sub: `${calib.rows_analysed} complete rows`,
                  },
                ].map((t) => (
                  <div key={t.label} className="widget" style={{ padding: "10px 12px" }}>
                    <div className="meta" style={{ fontSize: 10 }}>{t.label}</div>
                    {"b" in t && t.b ? (
                      <>
                        <div className="mono" style={{ fontSize: 18 }}>
                          {(t.b.mover_rate * 100).toFixed(0)}%
                        </div>
                        <div className="meta" style={{ fontSize: 10 }}>
                          {t.b.movers}/{t.b.n} · {t.b.big_mover_rate
                            ? `${(t.b.big_mover_rate * 100).toFixed(0)}% ≥${calib.thresholds.big_pct}%`
                            : ""}
                        </div>
                      </>
                    ) : (
                      <>
                        <div className="mono" style={{ fontSize: 18 }}>{t.plain}</div>
                        <div className="meta" style={{ fontSize: 10 }}>{t.sub}</div>
                      </>
                    )}
                  </div>
                ))}
              </div>

              {/* by event type — the actionable breakdown */}
              <div style={{ overflowX: "auto", maxWidth: "100%" }}>
                <table style={{ whiteSpace: "nowrap" }}>
                  <thead>
                    <tr>
                      <th>Declined event type</th>
                      <th>n</th>
                      <th>% moved</th>
                      <th>% big</th>
                      <th>avg swing</th>
                      <th>up / down / flat</th>
                    </tr>
                  </thead>
                  <tbody>
                    {calib.by_event_type
                      .filter((b: CalibrationBucket) => b.n >= 3)
                      .map((b: CalibrationBucket) => (
                        <tr key={b.key}>
                          <td className="mono">{b.key}</td>
                          <td className="mono">{b.n}</td>
                          <td className={`mono ${b.mover_rate >= 0.3 ? "pnl-neg" : ""}`}>
                            {(b.mover_rate * 100).toFixed(0)}%
                          </td>
                          <td className="mono">{(b.big_mover_rate * 100).toFixed(0)}%</td>
                          <td className="mono">
                            {b.avg_abs_excursion !== null ? `${b.avg_abs_excursion}%` : "—"}
                          </td>
                          <td className="mono">{b.up} / {b.down} / {b.flat}</td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              </div>
              <p className="meta" style={{ fontSize: 10, marginTop: 6 }}>
                A high “% moved” on a declined event type = the rules are too
                tight there. Red = ≥30% of declined filings moved.
              </p>

              {/* biggest missed movers */}
              {calib.top_missed.length > 0 && (
                <div style={{ marginTop: 12 }}>
                  <div className="meta" style={{ fontSize: 11, marginBottom: 4 }}>
                    Biggest movers the bot passed on (HOLD):
                  </div>
                  <div style={{ overflowX: "auto", maxWidth: "100%" }}>
                    <table style={{ whiteSpace: "nowrap" }}>
                      <thead>
                        <tr>
                          <th>Symbol</th>
                          <th>Event</th>
                          <th>Conf</th>
                          <th>Swing</th>
                          <th>Net 15m</th>
                          <th>Label</th>
                        </tr>
                      </thead>
                      <tbody>
                        {calib.top_missed.map((m, i) => (
                          <tr key={`${m.symbol}-${i}`}>
                            <td className="mono">{m.symbol ?? "—"}</td>
                            <td className="mono">{m.event_type ?? "—"}</td>
                            <td className="mono">
                              {m.confidence !== null ? m.confidence.toFixed(2) : "—"}
                            </td>
                            <td className="mono">{m.abs_excursion}%</td>
                            <td
                              className={`mono ${
                                m.ret_15m_pct !== null
                                  ? m.ret_15m_pct >= 0 ? "pnl-pos" : "pnl-neg"
                                  : ""
                              }`}
                            >
                              {m.ret_15m_pct !== null ? `${m.ret_15m_pct}%` : "—"}
                            </td>
                            <td className="mono">{m.label_15m ?? "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </div>
          ))}
      </div>

      {/* Look-ahead bias warning */}
      <div className="widget widget-wide" style={{ marginBottom: 12, padding: "10px 14px" }}>
        <span className="mono pnl-pos" style={{ fontSize: 10 }}>FEATURE</span>{" "}
        <span className="meta">
          = knowable at decision time (T0…T+5) — safe model input.
        </span>{" "}
        <span className="mono pnl-neg" style={{ fontSize: 10 }}>TARGET</span>{" "}
        <span className="meta">
          = only knowable at T+15 — train on it, never feed it in as an input
          (that's look-ahead bias). Split train/validation by TIME, not randomly.
        </span>
      </div>

      {/* Column picker */}
      <div className="widget widget-wide" style={{ marginBottom: 12 }}>
        <h3 style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <button
            className="chart-btn"
            style={{ border: "1px solid var(--border)" }}
            onClick={() => setPickerOpen((v) => !v)}
            data-testid="dataset-column-toggle"
          >
            {pickerOpen ? "▾" : "▸"} Columns ({orderedSelection.length}/{allKeys.length})
          </button>
          <span className="meta">Presets:</span>
          {Object.entries(catalog?.presets ?? {}).map(([name, keys]) => (
            <button
              key={name}
              className="chart-btn"
              style={{ border: "1px solid var(--border)" }}
              onClick={() => applyPreset(keys)}
              title={`${keys.length} columns`}
            >
              {name.replace(/_/g, " ")}
            </button>
          ))}
        </h3>
        {pickerOpen && (
          <div style={{ padding: "4px 12px 12px" }}>
            {Object.entries(byCategory).map(([cat, cols]) => {
              const keys = cols.map((c) => c.key);
              const allOn = keys.every((k) => selected.includes(k));
              return (
                <div key={cat} style={{ marginBottom: 10 }}>
                  <div style={{ marginBottom: 4 }}>
                    <label className="mono" style={{ fontSize: 11, cursor: "pointer" }}>
                      <input
                        type="checkbox"
                        checked={allOn}
                        onChange={() => toggleGroup(keys, !allOn)}
                        style={{ marginRight: 6 }}
                      />
                      {CATEGORY_LABELS[cat] ?? cat}
                    </label>
                  </div>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                    {cols.map((c) => (
                      <label
                        key={c.key}
                        title={`${c.description} — role: ${c.role}`}
                        style={{
                          display: "inline-flex",
                          alignItems: "center",
                          gap: 4,
                          border: "1px solid var(--border)",
                          borderRadius: 4,
                          padding: "2px 6px",
                          fontSize: 11,
                          cursor: "pointer",
                          opacity: selected.includes(c.key) ? 1 : 0.45,
                        }}
                      >
                        <input
                          type="checkbox"
                          checked={selected.includes(c.key)}
                          onChange={() => toggle(c.key)}
                          style={{ margin: 0 }}
                        />
                        <span className="mono">{c.key}</span>
                        <RoleBadge role={c.role} />
                      </label>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Column health / leakage report */}
      <div className="widget widget-wide" style={{ marginBottom: 12 }}>
        <h3 style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <button
            className="chart-btn"
            style={{ border: "1px solid var(--border)" }}
            onClick={() => setHealthOpen((v) => !v)}
            data-testid="dataset-health-toggle"
          >
            {healthOpen ? "▾" : "▸"} Column health
          </button>
          {healthOpen && (
            <>
              <span className="meta">correlation vs target:</span>
              <select
                value={healthTarget}
                onChange={(e) => setHealthTarget(e.target.value)}
                style={{ width: "auto", padding: "2px 6px" }}
              >
                {HEALTH_TARGETS.map((t) => (
                  <option key={t} value={t}>{t}</option>
                ))}
              </select>
              {health && (
                <span className="meta">{health.rows_sampled} rows sampled</span>
              )}
            </>
          )}
        </h3>
        {healthOpen && (
          !health ? (
            <p className="empty">Loading…</p>
          ) : (
            <div style={{ overflowX: "auto", maxWidth: "100%", padding: "0 12px 12px" }}>
              <table style={{ whiteSpace: "nowrap" }}>
                <thead>
                  <tr>
                    <th>Column</th>
                    <th>Role</th>
                    <th>Null %</th>
                    <th>Mean</th>
                    <th>Std</th>
                    <th>Corr → {health.target}</th>
                    <th>Flags</th>
                  </tr>
                </thead>
                <tbody>
                  {health.columns.map((c) => {
                    const nullPct =
                      c.null_rate !== null ? Math.round(c.null_rate * 100) : null;
                    return (
                      <tr key={c.key}>
                        <td className="mono">{c.key}</td>
                        <td><RoleBadge role={c.role} /></td>
                        <td className={`mono ${nullPct !== null && nullPct > 50 ? "pnl-neg" : ""}`}>
                          {nullPct !== null ? `${nullPct}%` : "—"}
                        </td>
                        <td className="mono">{c.mean !== undefined ? c.mean : "—"}</td>
                        <td className="mono">{c.std !== undefined ? c.std : "—"}</td>
                        <td className="mono">
                          {c.corr_target !== undefined && c.corr_target !== null
                            ? c.corr_target.toFixed(3)
                            : "—"}
                        </td>
                        <td className="mono">
                          {c.leak_warning && (
                            <span
                              className="pnl-neg"
                              title="This FEATURE tracks the target almost perfectly — that's look-ahead leakage, not alpha. Do not train on it."
                            >
                              ⚠ LEAK
                            </span>
                          )}
                          {c.std === 0 && !c.leak_warning && c.numeric && (
                            <span className="meta" title="Constant — carries no signal">
                              constant
                            </span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )
        )}
      </div>

      {/* Preview */}
      <div className="widget widget-wide">
        <h3 style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          Preview{" "}
          <span className="meta">
            {rowsResp ? `${rowsResp.count} of ${rowsResp.total} rows` : ""}
          </span>
          <span style={{ flex: 1 }} />
          <select
            value={filters.source ?? ""}
            onChange={(e) =>
              setFilters((f) => ({ ...f, source: e.target.value || undefined }))
            }
            style={{ width: "auto", padding: "2px 6px" }}
            title="signal = the bot acted or was risk-blocked; shadow = analyzed but never signaled (the negatives)"
          >
            <option value="">Signal + shadow</option>
            <option value="signal">Signal rows</option>
            <option value="shadow">Shadow rows</option>
          </select>
          <select
            value={filters.event_type ?? ""}
            onChange={(e) =>
              setFilters((f) => ({ ...f, event_type: e.target.value || undefined }))
            }
            style={{ width: "auto", padding: "2px 6px" }}
          >
            <option value="">All events</option>
            {(stats?.by_event_type ?? [])
              .filter((e) => e.event_type !== "UNKNOWN")
              .map((e) => (
                <option key={e.event_type} value={e.event_type}>
                  {e.event_type} ({e.samples})
                </option>
              ))}
          </select>
          <select
            value={filters.action ?? ""}
            onChange={(e) =>
              setFilters((f) => ({ ...f, action: e.target.value || undefined }))
            }
            style={{ width: "auto", padding: "2px 6px" }}
          >
            <option value="">BUY + SELL</option>
            <option value="BUY">BUY</option>
            <option value="SELL">SELL</option>
          </select>
          <select
            value={filters.taken ?? ""}
            onChange={(e) =>
              setFilters((f) => ({ ...f, taken: e.target.value || undefined }))
            }
            style={{ width: "auto", padding: "2px 6px" }}
          >
            <option value="">Taken + blocked</option>
            <option value="taken">Taken only</option>
            <option value="blocked">Blocked only</option>
          </select>
          <select
            value={filters.label ?? ""}
            onChange={(e) =>
              setFilters((f) => ({ ...f, label: e.target.value || undefined }))
            }
            style={{ width: "auto", padding: "2px 6px" }}
          >
            <option value="">Any label</option>
            <option value="UP">UP</option>
            <option value="DOWN">DOWN</option>
            <option value="FLAT">FLAT</option>
          </select>
          <label className="meta" style={{ cursor: "pointer" }}>
            <input
              type="checkbox"
              checked={filters.enriched_only ?? false}
              onChange={(e) =>
                setFilters((f) => ({
                  ...f,
                  enriched_only: e.target.checked || undefined,
                }))
              }
              style={{ marginRight: 4 }}
            />
            enriched only
          </label>
          <select
            value={limit}
            onChange={(e) => setLimit(Number(e.target.value))}
            style={{ width: "auto", padding: "2px 6px" }}
          >
            {[25, 50, 100, 250].map((n) => (
              <option key={n} value={n}>{n} rows</option>
            ))}
          </select>
        </h3>
        {isLoading ? (
          <p className="empty">Loading…</p>
        ) : error ? (
          <p className="empty">Failed to load dataset: {error.message}</p>
        ) : !rowsResp || rowsResp.rows.length === 0 ? (
          <p className="empty">
            No rows yet — every signal becomes a dataset row automatically
            (outcome probes at +5m/+30m, then 1-min-candle enrichment ~16
            minutes after the signal). Use “Enrich now” to backfill history.
          </p>
        ) : (
          <div style={{ overflowX: "auto", maxWidth: "100%" }}>
            <table style={{ whiteSpace: "nowrap" }}>
              <thead>
                <tr>
                  {orderedSelection.map((k) => {
                    const col = catalog?.columns.find((c) => c.key === k);
                    return (
                      <th key={k} title={col ? `${col.description} — ${col.role}` : k}>
                        <span
                          className={
                            col?.role === "target"
                              ? "pnl-neg"
                              : col?.role === "feature"
                                ? "pnl-pos"
                                : undefined
                          }
                        >
                          {k}
                        </span>
                      </th>
                    );
                  })}
                </tr>
              </thead>
              <tbody>
                {rowsResp.rows.map((r, i) => (
                  <tr key={(r["outcome_id"] as number) ?? i}>
                    {orderedSelection.map((k) => (
                      <td
                        key={k}
                        className="mono"
                        title={r[k] === null || r[k] === undefined ? "" : String(r[k])}
                      >
                        {fmtCell(r[k])}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
