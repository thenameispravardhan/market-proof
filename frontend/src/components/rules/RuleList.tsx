// RuleList — shows rules for a strategy with drag-and-drop reorder.
import { useRules, useDeleteRule, useReorderRules } from "../../hooks/useApi";
import type { SignalRule } from "../../types";

interface Props {
  strategyId: number | null;
  selectedId: number | null;
  onSelect: (id: number) => void;
}

export function RuleList({ strategyId, selectedId, onSelect }: Props) {
  const { data: rules, isLoading } = useRules(strategyId);
  const deleteRule = useDeleteRule();
  const reorder = useReorderRules();

  const handleDelete = async (id: number) => {
    if (!confirm("Delete this rule?")) return;
    await deleteRule.mutateAsync(id);
  };

  const moveUp = async (idx: number) => {
    if (!rules || !strategyId || idx === 0) return;
    const ids = rules.map((r) => r.id);
    [ids[idx - 1], ids[idx]] = [ids[idx], ids[idx - 1]];
    await reorder.mutateAsync({ strategy_id: strategyId, ordered_ids: ids });
  };

  const moveDown = async (idx: number) => {
    if (!rules || !strategyId || idx >= rules.length - 1) return;
    const ids = rules.map((r) => r.id);
    [ids[idx], ids[idx + 1]] = [ids[idx + 1], ids[idx]];
    await reorder.mutateAsync({ strategy_id: strategyId, ordered_ids: ids });
  };

  return (
    <div className="widget" data-testid="rule-list">
      <h3>Rules {strategyId ? "" : "(all strategies)"}</h3>
      {isLoading ? (
        <p className="empty">Loading…</p>
      ) : !rules || rules.length === 0 ? (
        <p className="empty">
          No rules yet.{" "}
          {strategyId
            ? "Create one in the editor."
            : "Select a strategy first."}
        </p>
      ) : (
        <div className="list">
          {rules.map((rule: SignalRule, idx) => (
            <div
              key={rule.id}
              className={`item${selectedId === rule.id ? " selected" : ""}`}
              onClick={() => onSelect(rule.id)}
            >
              <div className="head">
                <span className="symbol">#{rule.priority}</span>
                <span className={`badge ${rule.enabled ? "info" : "warn"}`}>
                  {rule.action}
                </span>
                {!rule.enabled && (
                  <span className="badge warn" style={{ marginLeft: 4 }}>disabled</span>
                )}
              </div>
              <div className="body">{rule.name}</div>
              <div className="body reason" style={{ display: "flex", gap: 8, marginTop: 6 }}>
                {strategyId && (
                  <>
                    <button
                      className="btn-sm"
                      onClick={(e) => { e.stopPropagation(); moveUp(idx); }}
                      disabled={idx === 0}
                      title="Move up"
                    >↑</button>
                    <button
                      className="btn-sm"
                      onClick={(e) => { e.stopPropagation(); moveDown(idx); }}
                      disabled={idx === rules.length - 1}
                      title="Move down"
                    >↓</button>
                  </>
                )}
                <button
                  className="btn-sm danger"
                  onClick={(e) => { e.stopPropagation(); handleDelete(rule.id); }}
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
