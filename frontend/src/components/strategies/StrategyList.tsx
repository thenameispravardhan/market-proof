// StrategyList — all strategies with a working on/off switch.
// A disabled strategy's rules are skipped by the live analyzer, so this
// switch is how you turn a whole strategy on or off.
import { useStrategies, useDeleteStrategy, useToggleStrategy } from "../../hooks/useApi";
import { Toggle } from "../common/Toggle";
import type { Strategy } from "../../types";

interface Props {
  selectedId: number | null;
  onSelect: (id: number) => void;
  onNew: () => void;
}

export function StrategyList({ selectedId, onSelect, onNew }: Props) {
  const { data: strategies, isLoading } = useStrategies();
  const deleteStrategy = useDeleteStrategy();
  const toggle = useToggleStrategy();

  const handleDelete = async (s: Strategy) => {
    if (!confirm(`Delete strategy "${s.name}" and ALL its rules?`)) return;
    await deleteStrategy.mutateAsync(s.id);
  };

  return (
    <div className="widget" data-testid="strategy-list">
      <div className="widget-header">
        <h3>
          Strategies
          <span className="meta">{strategies?.length ?? 0} total</span>
        </h3>
        <button className="btn-sm primary" onClick={onNew}>+ New</button>
      </div>
      <div className="widget-body" style={{ padding: 0 }}>
        {isLoading ? (
          <p className="empty">Loading…</p>
        ) : !strategies || strategies.length === 0 ? (
          <p className="empty">No strategies yet. Create one to get started.</p>
        ) : (
          <div className="list">
            {strategies.map((s: Strategy) => (
              <div
                key={s.id}
                className={`item strategy-item${selectedId === s.id ? " selected" : ""}${
                  s.enabled ? "" : " is-off"
                }`}
                onClick={() => onSelect(s.id)}
              >
                <div className="head">
                  <span className="symbol">{s.name}</span>
                  <Toggle
                    on={s.enabled}
                    disabled={toggle.isPending}
                    onChange={(next) => toggle.mutate({ id: s.id, enabled: next })}
                    title={s.enabled ? "Strategy is active" : "Strategy is off"}
                  />
                </div>
                {s.description && <div className="body">{s.description}</div>}
                <div className="rule-actions">
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
    </div>
  );
}
