// Pipeline Latency tile — the "latency budget" at a glance.
//
// Backed by /api/metrics/pipeline-latency (the Phase-0 timing
// instrumentation, finally surfaced): p50/p95/p99 of the end-to-end
// analysis time, the LLM share, the fast-track split, the pipeline-
// deadline hit rate, and a per-analysis sparkline so degradation is
// visible BEFORE it costs P&L. If p95 creeps from ~2.5s toward 4s,
// DeepSeek is congested — flip to fast-track-only until it recovers.

import { Area, AreaChart, ResponsiveContainer, Tooltip, YAxis } from "recharts";
import { usePipelineLatency } from "../../hooks/useApi";
import { Skeleton } from "./Skeleton";

function ms(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return v >= 1000 ? `${(v / 1000).toFixed(2)}s` : `${Math.round(v)}ms`;
}

// p95 of total_ms colors the headline: green under 3s, amber under 5s,
// red beyond — tuned around the measured ~2.5-2.7s baseline.
function p95Class(p95: number | null | undefined): string {
  if (p95 === null || p95 === undefined) return "";
  if (p95 < 3000) return "pnl-pos";
  if (p95 < 5000) return "lat-warn";
  return "pnl-neg";
}

export function PipelineLatency() {
  const { data, isLoading, error } = usePipelineLatency(100);

  const total = data?.latency.total_ms;
  const series = (data?.series ?? []).filter((p) => p.total_ms !== null);

  return (
    <div className="widget" data-testid="pipeline-latency">
      <h3>
        Pipeline Latency{" "}
        <span className={`mono ${p95Class(total?.p95)}`}>
          p95 {ms(total?.p95)}
        </span>
      </h3>
      {isLoading ? (
        <Skeleton height={150} />
      ) : error ? (
        <p className="empty">Failed to load metrics: {error.message}</p>
      ) : !data || data.window === 0 ? (
        <p className="empty">No analyses yet — metrics appear after the first filing is processed.</p>
      ) : (
        <div className="lat-body">
          <div className="lat-stats">
            <div className="lat-stat">
              <span className="lat-label">p50</span>
              <span className="mono">{ms(total?.p50)}</span>
            </div>
            <div className="lat-stat">
              <span className="lat-label">p99</span>
              <span className="mono">{ms(total?.p99)}</span>
            </div>
            <div className="lat-stat">
              <span className="lat-label">LLM p95</span>
              <span className="mono">{ms(data.latency.llm_ms.p95)}</span>
            </div>
            <div className="lat-stat">
              <span className="lat-label">Fast track</span>
              <span className="mono">
                {data.fast_track_pct !== null ? `${data.fast_track_pct}%` : "—"}
              </span>
            </div>
            <div className="lat-stat">
              <span className="lat-label">Deadline hits</span>
              <span
                className={`mono ${
                  (data.deadline.hit_rate_pct ?? 0) > 0 ? "pnl-neg" : ""
                }`}
              >
                {data.deadline.hit_rate_pct !== null
                  ? `${data.deadline.hit_rate_pct}%`
                  : "—"}
              </span>
            </div>
            <div className="lat-stat">
              <span className="lat-label">News age p95</span>
              <span className="mono">
                {data.news_age_s_at_start.p95 !== null
                  ? `${data.news_age_s_at_start.p95}s`
                  : "—"}
              </span>
            </div>
          </div>
          {series.length >= 2 ? (
            <div style={{ width: "100%", height: 64 }}>
              <ResponsiveContainer>
                <AreaChart
                  data={series}
                  margin={{ top: 4, right: 4, bottom: 0, left: 4 }}
                >
                  <defs>
                    <linearGradient id="latFill" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="#58a6ff" stopOpacity={0.4} />
                      <stop offset="100%" stopColor="#58a6ff" stopOpacity={0.04} />
                    </linearGradient>
                  </defs>
                  <YAxis hide domain={[0, "auto"]} />
                  <Tooltip
                    contentStyle={{ background: "#161b22", border: "1px solid #30363d" }}
                    labelFormatter={() => ""}
                    formatter={(v: number, _name, item) => [
                      `${ms(v)} (${(item?.payload as { track?: string })?.track ?? "?"})`,
                      "total",
                    ]}
                  />
                  <Area
                    type="monotone"
                    dataKey="total_ms"
                    stroke="#58a6ff"
                    strokeWidth={1.5}
                    fill="url(#latFill)"
                    isAnimationActive={false}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <p className="empty">Sparkline appears after a few analyses.</p>
          )}
        </div>
      )}
    </div>
  );
}
