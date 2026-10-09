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
import { useLiveQuote } from "../../hooks/useQuotes";
import type { ManagedPosition, Position } from "../../types";
import { ApiClientError } from "../../api/client";
import { LevelsCell } from "../positions/LevelsCell";
import { CAP_BADGE_CLASS, capTier } from "../../lib/marketCap";
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

// One open-position row. Overlays the live `/ws` mark over the 5s REST
// poll and recomputes unrealised P&L from it; (last - avg) * qty handles
// both long and short (qty is negative for shorts). Falls back to the REST
// values until the first tick for this symbol streams in.
function PositionRow({
  p,
  m,
  busy,
  onClose,
}: {
  p: Position;
  m: ManagedPosition | undefined;
  busy: string | null;
  onClose: (symbol: string) => void;
}) {
  const live = useLiveQuote(p.symbol);
  const ltp = live?.last_price ?? p.last_price;
  const pnl =
    live?.last_price != null
      ? (live.last_price - p.average_price) * p.quantity
      : p.unrealized_pnl;
  return (
    <tr>
      <td className="mono symbol">{p.symbol}</td>
      <td>
        <span className={`badge ${CAP_BADGE_CLASS[capTier(p.symbol)]}`}>
          {capTier(p.symbol)}
        </span>
      </td>
      <td className="mono">{p.quantity}</td>
      <td className="mono">{fmtMoney(p.average_price)}</td>
      <td className="mono">{fmtMoney(ltp)}</td>
      <td className="mono">
        <LevelsCell
          symbol={p.symbol}
          stopLoss={m?.stop_loss ?? null}
          target={m?.target ?? null}
        />
      </td>
      <td className={`mono ${pnlClass(pnl)}`}>{fmtMoney(pnl)}</td>
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
}

export function ActivePositions() {
  const { data, isLoading, error } = usePositions();
  const { data: managed } = useManagedPositions();
  const closeOne = useClosePosition();
  const closeAll = useCloseAllPositions();
  const [busy, setBusy] = useState<string | null>(null);
  const [closeError, setCloseError] = useState<string | null>(null);

  const open = (data ?? []).filter((p) => p.quantity !== 0);
  const managedBy = new Map((managed ?? []).map((m) => [m.symbol, m]));

  // Both buttons send market orders (real ones in live mode), so each asks
  // first, and a failure is shown instead of silently leaving the row open.
  const onClose = async (symbol: string) => {
    if (!window.confirm(`Close your ${symbol} position at market price?`)) return;
    setBusy(symbol);
    setCloseError(null);
    try {
      await closeOne.mutateAsync(symbol);
    } catch (e) {
      setCloseError(`Could not close ${symbol}: ${(e as Error).message}`);
    } finally {
      setBusy(null);
    }
  };

  const onCloseAll = () => {
    const n = open.length;
    if (!window.confirm(`Close all ${n} open position${n === 1 ? "" : "s"} at market price?`)) return;
    setCloseError(null);
    closeAll.mutate(undefined, {
      onError: (e) => setCloseError(`Could not square off: ${(e as Error).message}`),
    });
  };

  return (
    <div className="widget widget-wide" data-testid="active-positions">
      <h3>
        Active Positions
        {open.length > 0 && (
          <button
            className="btn-sm danger"
            onClick={onCloseAll}
            disabled={closeAll.isPending}
            title="Square off every open position at market"
          >
            {closeAll.isPending ? "Closing…" : "Square off all"}
          </button>
        )}
      </h3>
      {closeError && (
        <p className="empty pnl-neg" role="alert">
          {closeError}
        </p>
      )}
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
              <th>Cap</th>
              <th className="mono">Qty</th>
              <th className="mono">Avg</th>
              <th className="mono">LTP</th>
              <th className="mono">SL / Target</th>
              <th className="mono">uPnL</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {open.map((p) => (
              <PositionRow
                key={p.id}
                p={p}
                m={managedBy.get(p.symbol)}
                busy={busy}
                onClose={onClose}
              />
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
