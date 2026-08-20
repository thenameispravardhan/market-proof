// Trade History — executed trades as round-trips (entry + exit paired
// into one row), split into two sections: Fyers and Paper Trading.
//
// Round-trips are reconstructed by `buildRoundTrips` (shared with the
// dashboard P&L chart so the two views agree): filled legs are paired
// FIFO per symbol to show entry price, exit price, quantity and P&L on
// a single row. A position that hasn't been closed yet shows as "OPEN"
// with no exit/P&L — its stop-loss / target is editable inline.

import { useMemo } from "react";
import {
  useBrokerAccounts,
  useDeleteTrades,
  useManagedPositions,
  useTrades,
} from "../hooks/useApi";
import { LevelsCell } from "../components/positions/LevelsCell";
import { buildRoundTrips, type RoundTrip } from "../lib/roundtrips";
import type { ManagedPosition } from "../types";

function fmtMoney(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return v.toLocaleString("en-IN", { maximumFractionDigits: 2, minimumFractionDigits: 2 });
}

function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  let s = iso.trim();
  if (!/Z$|[+-]\d{2}:?\d{2}$/.test(s)) s += "Z";
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return iso;
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata",
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(d);
}

function pnlClass(v: number | null | undefined): string {
  if (!v) return "";
  return v > 0 ? "pnl-pos" : v < 0 ? "pnl-neg" : "";
}

function Section({
  title,
  rows,
  managedBy,
  onDelete,
}: {
  title: string;
  rows: RoundTrip[];
  managedBy: Map<string, ManagedPosition>;
  onDelete: (r: RoundTrip) => void;
}) {
  const realised = rows.reduce((acc, r) => acc + (r.pnl ?? 0), 0);
  return (
    <div className="widget widget-wide" data-testid={`trades-${title.toLowerCase().includes("fyers") ? "fyers" : "paper"}`}>
      <h3>
        {title}
        <span className="mono" style={{ fontWeight: 400 }}>
          {rows.length} trade{rows.length === 1 ? "" : "s"} · P&amp;L{" "}
          <span className={pnlClass(realised)}>₹{fmtMoney(realised)}</span>
        </span>
      </h3>
      {rows.length === 0 ? (
        <p className="empty">No executed trades on this account yet.</p>
      ) : (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Side</th>
                <th className="mono">Qty</th>
                <th className="mono">Entry</th>
                <th className="mono">Exit</th>
                <th className="mono">SL / Target</th>
                <th className="mono">P&amp;L</th>
                <th>Entry (IST)</th>
                <th>Closed (IST)</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const m = managedBy.get(r.symbol);
                return (
                  <tr key={r.key}>
                    <td className="mono">{r.symbol}</td>
                    <td>
                      <span className={`badge ${r.side === "BUY" ? "buy" : "sell"}`}>{r.side}</span>
                    </td>
                    <td className="mono">{r.quantity}</td>
                    <td className="mono">{fmtMoney(r.entryPrice)}</td>
                    <td className="mono">
                      {r.open ? <span className="badge warn">OPEN</span> : fmtMoney(r.exitPrice)}
                    </td>
                    <td className="mono">
                      {r.open ? (
                        <LevelsCell
                          symbol={r.symbol}
                          stopLoss={m?.stop_loss ?? null}
                          target={m?.target ?? null}
                        />
                      ) : (
                        <span className="text-dim">—</span>
                      )}
                    </td>
                    <td className={`mono ${pnlClass(r.pnl)}`}>
                      {r.open ? "—" : fmtMoney(r.pnl)}
                    </td>
                    <td className="mono">{fmtTime(r.entryTime)}</td>
                    <td className="mono">{r.open ? "—" : fmtTime(r.exitTime)}</td>
                    <td>
                      {/* Open rows are deletable too: most are phantoms —
                          an entry leg whose exit never got recorded — not
                          live positions. The API still refuses (409) if the
                          symbol has a real open position, so a genuinely
                          live one cannot be removed from under the trade
                          manager. */}
                      <button
                        className="btn-sm danger"
                        title={
                          r.open
                            ? "Delete this open entry leg from history"
                            : "Delete this round-trip from history"
                        }
                        aria-label={`Delete ${r.symbol} ${r.open ? "open leg" : "round-trip"}`}
                        onClick={() => onDelete(r)}
                      >
                        ✕
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default function TradeHistory() {
  // Filled-only: a burst of rejected HOLD signals must not push real fills
  // (especially older entry legs) out of the newest-N window, or a closed
  // round-trip would lose its entry and show as a phantom open position.
  const { data: trades, isLoading, error } = useTrades(500, "filled");
  const del = useDeleteTrades();
  const { data: accounts } = useBrokerAccounts();
  const { data: managed } = useManagedPositions();

  const byId = useMemo(
    () => new Map((accounts ?? []).map((a) => [a.id, a])),
    [accounts]
  );
  const managedBy = useMemo(
    () => new Map((managed ?? []).map((m) => [m.symbol, m])),
    [managed]
  );
  const roundtrips = useMemo(
    () => buildRoundTrips(trades ?? [], byId),
    [trades, byId]
  );
  const fyers = roundtrips.filter((r) => !r.isPaper);
  const paper = roundtrips.filter((r) => r.isPaper);

  // A displayed row is a *slice* of trade rows, not a row itself: a
  // partially closed lot (BUY 10 / SELL 4 leaves an open 6) puts the same
  // trade behind two displayed rows. So delete the whole connected
  // cluster — every row reachable through a shared leg — and name it up
  // front, instead of letting the neighbours vanish unexplained.
  function clusterOf(r: RoundTrip): RoundTrip[] {
    const picked = [r];
    const ids = new Set(r.tradeIds);
    for (let grew = true; grew; ) {
      grew = false;
      for (const o of roundtrips) {
        if (picked.includes(o)) continue;
        if (o.tradeIds.some((id) => ids.has(id))) {
          picked.push(o);
          o.tradeIds.forEach((id) => ids.add(id));
          grew = true;
        }
      }
    }
    return picked;
  }

  async function handleDelete(r: RoundTrip) {
    const group = clusterOf(r);
    const ids = [...new Set(group.flatMap((g) => g.tradeIds))];
    const pnl = group.reduce((a, g) => a + (g.pnl ?? 0), 0);

    const what =
      group.length === 1
        ? r.open
          ? `the OPEN ${r.symbol} ${r.side} entry leg (qty ${r.quantity})`
          : `the ${r.symbol} ${r.side} round-trip (P&L ₹${fmtMoney(r.pnl)})`
        : `${group.length} linked ${r.symbol} rows (P&L ₹${fmtMoney(pnl)})`;

    if (
      !confirm(
        `Delete ${what} from history?\n\n` +
          (group.length > 1
            ? `These rows share ${ids.length} trade record(s) and cannot be ` +
              "separated, so they go together.\n\n"
            : "") +
          "This removes them from win rate, performance sizing and the " +
          "daily-loss breaker. Each row is recorded in the audit log."
      )
    )
      return;

    try {
      await del.mutateAsync(ids);
    } catch (e) {
      alert(`Could not delete: ${(e as Error).message}`);
    }
  }

  const realised = roundtrips.reduce((a, r) => a + (r.pnl ?? 0), 0);
  const closed = roundtrips.filter((r) => !r.open && r.pnl !== null);
  const wins = closed.filter((r) => (r.pnl ?? 0) > 0).length;
  const winRate = closed.length > 0 ? (wins / closed.length) * 100 : null;

  if (isLoading) {
    return (
      <div>
        <h1 className="page-title">Trade History</h1>
        <p className="empty">Loading…</p>
      </div>
    );
  }
  if (error) {
    return (
      <div>
        <h1 className="page-title">Trade History</h1>
        <p className="empty">Failed to load trades: {(error as Error).message}</p>
      </div>
    );
  }

  return (
    <div>
      <h1 className="page-title">Trade History</h1>

      <div className="stat-row">
        <div className="stat">
          <div className="stat-label">Round-trips</div>
          <div className="stat-value">{roundtrips.length}</div>
        </div>
        <div className="stat">
          <div className="stat-label">Realised P&amp;L</div>
          <div className={`stat-value ${pnlClass(realised)}`}>₹{fmtMoney(realised)}</div>
        </div>
        <div className="stat">
          <div className="stat-label">Win rate</div>
          <div className="stat-value">
            {winRate === null ? "—" : `${winRate.toFixed(0)}%`}
          </div>
        </div>
      </div>

      <div className="dashboard-grid">
        <Section
          title="Fyers — Executed Trades"
          rows={fyers}
          managedBy={managedBy}
          onDelete={handleDelete}
        />
        <Section
          title="Paper Trading — Executed Trades"
          rows={paper}
          managedBy={managedBy}
          onDelete={handleDelete}
        />
      </div>
    </div>
  );
}
