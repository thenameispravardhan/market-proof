// RunList — list of backtest runs with status indicators.
import { useBacktestRuns, useDeleteBacktestRun } from "../../hooks/useApi";
import type { BacktestRun } from "../../types";

interface Props {
  selectedId: number | null;
  onSelect: (id: number) => void;
}

const STATUS_CLASS: Record<string, string> = {
  done: "info",
  running: "warn",
  pending: "warn",
  failed: "danger",
};

function fmtDate(d: string): string {
  return new Date(d).toLocaleString();
}

export function RunList({ selectedId, onSelect }: Props) {
  const { data: runs, isLoading } = useBacktestRuns();
  const deleteRun = useDeleteBacktestRun();

  const handleDelete = async (r: BacktestRun) => {
    if (!confirm(`Delete backtest run "${r.name ?? r.id}"?`)) return;
    await deleteRun.mutateAsync(r.id);
  };

  return (
    <div className="widget" data-testid="run-list">
      <h3>Backtest Runs</h3>
      {isLoading ? (
        <p className="empty">Loading…</p>
      ) : !runs || runs.length === 0 ? (
        <p className="empty">No runs yet. Create one with the button below.</p>
      ) : (
        <div className="list">
          {runs.map((r: BacktestRun) => (
            <div
              key={r.id}
              className={`item${selectedId === r.id ? " selected" : ""}`}
              onClick={() => onSelect(r.id)}
            >
              <div className="head">
                <span className="symbol">{r.name ?? `Run #${r.id}`}</span>
                <span className={`badge ${STATUS_CLASS[r.status] ?? "warn"}`}>
                  {r.status}
                </span>
              </div>
              <div className="body">
                {r.start_date} → {r.end_date}
              </div>
              <div className="body reason">
                Capital: ₹{r.initial_capital.toLocaleString()} · {fmtDate(r.created_at)}
              </div>
              {r.status === "done" && r.metrics && (
                <div className="body reason mono" style={{ fontSize: 12 }}>
                  Return: {typeof r.metrics.total_return_pct === "number" ? r.metrics.total_return_pct.toFixed(2) + "%" : "—"} ·
                  Sharpe: {typeof r.metrics.sharpe === "number" ? r.metrics.sharpe.toFixed(2) : "—"}
                </div>
              )}
              <div className="body reason" style={{ marginTop: 6 }}>
                <button
                  className="btn-sm danger"
                  onClick={(e) => { e.stopPropagation(); handleDelete(r); }}
                >
                  Delete
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
