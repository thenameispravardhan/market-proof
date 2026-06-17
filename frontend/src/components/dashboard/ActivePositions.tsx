// Active positions: open paper/live positions with quantity, average
// price, last price, and unrealised P&L. Now with trade-management
// controls — close a single position or square off everything — and a
// managed-book overlay showing each position's live stop-loss / target.

import { useState } from "react";
import {
  useCloseAllPositions,
  useClosePosition,
  useManagedPositions,
  usePositions,
} from "../../hooks/useApi";
import { ApiClientError } from "../../api/client";
import { LevelsCell } from "../positions/LevelsCell";
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
  const { data: managed } = useManagedPositions();
  const closeOne = useClosePosition();
  const closeAll = useCloseAllPositions();
  const [busy, setBusy] = useState<string | null>(null);

  const open = (data ?? []).filter((p) => p.quantity !== 0);
  const managedBy = new Map((managed ?? []).map((m) => [m.symbol, m]));

  const onClose = async (symbol: string) => {
    setBusy(symbol);
    try {
      await closeOne.mutateAsync(symbol);
    } catch {
      /* surfaced via react-query; ignore here */
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="widget widget-wide" data-testid="active-positions">
      <h3>
        Active Positions
        {open.length > 0 && (
          <button
            className="btn-sm danger"
            onClick={() => closeAll.mutate()}
            disabled={closeAll.isPending}
            title="Square off every open position at market"
          >
            {closeAll.isPending ? "Closing…" : "Square off all"}
          </button>
        )}
      </h3>
      {isLoading ? (
        <SkeletonList rows={3} />
      ) : error ? (
        <p className="empty">
          {error instanceof ApiClientError && error.status === 404
            ? "Endpoint not available yet — backend doesn't expose positions."
            : `Failed to load: ${error.message}`}
        </p>
      ) : open.length === 0 ? (
        <p className="empty">No open positions.</p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Symbol</th>
              <th className="mono">Qty</th>
              <th className="mono">Avg</th>
              <th className="mono">LTP</th>
              <th className="mono">SL / Target</th>
              <th className="mono">uPnL</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {open.map((p) => {
              const m = managedBy.get(p.symbol);
              return (
                <tr key={p.id}>
                  <td className="mono symbol">{p.symbol}</td>
                  <td className="mono">{p.quantity}</td>
                  <td className="mono">{fmtMoney(p.average_price)}</td>
                  <td className="mono">{fmtMoney(p.last_price)}</td>
                  <td className="mono">
                    <LevelsCell
                      symbol={p.symbol}
                      stopLoss={m?.stop_loss ?? null}
                      target={m?.target ?? null}
                    />
                  </td>
                  <td className={`mono ${pnlClass(p.unrealized_pnl)}`}>
                    {fmtMoney(p.unrealized_pnl)}
                  </td>
                  <td>
                    <button
                      className="btn-sm"
                      onClick={() => onClose(p.symbol)}
                      disabled={busy === p.symbol}
                      title="Close this position at market"
                    >
                      {busy === p.symbol ? "…" : "Close"}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
