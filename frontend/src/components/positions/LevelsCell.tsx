// Inline stop-loss / target editor for an open position. Used by both
// the dashboard's Active Positions table and the Trade History open
// rows so an operator can re-arm exits without re-placing the trade.
//
// Display mode shows "SL / Target" with an Edit button; edit mode swaps
// in two compact number inputs plus Save / Cancel. Saving POSTs to
// /api/positions/{symbol}/levels and refreshes the managed book.

import { useState } from "react";
import { useUpdatePositionLevels } from "../../hooks/useApi";

function fmt(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return v.toLocaleString(undefined, { maximumFractionDigits: 2, minimumFractionDigits: 2 });
}

// Empty string clears the level (null); anything else parses to a number.
function parse(v: string): number | null {
  const s = v.trim();
  if (s === "") return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
}

export function LevelsCell({
  symbol,
  stopLoss,
  target,
}: {
  symbol: string;
  stopLoss: number | null;
  target: number | null;
}) {
  const update = useUpdatePositionLevels();
  const [editing, setEditing] = useState(false);
  const [sl, setSl] = useState("");
  const [tgt, setTgt] = useState("");

  const begin = () => {
    setSl(stopLoss != null ? String(stopLoss) : "");
    setTgt(target != null ? String(target) : "");
    update.reset();
    setEditing(true);
  };

  const save = async () => {
    try {
      await update.mutateAsync({ symbol, stop_loss: parse(sl), target: parse(tgt) });
      setEditing(false);
    } catch {
      /* error surfaced inline below */
    }
  };

  if (editing) {
    return (
      <div style={{ display: "flex", alignItems: "center", gap: 4, flexWrap: "wrap" }}>
        <input
          aria-label={`Stop-loss for ${symbol}`}
          type="number"
          step="0.05"
          value={sl}
          placeholder="SL"
          onChange={(e) => setSl(e.target.value)}
          style={{ width: 72 }}
        />
        <input
          aria-label={`Target for ${symbol}`}
          type="number"
          step="0.05"
          value={tgt}
          placeholder="Target"
          onChange={(e) => setTgt(e.target.value)}
          style={{ width: 72 }}
        />
        <button
          className="btn-sm primary"
          onClick={save}
          disabled={update.isPending}
        >
          {update.isPending ? "…" : "Save"}
        </button>
        <button className="btn-sm" onClick={() => setEditing(false)} disabled={update.isPending}>
          Cancel
        </button>
        {update.isError && (
          <span className="pnl-neg" style={{ fontSize: 10 }}>
            {(update.error as Error).message}
          </span>
        )}
      </div>
    );
  }

  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
      <span className="text-dim">
        {stopLoss != null || target != null ? `${fmt(stopLoss)} / ${fmt(target)}` : "—"}
      </span>
      <button className="btn-sm" onClick={begin} title="Edit stop-loss / target">
        Edit
      </button>
    </span>
  );
}
