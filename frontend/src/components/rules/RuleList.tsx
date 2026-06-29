// RuleList — rules for a strategy, each with an on/off switch, a plain
// summary of its conditions, and reorder / delete controls.
import { useRules, useDeleteRule, useReorderRules, useToggleRule } from "../../hooks/useApi";
import { Toggle } from "../common/Toggle";
import type { SignalCondition, SignalRule } from "../../types";

interface Props {
  strategyId: number | null;
  selectedId: number | null;
  onSelect: (id: number) => void;
}

const FIELD_LABEL: Record<string, string> = {
  sentiment: "sentiment",
  sentiment_score: "score",
  confidence: "confidence",
  recommendation: "AI rec",
};
const OP_SYM: Record<string, string> = {
  "==": "is",
  "!=": "≠",
  ">=": "≥",
  "<=": "≤",
  ">": ">",
  "<": "<",
  in: "in",
  not_in: "not in",
};

function condText(c: SignalCondition): string {
  const f = FIELD_LABEL[c.field] ?? c.field;
  const op = OP_SYM[c.op] ?? c.op;
  const v = Array.isArray(c.value) ? c.value.join("/") : String(c.value);
  return `${f} ${op} ${v}`;
}

function summarize(rule: SignalRule): string {
  const all = rule.conditions?.all_of ?? [];
  if (all.length === 0) return "no conditions";
  return all.map(condText).join("  ·  ");
}

export function RuleList({ strategyId, selectedId, onSelect }: Props) {
  const { data: rules, isLoading } = useRules(strategyId);
  const deleteRule = useDeleteRule();
  const reorder = useReorderRules();
  const toggle = useToggleRule();

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
      <div className="widget-header">
        <h3>
          Rules
          <span className="meta">
            {strategyId ? `${rules?.length ?? 0} total` : "pick a strategy"}
          </span>
        </h3>
      </div>
      <div className="widget-body" style={{ padding: 0 }}>
        {isLoading ? (
          <p className="empty">Loading…</p>
        ) : !rules || rules.length === 0 ? (
          <p className="empty">
            No rules yet.{" "}
            {strategyId
              ? "Create one in the editor →"
              : "Select a strategy first."}
          </p>
        ) : (
          <div className="list">
            {rules.map((rule: SignalRule, idx) => (
              <div
                key={rule.id}
                className={`item rule-item${selectedId === rule.id ? " selected" : ""}${
                  rule.enabled ? "" : " is-off"
                }`}
                onClick={() => onSelect(rule.id)}
              >
                <div className="head">
                  <span className="rule-order" title="Order checked (lower runs first)">
                    #{rule.priority}
                  </span>
                  <span className={`badge ${rule.action.toLowerCase()}`}>
                    {rule.action}
                  </span>
                  <span className="rule-name">{rule.name}</span>
                  <Toggle
                    on={rule.enabled}
                    size="sm"
                    disabled={toggle.isPending}
                    onChange={(next) => toggle.mutate({ id: rule.id, enabled: next })}
                    title={rule.enabled ? "Rule is active" : "Rule is paused"}
                  />
                </div>
                <div className="rule-summary">{summarize(rule)}</div>
                <div className="rule-actions">
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
    </div>
  );
}
