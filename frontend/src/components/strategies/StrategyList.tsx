// StrategyList — shows all strategies with enable/disable toggle.
import { useStrategies, useDeleteStrategy, useUpdateStrategy } from "../../hooks/useApi";
import type { Strategy } from "../../types";

interface Props {
  selectedId: number | null;
  onSelect: (id: number) => void;
  onNew: () => void;
}

export function StrategyList({ selectedId, onSelect, onNew }: Props) {
  const { data: strategies, isLoading } = useStrategies();
  const deleteStrategy = useDeleteStrategy();
  const updateStrategy = useUpdateStrategy(null);

  const handleDelete = async (s: Strategy) => {
    if (!confirm(`Delete strategy "${s.name}" and ALL its rules?`)) return;
    await deleteStrategy.mutateAsync(s.id);
  };

  const toggleEnabled = async (s: Strategy) => {
    // We need the update hook with the correct id — use a one-off approach
    try {
      await fetch(`/api/strategies/${s.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: !s.enabled }),
      });
      // invalidate via parent
    } catch {
      // silent
    }
  };

  return (
    <div className="widget" data-testid="strategy-list">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
        <h3 style={{ margin: 0 }}>Strategies</h3>
        <button className="btn-sm primary" onClick={onNew}>+ New</button>
      </div>
      {isLoading ? (
        <p className="empty">Loading…</p>
      ) : !strategies || strategies.length === 0 ? (
        <p className="empty">No strategies yet. Create one to get started.</p>
      ) : (
        <div className="list">
          {strategies.map((s: Strategy) => (
            <div
              key={s.id}
              className={`item${selectedId === s.id ? " selected" : ""}`}
              onClick={() => onSelect(s.id)}
            >
              <div className="head">
                <span className="symbol">{s.name}</span>
                <span className={`badge ${s.enabled ? "info" : "warn"}`}>
                  {s.enabled ? "enabled" : "disabled"}
                </span>
              </div>
              {s.description && <div className="body">{s.description}</div>}
              <div className="body reason" style={{ display: "flex", gap: 8, marginTop: 6 }}>
                <button
                  className="btn-sm"
                  onClick={(e) => { e.stopPropagation(); toggleEnabled(s); }}
                >
                  {s.enabled ? "Disable" : "Enable"}
                </button>
                <button
                  className="btn-sm danger"
                  onClick={(e) => { e.stopPropagation(); handleDelete(s); }}
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
