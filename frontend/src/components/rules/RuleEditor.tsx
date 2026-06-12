// RuleEditor — visual condition builder + action selector.
import { useEffect, useState } from "react";
import { useRules, useCreateRule, useUpdateRule } from "../../hooks/useApi";
import type { Action, SignalCondition, SignalConditions } from "../../types";

const FIELDS = [
  "event_type", "sentiment", "sentiment_score", "confidence",
  "recommendation", "deal_value_inr_crore", "stake_change_pct",
  "dividend_per_share", "buyback_value_inr_crore",
];
const OPS = ["==", "!=", "in", "not_in", ">=", "<=", ">", "<"];
const ACTIONS = ["BUY", "SELL", "HOLD", "BLOCK"];

interface Props {
  ruleId: number | null;
  strategyId: number | null;
  onSaved?: () => void;
}

function newCond(): SignalCondition {
  return { field: "sentiment_score", op: ">=", value: 50 };
}

export function RuleEditor({ ruleId, strategyId, onSaved }: Props) {
  const { data: rules } = useRules(strategyId ?? undefined);
  const create = useCreateRule();
  const update = useUpdateRule(ruleId);

  const rule = rules?.find((r) => r.id === ruleId) ?? null;

  const [name, setName] = useState("");
  const [priority, setPriority] = useState(100);
  const [action, setAction] = useState<Action>("BUY");
  const [enabled, setEnabled] = useState(true);
  const [allOf, setAllOf] = useState<SignalCondition[]>([newCond()]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (rule) {
      setName(rule.name);
      setPriority(rule.priority);
      setAction(rule.action);
      setEnabled(rule.enabled);
      setAllOf((rule.conditions.all_of ?? []).length > 0
        ? (rule.conditions.all_of as SignalCondition[])
        : [newCond()]
      );
    } else {
      setName("");
      setPriority(100);
      setAction("BUY");
      setEnabled(true);
      setAllOf([newCond()]);
    }
    setError(null);
  }, [rule]);

  const updateCond = (idx: number, patch: Partial<SignalCondition>) => {
    setAllOf((prev) => prev.map((c, i) => i === idx ? { ...c, ...patch } : c));
  };

  const handleSave = async () => {
    if (!strategyId && !rule) {
      setError("Select a strategy first.");
      return;
    }
    const conditions: SignalConditions = { all_of: allOf };
    const body = {
      strategy_id: rule?.strategy_id ?? strategyId!,
      name,
      priority,
      action,
      enabled,
      conditions,
    };
    try {
      if (rule) {
        await update.mutateAsync(body);
      } else {
        await create.mutateAsync(body);
      }
      onSaved?.();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="widget" data-testid="rule-editor">
      <h3>{rule ? `Edit Rule #${rule.id}` : "New Rule"}</h3>
      <div className="field">
        <label htmlFor="rule-name">Name</label>
        <input id="rule-name" value={name} onChange={(e) => setName(e.target.value)} />
      </div>
      <div className="field-row">
        <div className="field">
          <label htmlFor="rule-priority">Priority (lower = first)</label>
          <input
            id="rule-priority"
            type="number"
            value={priority}
            onChange={(e) => setPriority(Number(e.target.value))}
          />
        </div>
        <div className="field">
          <label htmlFor="rule-action">Action</label>
          <select id="rule-action" value={action} onChange={(e) => setAction(e.target.value as Action)}>
            {ACTIONS.map((a) => <option key={a}>{a}</option>)}
          </select>
        </div>
        <div className="field" style={{ justifyContent: "flex-end", paddingTop: 20 }}>
          <label>
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
            />{" "}
            Enabled
          </label>
        </div>
      </div>

      <div className="field">
        <label>Conditions (ALL must match)</label>
        {allOf.map((cond, idx) => (
          <div key={idx} className="field-row" style={{ marginBottom: 8 }}>
            <select
              value={cond.field}
              onChange={(e) => updateCond(idx, { field: e.target.value })}
            >
              {FIELDS.map((f) => <option key={f}>{f}</option>)}
            </select>
            <select
              value={cond.op}
              onChange={(e) => updateCond(idx, { op: e.target.value })}
            >
              {OPS.map((o) => <option key={o}>{o}</option>)}
            </select>
            <input
              value={String(cond.value)}
              onChange={(e) => {
                const v = e.target.value;
                updateCond(idx, { value: isNaN(Number(v)) ? v : Number(v) });
              }}
              placeholder="value"
              style={{ width: 120 }}
            />
            <button
              className="btn-sm danger"
              onClick={() => setAllOf((prev) => prev.filter((_, i) => i !== idx))}
              disabled={allOf.length <= 1}
            >×</button>
          </div>
        ))}
        <button className="btn-sm" onClick={() => setAllOf((prev) => [...prev, newCond()])}>
          + Add condition
        </button>
      </div>

      <div className="field">
        <label>JSON Preview</label>
        <pre className="mono" style={{ fontSize: 11, background: "#0d1117", padding: 8, borderRadius: 4 }}>
          {JSON.stringify({ all_of: allOf }, null, 2)}
        </pre>
      </div>

      {error && <p className="pnl-neg">{error}</p>}

      <button
        className="primary"
        onClick={handleSave}
        disabled={create.isPending || update.isPending}
        data-testid="save-rule"
      >
        {create.isPending || update.isPending ? "Saving…" : rule ? "Update Rule" : "Create Rule"}
      </button>
    </div>
  );
}
