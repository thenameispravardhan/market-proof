// Active positions: open paper/live positions with quantity, average
// price, last price, and unrealised P&L. Now with trade-management
// controls — close a single position or square off everything — and a
// managed-book overlay showing each position's live stop-loss / target.

import { useState } from "react";
import {
  useCloseAllPositions,
  useClosePosition,
  useGlobalSettings,
  useManagedPositions,
  usePositions,
} from "../../hooks/useApi";
import { useLiveQuote } from "../../hooks/useQuotes";
import type { ManagedPosition, Position } from "../../types";
import { ApiClientError } from "../../api/client";
import { displayQty, isLeveraged } from "../../lib/leverage";
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

// One open-position row. Overlays the live `/ws` mark over the 5s REST
// poll and recomputes unrealised P&L from it; (last - avg) * qty handles
// both long and short (qty is negative for shorts). Falls back to the REST
// values until the first tick for this symbol streams in.
function PositionRow({
  p,
  m,
  busy,
  leverage,
  onClose,
}: {
  p: Position;
  m: ManagedPosition | undefined;
  busy: string | null;
  leverage: number;
  onClose: (symbol: string) => void;
}) {
  const live = useLiveQuote(p.symbol);
  const ltp = live?.last_price ?? p.last_price;
  // uPnL stays on the full (leveraged) qty — only the printed share
  // count is scaled to the 1× view.
  const pnl =
    live?.last_price != null
      ? (live.last_price - p.average_price) * p.quantity
      : p.unrealized_pnl;
  return (
    <tr>
      <td className="mono symbol">{p.symbol}</td>
      <td className="mono">{displayQty(p.quantity, leverage)}</td>
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
  const { data: settings } = useGlobalSettings();
  const closeOne = useClosePosition();
  const closeAll = useCloseAllPositions();
  const [busy, setBusy] = useState<string | null>(null);
  // Display-only 1× view (see lib/leverage). Missing settings → 1 (no-op).
  const leverage = settings?.global?.INTRADAY_LEVERAGE ?? 1;

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
              <th
                className="mono"
                title={
                  isLeveraged(leverage)
                    ? `Shown at 1× (raw fill size ÷ ${leverage} leverage). uPnL is the full leveraged amount.`
                    : undefined
                }
              >
                Qty{isLeveraged(leverage) ? " (1×)" : ""}
              </th>
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
                leverage={leverage}
                onClose={onClose}
              />
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
