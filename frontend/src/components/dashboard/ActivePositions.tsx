// Active positions: open paper/live positions with quantity, average
// price, last price, and unrealised P&L. We colour the P&L green/red
// and pin the unrealised column to the right so it scans.

import { usePositions } from "../../hooks/useApi";
import { ApiClientError } from "../../api/client";
import { SkeletonList } from "./Skeleton";

function fmtMoney(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return v.toLocaleString(undefined, { maximumFractionDigits: 2, minimumFractionDigits: 2 });
}

function pnlClass(v: number | null | undefined): string {
  if (v === null || v === undefined) return "";
  if (v > 0) return "pnl-pos";
  if (v < 0) return "pnl-neg";
  return "";
}

export function ActivePositions() {
  const { data, isLoading, error } = usePositions();

  return (
    <div className="widget" data-testid="active-positions">
      <h3>Active Positions</h3>
      {isLoading ? (
        <SkeletonList rows={3} />
      ) : error ? (
        <p className="empty">
          {error instanceof ApiClientError && error.status === 404
            ? "Endpoint not available yet — backend doesn't expose positions."
            : `Failed to load: ${error.message}`}
        </p>
      ) : !data || data.length === 0 ? (
        <p className="empty">No open positions.</p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Symbol</th>
              <th className="mono">Qty</th>
              <th className="mono">Avg</th>
              <th className="mono">LTP</th>
              <th className="mono">uPnL</th>
            </tr>
          </thead>
          <tbody>
            {data.map((p) => (
              <tr key={p.id}>
                <td className="mono">{p.symbol}</td>
                <td className="mono">{p.quantity}</td>
                <td className="mono">{fmtMoney(p.average_price)}</td>
                <td className="mono">{fmtMoney(p.last_price)}</td>
                <td className={`mono ${pnlClass(p.unrealized_pnl)}`}>
                  {fmtMoney(p.unrealized_pnl)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
