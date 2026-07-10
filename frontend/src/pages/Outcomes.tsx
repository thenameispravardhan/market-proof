// Signal Outcomes — the closed-loop feedback view (Phase 4 completion).
//
// What the stock ACTUALLY did after every signal (approved and blocked
// alike): price at signal → +5m → +30m, whether the AI's direction was
// right, and whether the trade was taken. The per-event-type summary is
// the empirical answer to "which events should my rules trade?" — and
// the CSV export is the offline ML training set.

import { useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  useOutcomes,
  useOutcomesSummary,
  type OutcomeRow,
} from "../hooks/useApi";

const POS = "#3fb950";
const NEG = "#f85149";

function pct(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(2)}%`;
}

function pctClass(v: number | null | undefined): string {
  if (v === null || v === undefined) return "";
  return v > 0 ? "pnl-pos" : v < 0 ? "pnl-neg" : "";
}

function fmtPrice(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return `₹${v.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso.endsWith("Z") ? iso : iso + "Z");
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-IN", {
    timeZone: "Asia/Kolkata",
    day: "2-digit", month: "short",
    hour: "2-digit", minute: "2-digit",
  });
}

function VerdictBadge({ row }: { row: OutcomeRow }) {
  if (row.ai_was_correct === null) return <span className="mono">—</span>;
  return (
    <span className={`mono ${row.ai_was_correct ? "pnl-pos" : "pnl-neg"}`}>
      {row.ai_was_correct ? "✓ right" : "✗ wrong"}
    </span>
  );
}

export default function Outcomes() {
  const [eventFilter, setEventFilter] = useState<string>("");
  const { data: rows, isLoading, error } = useOutcomes(300, eventFilter || undefined);
  const { data: summary } = useOutcomesSummary(1000);

  const eventTypes = useMemo(
    () => (summary?.by_event_type ?? []).map((s) => s.event_type),
    [summary]
  );
  const chartData = useMemo(
    () =>
      (summary?.by_event_type ?? [])
        .filter((s) => s.avg_move_5m_pct !== null)
        .map((s) => ({
          event_type: s.event_type,
          avg5m: s.avg_move_5m_pct as number,
          samples: s.samples,
        })),
    [summary]
  );

  return (
    <div>
      <div className="dashboard-head">
        <h1 className="page-title">Signal Outcomes</h1>
        <a
          className="chart-btn"
          style={{ border: "1px solid var(--border)", textDecoration: "none" }}
          href="/api/outcomes/export.csv?limit=5000"
          download
        >
          Export CSV
        </a>
      </div>

      {summary && chartData.length > 0 && (
        <div className="widget widget-wide" style={{ marginBottom: 12 }}>
          <h3>
            Avg 5-minute move by event type{" "}
            <span className="meta">{summary.total} outcomes</span>
          </h3>
          <div style={{ width: "100%", height: 200, padding: "8px 12px" }}>
            <ResponsiveContainer>
              <BarChart data={chartData} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
                <CartesianGrid stroke="#30363d" strokeDasharray="3 3" />
                <XAxis dataKey="event_type" stroke="#8b949e" fontSize={10} interval={0} />
                <YAxis stroke="#8b949e" fontSize={11} width={48} unit="%" />
                <Tooltip
                  contentStyle={{ background: "#161b22", border: "1px solid #30363d" }}
                  formatter={(v: number, _n, item) => [
                    `${pct(v)} over ${(item?.payload as { samples?: number })?.samples ?? "?"} samples`,
                    "avg 5m move",
                  ]}
                />
                <Bar dataKey="avg5m" radius={[2, 2, 0, 0]}>
                  {chartData.map((d) => (
                    <Cell key={d.event_type} fill={d.avg5m < 0 ? NEG : POS} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {summary && summary.by_event_type.length > 0 && (
        <div className="widget widget-wide" style={{ marginBottom: 12 }}>
          <h3>Performance by event type</h3>
          <table>
            <thead>
              <tr>
                <th>Event</th>
                <th>Samples</th>
                <th>Avg 5m</th>
                <th>Avg 30m</th>
                <th>Accuracy (5m)</th>
                <th>Taken</th>
                <th>Blocked</th>
              </tr>
            </thead>
            <tbody>
              {summary.by_event_type.map((s) => (
                <tr key={s.event_type}>
                  <td className="mono">{s.event_type}</td>
                  <td className="mono">{s.samples}</td>
                  <td className={`mono ${pctClass(s.avg_move_5m_pct)}`}>
                    {pct(s.avg_move_5m_pct)}
                  </td>
                  <td className={`mono ${pctClass(s.avg_move_30m_pct)}`}>
                    {pct(s.avg_move_30m_pct)}
                  </td>
                  <td className="mono">
                    {s.accuracy_5m_pct !== null ? `${s.accuracy_5m_pct}%` : "—"}
                  </td>
                  <td className="mono">{s.taken}</td>
                  <td className="mono">{s.blocked}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="widget widget-wide">
        <h3>
          Outcomes{" "}
          <span className="meta">
            <select
              value={eventFilter}
              onChange={(e) => setEventFilter(e.target.value)}
              style={{ width: "auto", padding: "2px 6px" }}
            >
              <option value="">All event types</option>
              {eventTypes.map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
          </span>
        </h3>
        {isLoading ? (
          <p className="empty">Loading…</p>
        ) : error ? (
          <p className="empty">Failed to load outcomes: {error.message}</p>
        ) : !rows || rows.length === 0 ? (
          <p className="empty">
            No outcomes recorded yet — the outcome logger samples every
            signal at +5m and +30m once signals start flowing.
          </p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>When (IST)</th>
                <th>Symbol</th>
                <th>Action</th>
                <th>Event</th>
                <th>Conf</th>
                <th>At signal</th>
                <th>+5m</th>
                <th>+30m</th>
                <th>5m move</th>
                <th>30m move</th>
                <th>AI verdict</th>
                <th>Taken</th>
                <th>Note</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.outcome_id}>
                  <td className="mono">{fmtTime(r.created_at)}</td>
                  <td className="mono">{r.symbol}</td>
                  <td className={`mono ${r.action === "BUY" ? "pnl-pos" : "pnl-neg"}`}>
                    {r.action}
                  </td>
                  <td className="mono">{r.event_type ?? "—"}</td>
                  <td className="mono">
                    {r.confidence !== null ? `${Math.round(r.confidence * 100)}%` : "—"}
                  </td>
                  <td className="mono">{fmtPrice(r.price_at_signal)}</td>
                  <td className="mono">{fmtPrice(r.price_5m)}</td>
                  <td className="mono">{fmtPrice(r.price_30m)}</td>
                  <td className={`mono ${pctClass(r.move_5m_pct)}`}>{pct(r.move_5m_pct)}</td>
                  <td className={`mono ${pctClass(r.move_30m_pct)}`}>{pct(r.move_30m_pct)}</td>
                  <td><VerdictBadge row={r} /></td>
                  <td className="mono">{r.trade_taken ? "yes" : "no"}</td>
                  <td className="mono">{r.note ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
