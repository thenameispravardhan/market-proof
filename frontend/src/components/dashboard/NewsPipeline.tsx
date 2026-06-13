// News Pipeline: one row per news item, showing its full journey from
// raw scrape (announcement) → AI verdict (analysis) → trade decision
// (signal). The three backend endpoints are joined client-side via
// the `announcement_id` / `analysis_id` foreign keys, so the operator
// sees the workflow in a single tabular view instead of three separate
// widget cards.
//
// The join is defensive: announcements without an analysis show "—"
// in the AI/Signal columns; analyses without a signal show "—"; etc.

import { useMemo } from "react";
import { useAnalyses, useAnnouncements, useSignals } from "../../hooks/useApi";
import { ApiClientError } from "../../api/client";
import { SkeletonList } from "./Skeleton";
import type { Analysis, Announcement, Signal } from "../../types";

function fmtTime(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return `${(v * 100).toFixed(0)}%`;
}

function sentimentClass(s: string | null | undefined): string {
  if (!s) return "neutral";
  if (s === "positive") return "positive";
  if (s === "negative") return "negative";
  return "neutral";
}

function actionClass(a: string | null | undefined): string {
  if (!a) return "neutral";
  if (a === "BUY") return "buy";
  if (a === "SELL") return "sell";
  if (a === "BLOCK") return "block";
  return "hold";
}

function statusClass(s: string): string {
  if (s === "filled" || s === "placed") return "positive";
  if (s === "rejected" || s === "blocked") return "negative";
  if (s === "pending") return "warn";
  return "neutral";
}

interface PipelineRow {
  announcement: Announcement;
  analysis: Analysis | null;
  signal: Signal | null;
}

export function NewsPipeline() {
  const ann = useAnnouncements(30);
  const ana = useAnalyses(30);
  const sig = useSignals(30);

  const isLoading = ann.isLoading || ana.isLoading || sig.isLoading;
  const error = ann.error ?? ana.error ?? sig.error;

  // Join the three datasets by foreign key. We build the rows from the
  // announcements list (the source of truth) and look up the matching
  // analysis / signal via maps. This guarantees every news item gets
  // a row even before its analysis / signal lands.
  const rows: PipelineRow[] = useMemo(() => {
    if (!ann.data) return [];
    const anaByAnn = new Map<number, Analysis>(
      (ana.data ?? []).map((a) => [a.announcement_id, a])
    );
    const sigByAna = new Map<number, Signal>(
      (sig.data ?? []).map((s) => [s.analysis_id ?? -1, s]).filter(([k]) => k !== -1)
    );
    return ann.data.map((a) => {
      const analysis = anaByAnn.get(a.id) ?? null;
      const signal = analysis ? sigByAna.get(analysis.id) ?? null : null;
      return { announcement: a, analysis, signal };
    });
  }, [ann.data, ana.data, sig.data]);

  return (
    <div className="widget widget-wide" data-testid="news-pipeline">
      <h3>
        News Pipeline
        <span className="meta">
          scrape → analyze → signal ({rows.length})
        </span>
      </h3>
      {isLoading ? (
        <SkeletonList rows={5} />
      ) : error ? (
        <p className="empty">
          {error instanceof ApiClientError && error.status === 404
            ? "Endpoint not available yet — backend doesn't expose the pipeline."
            : `Failed to load: ${error.message}`}
        </p>
      ) : rows.length === 0 ? (
        <p className="empty">No news yet. Waiting for the first scrape…</p>
      ) : (
        <div className="table-scroll">
          <table className="table table-pipeline">
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Exch</th>
                <th>Event</th>
                <th>Headline</th>
                <th>Filed</th>
                <th>AI Sentiment</th>
                <th>AI Reco</th>
                <th className="mono">AI Conf</th>
                <th>Signal</th>
                <th className="mono">Sig Conf</th>
                <th>Status</th>
                <th>Rationale</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(({ announcement: a, analysis: x, signal: s }) => (
                <tr key={a.id}>
                  <td className="mono" title={a.symbol}>{a.symbol}</td>
                  <td title={a.exchange}>{a.exchange}</td>
                  <td>
                    <span className={`badge ${actionClass(a.event_type)}`} title={a.event_type}>
                      {a.event_type}
                    </span>
                  </td>
                  <td className="cell-text" title={a.headline}>
                    {a.headline}
                  </td>
                  <td className="cell-text mono">{fmtTime(a.filed_at)}</td>
                  <td>
                    {x ? (
                      <span className={`badge ${sentimentClass(x.sentiment)}`}>
                        {x.sentiment ?? "—"}
                      </span>
                    ) : (
                      <span className="text-dim">—</span>
                    )}
                  </td>
                  <td>
                    {x?.recommendation ? (
                      <span className={`badge ${actionClass(x.recommendation)}`}>
                        {x.recommendation}
                      </span>
                    ) : (
                      <span className="text-dim">—</span>
                    )}
                  </td>
                  <td className="mono">{x ? fmtPct(x.confidence) : "—"}</td>
                  <td>
                    {s ? (
                      <span className={`badge ${actionClass(s.action)}`} title={s.action}>
                        {s.action}
                      </span>
                    ) : (
                      <span className="text-dim">—</span>
                    )}
                  </td>
                  <td className="mono">{s ? fmtPct(s.confidence) : "—"}</td>
                  <td>
                    {s ? (
                      <span className={`badge ${statusClass(s.status)}`} title={s.status}>
                        {s.status}
                      </span>
                    ) : (
                      <span className="text-dim">—</span>
                    )}
                  </td>
                  <td className="cell-text" title={s?.rationale ?? x?.rationale ?? ""}>
                    {s?.rationale ?? x?.rationale ?? "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
