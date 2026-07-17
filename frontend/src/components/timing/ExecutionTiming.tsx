// Execution Timing — where the wall-clock goes, layer by layer.
//
// Reconstructs each filing's journey from four measured timestamps
// (Trade → Signal → Analysis → Announcement) and shows how long it spent
// in each layer, plus the analysis sub-breakdown (PDF + LLM) from the
// pipeline-latency endpoint. The point: see which layer to attack when
// the bot feels slow — and remember detection is exchange lag you can't
// control, while analysis is the layer you can.

import type { CSSProperties } from "react";

import {
  useExecutionLatency,
  usePipelineLatency,
  type LatencyStats,
} from "../../hooks/useApi";

function ms(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return v >= 1000 ? `${(v / 1000).toFixed(2)}s` : `${Math.round(v)}ms`;
}

// The four end-to-end layers, in order, with a fixed color each.
const STAGES: {
  key: "detection_ms" | "analysis_ms" | "signal_to_order_ms" | "order_to_fill_ms";
  label: string;
  color: string;
  note: string;
}[] = [
  { key: "detection_ms", label: "Detection", color: "#8b949e", note: "exchange publish lag + scrape — not yours to control" },
  { key: "analysis_ms", label: "Analysis (PDF + AI)", color: "#58a6ff", note: "the bot's own processing — the layer you tune" },
  { key: "signal_to_order_ms", label: "Signal → Order", color: "#3fb950", note: "risk checks + order routing" },
  { key: "order_to_fill_ms", label: "Order → Fill", color: "#d29922", note: "broker round-trip (≈0 in paper mode)" },
];

const barTrack: CSSProperties = {
  background: "var(--surface-2, #21262d)",
  borderRadius: 4,
  height: 22,
  overflow: "hidden",
  flex: 1,
};

function StatTable({ stages }: { stages: Record<string, LatencyStats> }) {
  return (
    <table>
      <thead>
        <tr>
          <th>Layer</th>
          <th>p50</th>
          <th>p95</th>
          <th>p99</th>
          <th>max</th>
          <th>n</th>
        </tr>
      </thead>
      <tbody>
        {STAGES.map((s) => {
          const st = stages[s.key];
          return (
            <tr key={s.key}>
              <td>{s.label}</td>
              <td className="mono">{ms(st?.p50)}</td>
              <td className="mono">{ms(st?.p95)}</td>
              <td className="mono">{ms(st?.p99)}</td>
              <td className="mono">{ms(st?.max)}</td>
              <td className="mono">{st?.count ?? 0}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

export function ExecutionTiming() {
  const exec = useExecutionLatency(100);
  const pipe = usePipelineLatency(100);

  if (exec.isLoading) return <p className="empty">Loading…</p>;
  if (exec.error) return <p className="empty">Failed to load: {exec.error.message}</p>;

  const data = exec.data;
  const hasTrades = !!data && data.window > 0;

  // Bars scale to the largest layer p50 so the smallest is still visible.
  const p50s = STAGES.map((s) => data?.stages[s.key]?.p50 ?? 0);
  const maxP50 = Math.max(1, ...p50s);
  const totalP50 = p50s.reduce((a, b) => a + b, 0);

  // Analysis sub-breakdown (from pipeline-latency): PDF fetch / extract / LLM.
  const pl = pipe.data;
  const sub = pl
    ? [
        { label: "PDF fetch", v: pl.latency.pdf_fetch_ms.p50, color: "#6e7681" },
        { label: "PDF extract", v: pl.latency.pdf_extract_ms.p50, color: "#388bfd" },
        { label: "LLM (DeepSeek)", v: pl.latency.llm_ms.p50, color: "#58a6ff" },
      ]
    : [];
  const subMax = Math.max(1, ...sub.map((x) => x.v ?? 0));

  return (
    <div>
      {/* ---- headline waterfall --------------------------------------- */}
      <div className="widget" data-testid="execution-timing">
        <h3>
          Time per layer{" "}
          <span className="mono" style={{ color: "var(--muted, #8b949e)" }}>
            median end-to-end {ms(totalP50)}
          </span>
        </h3>
        {!hasTrades ? (
          <p className="empty">
            No filled trades yet — this fills in once orders execute (paper
            trades count too, so Monday&apos;s paper session will populate it).
          </p>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 10, marginTop: 8 }}>
            {STAGES.map((s) => {
              const st = data!.stages[s.key];
              const p50 = st?.p50 ?? 0;
              const pct = (p50 / maxP50) * 100;
              return (
                <div key={s.key}>
                  <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 2 }}>
                    <span style={{ fontWeight: 600 }}>{s.label}</span>
                    <span className="mono">
                      p50 {ms(p50)} · p95 {ms(st?.p95)}
                    </span>
                  </div>
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <div style={barTrack}>
                      <div
                        style={{
                          width: `${Math.max(pct, p50 > 0 ? 2 : 0)}%`,
                          height: "100%",
                          background: s.color,
                          transition: "width .3s",
                        }}
                      />
                    </div>
                  </div>
                  <div className="field-hint" style={{ marginTop: 2 }}>{s.note}</div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* ---- per-layer stats ------------------------------------------ */}
      <div className="widget" style={{ marginTop: 16 }}>
        <h3>Percentiles per layer</h3>
        {hasTrades ? (
          <StatTable stages={data!.stages} />
        ) : (
          <p className="empty">Awaiting filled trades.</p>
        )}
      </div>

      {/* ---- detection race: NSE vs BSE ------------------------------- */}
      <div className="widget" style={{ marginTop: 16 }}>
        <h3>
          Detection race — which exchange publishes faster{" "}
          <span className="mono" style={{ color: "var(--muted, #8b949e)" }}>
            (last {data?.detection_samples ?? 0} filings)
          </span>
        </h3>
        {data && Object.keys(data.detection_by_exchange).length > 0 ? (
          (() => {
            const entries = Object.entries(data.detection_by_exchange);
            const raceMax = Math.max(1, ...entries.map(([, st]) => st.p50 ?? 0));
            const colors: Record<string, string> = { NSE: "#f0883e", BSE: "#a371f7" };
            return (
              <div style={{ display: "flex", flexDirection: "column", gap: 10, marginTop: 8 }}>
                {entries.map(([exch, st]) => (
                  <div key={exch}>
                    <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 2 }}>
                      <span style={{ fontWeight: 600 }}>{exch}</span>
                      <span className="mono">
                        p50 {ms(st.p50)} · p95 {ms(st.p95)} · n={st.count}
                      </span>
                    </div>
                    <div style={barTrack}>
                      <div
                        style={{
                          width: `${((st.p50 ?? 0) / raceMax) * 100}%`,
                          height: "100%",
                          background: colors[exch] ?? "#8b949e",
                          transition: "width .3s",
                        }}
                      />
                    </div>
                  </div>
                ))}
                <p className="field-hint" style={{ marginTop: 0 }}>
                  Publish lag per exchange (filed → scraped), over every recent
                  filing. Shorter bar = faster publisher. Dual-listed stocks are
                  effectively detected at the FASTER exchange&apos;s speed — the
                  monitors race, first one in wins.
                </p>
                {Object.keys(data.monitor_ticks).length > 0 && (
                  <p className="field-hint" style={{ marginTop: 0 }}>
                    Bot&apos;s own share:{" "}
                    {Object.entries(data.monitor_ticks)
                      .map(
                        ([exch, t]) =>
                          `${exch} polls every ~${t.last_gap_s ?? "?"}s, fetch ${ms(t.avg_tick_ms)}`,
                      )
                      .join(" · ")}{" "}
                    — everything beyond that is the exchange&apos;s publish lag.
                  </p>
                )}
              </div>
            );
          })()
        ) : (
          <p className="empty">No filings with timestamps yet.</p>
        )}
      </div>

      {/* ---- analysis sub-breakdown ----------------------------------- */}
      <div className="widget" style={{ marginTop: 16 }}>
        <h3>
          Inside the Analysis layer{" "}
          <span className="mono" style={{ color: "var(--muted, #8b949e)" }}>
            (last {pl?.window ?? 0} analyses)
          </span>
        </h3>
        {pl && pl.window > 0 ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 10, marginTop: 8 }}>
            {sub.map((x) => (
              <div key={x.label}>
                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 2 }}>
                  <span style={{ fontWeight: 600 }}>{x.label}</span>
                  <span className="mono">p50 {ms(x.v)}</span>
                </div>
                <div style={barTrack}>
                  <div
                    style={{
                      width: `${((x.v ?? 0) / subMax) * 100}%`,
                      height: "100%",
                      background: x.color,
                      transition: "width .3s",
                    }}
                  />
                </div>
              </div>
            ))}
            <p className="field-hint" style={{ marginTop: 0 }}>
              The LLM call is almost always the dominant slice — if it creeps
              up, DeepSeek is congested. The Deterministic Fast Track (Settings)
              skips it for unambiguous headlines.
            </p>
          </div>
        ) : (
          <p className="empty">No analyses with timing yet.</p>
        )}
      </div>

      {/* ---- recent trades detail ------------------------------------- */}
      <div className="widget" style={{ marginTop: 16 }}>
        <h3>Recent filled trades</h3>
        {hasTrades ? (
          <table>
            <thead>
              <tr>
                <th>Trade</th>
                <th>Detection</th>
                <th>Analysis</th>
                <th>Sig→Order</th>
                <th>Order→Fill</th>
              </tr>
            </thead>
            <tbody>
              {data!.series.map((r) => (
                <tr key={r.trade_id}>
                  <td className="mono">
                    {r.side} {r.symbol}
                  </td>
                  <td className="mono">{ms(r.detection_ms)}</td>
                  <td className="mono">{ms(r.analysis_ms)}</td>
                  <td className="mono">{ms(r.signal_to_order_ms)}</td>
                  <td className="mono">{ms(r.order_to_fill_ms)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="empty">No filled trades yet.</p>
        )}
      </div>
    </div>
  );
}
