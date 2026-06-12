// Trade signals: post-rule-engine action plan. Each row shows the
// symbol, action badge, confidence, and the rationale. We also show
// the current status badge so operators can see whether a signal is
// still pending or already placed/filled/rejected.

import { useSignals } from "../../hooks/useApi";
import { ApiClientError } from "../../api/client";
import { SkeletonList } from "./Skeleton";

function actionClass(a: string): string {
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

export function TradeSignals() {
  const { data, isLoading, error } = useSignals(20);

  return (
    <div className="widget" data-testid="trade-signals">
      <h3>Trade Signals</h3>
      {isLoading ? (
        <SkeletonList rows={4} />
      ) : error ? (
        <p className="empty">
          {error instanceof ApiClientError && error.status === 404
            ? "Endpoint not available yet — backend doesn't expose signals."
            : `Failed to load: ${error.message}`}
        </p>
      ) : !data || data.length === 0 ? (
        <p className="empty">No recent signals.</p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Action</th>
              <th className="mono">Conf</th>
              <th>Status</th>
              <th>Rationale</th>
            </tr>
          </thead>
          <tbody>
            {data.map((s) => (
              <tr key={s.id}>
                <td className="mono">{s.symbol}</td>
                <td>
                  <span className={`badge ${actionClass(s.action)}`}>{s.action}</span>
                </td>
                <td className="mono">
                  {s.confidence !== null && s.confidence !== undefined
                    ? `${(s.confidence * 100).toFixed(0)}%`
                    : "—"}
                </td>
                <td>
                  <span className={`badge ${statusClass(s.status)}`}>{s.status}</span>
                </td>
                <td>{s.rationale ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
