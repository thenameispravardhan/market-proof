// RuleEditor — visual condition builder + action selector.
//
// Only four analysis fields are offered, each with a typed input so a
// condition can never carry the wrong datatype (the old free-text box let
// you compare a number field to the string "BUY", which the engine then
// rejected at run time):
//   • sentiment        → enum  (positive / neutral / negative)
//   • sentiment_score  → number (−100 … 100)
//   • confidence       → number (0 … 1)
//   • recommendation   → enum  (BUY / SELL / HOLD)
import { useEffect, useState } from "react";
import { useRules, useCreateRule, useUpdateRule } from "../../hooks/useApi";
import { Toggle } from "../common/Toggle";
import type { Action, SignalCondition, SignalConditions } from "../../types";

type FieldKind = "enum" | "number";

interface FieldDef {
  value: string;
  label: string;
  kind: FieldKind;
  options?: { value: string; label: string }[]; // enum only
  min?: number; // number only
  max?: number;
  step?: number;
  fallback: string | number; // default value when this field is picked
  hint: string;
}

// The only four conditions we support, with the input each one needs.
const FIELDS: FieldDef[] = [
  {
    value: "sentiment",
    label: "Sentiment",
    kind: "enum",
    options: [
      { value: "positive", label: "Positive" },
      { value: "neutral", label: "Neutral" },
      { value: "negative", label: "Negative" },
    ],
    fallback: "positive",
    hint: "Overall tone the model assigned to the news.",
  },
  {
    value: "sentiment_score",
    label: "Sentiment score",
    kind: "number",
    min: -100,
    max: 100,
    step: 1,
    fallback: 50,
    hint: "−100 (most negative) … +100 (most positive).",
  },
  {
    value: "confidence",
    label: "Confidence",
    kind: "number",
    min: 0,
    max: 1,
    step: 0.05,
    fallback: 0.7,
    hint: "How sure the model is, from 0 to 1.",
  },
  {
    value: "recommendation",
    label: "AI recommendation",
    kind: "enum",
    options: [
      { value: "BUY", label: "BUY" },
      { value: "SELL", label: "SELL" },
      { value: "HOLD", label: "HOLD" },
    ],
    fallback: "BUY",
    hint: "The model's own call on the stock.",
  },
];

const FIELD_BY_VALUE = Object.fromEntries(FIELDS.map((f) => [f.value, f]));

// Operators differ by datatype: enums only support is / is-not; numbers
// support the ordering comparisons too.
const ENUM_OPS = [
  { value: "==", label: "is" },
  { value: "!=", label: "is not" },
];
const NUMBER_OPS = [
  { value: ">=", label: "is at least (≥)" },
  { value: "<=", label: "is at most (≤)" },
  { value: ">", label: "is more than (>)" },
  { value: "<", label: "is less than (<)" },
  { value: "==", label: "equals (=)" },
  { value: "!=", label: "is not (≠)" },
];

const opsFor = (kind: FieldKind) => (kind === "enum" ? ENUM_OPS : NUMBER_OPS);

const ACTIONS: Action[] = ["BUY", "SELL", "HOLD", "BLOCK"];

interface Props {
  ruleId: number | null;
  strategyId: number | null;
  onSaved?: () => void;
}

function newCond(): SignalCondition {
  return { field: "sentiment_score", op: ">=", value: 50 };
}

// Coerce a stored/loaded condition onto a supported field+op+value so
// legacy rows (or hand-edited JSON) never render a blank dropdown.
function normalizeCond(c: SignalCondition): SignalCondition {
  const def = FIELD_BY_VALUE[c.field];
  if (!def) return newCond();
  const ops = opsFor(def.kind).map((o) => o.value);
  const op = ops.includes(c.op) ? c.op : ops[0];
  let value = c.value;
  if (def.kind === "enum") {
    const allowed = def.options!.map((o) => o.value);
    value = allowed.includes(String(value)) ? String(value) : def.fallback;
  } else {
    value = typeof value === "number" && !Number.isNaN(value) ? value : def.fallback;
  }
  return { field: c.field, op, value };
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
      const conds = (rule.conditions.all_of ?? []) as SignalCondition[];
      setAllOf(conds.length > 0 ? conds.map(normalizeCond) : [newCond()]);
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
    setAllOf((prev) => prev.map((c, i) => (i === idx ? { ...c, ...patch } : c)));
  };

  // When the field changes, reset the op + value to ones valid for the
  // new field's datatype. This is what keeps datatypes correct.
  const changeField = (idx: number, nextField: string) => {
    const def = FIELD_BY_VALUE[nextField];
    if (!def) return;
    updateCond(idx, {
      field: nextField,
      op: opsFor(def.kind)[0].value,
      value: def.fallback,
    });
  };

  const changeValue = (idx: number, def: FieldDef, raw: string) => {
    if (def.kind === "number") {
      // Keep an empty box as "" so the user can clear and retype; save
      // validation rejects empties/NaNs before they reach the API.
      updateCond(idx, { value: raw === "" ? "" : Number(raw) });
    } else {
      updateCond(idx, { value: raw });
    }
  };

  const validate = (): string | null => {
    if (!name.trim()) return "Give the rule a name.";
    if (!strategyId && !rule) return "Select a strategy first.";
    for (const c of allOf) {
      const def = FIELD_BY_VALUE[c.field];
      if (!def) return `Unknown condition field: ${c.field}`;
      if (def.kind === "number") {
        const n = Number(c.value);
        if (c.value === "" || Number.isNaN(n)) {
          return `Enter a number for "${def.label}".`;
        }
        if (def.min !== undefined && n < def.min)
          return `"${def.label}" must be ≥ ${def.min}.`;
        if (def.max !== undefined && n > def.max)
          return `"${def.label}" must be ≤ ${def.max}.`;
      } else {
        const allowed = def.options!.map((o) => o.value);
        if (!allowed.includes(String(c.value)))
          return `Pick a valid value for "${def.label}".`;
      }
    }
    return null;
  };

  const handleSave = async () => {
    const v = validate();
    if (v) {
      setError(v);
      return;
    }
    // Final type-coercion so numbers go out as numbers, enums as strings.
    const cleaned: SignalCondition[] = allOf.map((c) => {
      const def = FIELD_BY_VALUE[c.field];
      return {
        field: c.field,
        op: c.op,
        value: def?.kind === "number" ? Number(c.value) : String(c.value),
      };
    });
    const conditions: SignalConditions = { all_of: cleaned };
    const body = {
      strategy_id: rule?.strategy_id ?? strategyId!,
      name: name.trim(),
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

  const saving = create.isPending || update.isPending;

  return (
    <div className="widget" data-testid="rule-editor">
      <div className="widget-header">
        <h3>
          {rule ? `Edit rule #${rule.id}` : "New rule"}
          <Toggle on={enabled} onChange={setEnabled} size="sm" label="Active" />
        </h3>
      </div>
      <div className="widget-body rule-editor-body">
        <div className="field">
          <label htmlFor="rule-name">Rule name</label>
          <input
            id="rule-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. High-conviction buy"
          />
        </div>

        <div className="field-row">
          <div className="field">
            <label htmlFor="rule-action">Then do</label>
            <select
              id="rule-action"
              value={action}
              onChange={(e) => setAction(e.target.value as Action)}
            >
              {ACTIONS.map((a) => (
                <option key={a}>{a}</option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="rule-priority">Order checked</label>
            <input
              id="rule-priority"
              type="number"
              value={priority}
              onChange={(e) => setPriority(Number(e.target.value))}
              title="Lower numbers are checked first; the first matching rule wins"
            />
            <div className="field-hint">Lower runs first.</div>
          </div>
        </div>

        <div className="field">
          <label>When ALL of these match</label>
          <div className="cond-list">
            {allOf.map((cond, idx) => {
              const def = FIELD_BY_VALUE[cond.field] ?? FIELDS[0];
              return (
                <div key={idx} className="cond-card">
                  <div className="cond-grid">
                    <select
                      aria-label="Field"
                      value={cond.field}
                      onChange={(e) => changeField(idx, e.target.value)}
                    >
                      {FIELDS.map((f) => (
                        <option key={f.value} value={f.value}>
                          {f.label}
                        </option>
                      ))}
                    </select>
                    <select
                      aria-label="Operator"
                      value={cond.op}
                      onChange={(e) => updateCond(idx, { op: e.target.value })}
                    >
                      {opsFor(def.kind).map((o) => (
                        <option key={o.value} value={o.value}>
                          {o.label}
                        </option>
                      ))}
                    </select>
                    {def.kind === "enum" ? (
                      <select
                        aria-label="Value"
                        value={String(cond.value)}
                        onChange={(e) => changeValue(idx, def, e.target.value)}
                      >
                        {def.options!.map((o) => (
                          <option key={o.value} value={o.value}>
                            {o.label}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <input
                        aria-label="Value"
                        type="number"
                        min={def.min}
                        max={def.max}
                        step={def.step}
                        value={cond.value === "" ? "" : String(cond.value)}
                        onChange={(e) => changeValue(idx, def, e.target.value)}
                        placeholder="value"
                      />
                    )}
                    <button
                      className="btn-sm danger cond-remove"
                      onClick={() =>
                        setAllOf((prev) => prev.filter((_, i) => i !== idx))
                      }
                      disabled={allOf.length <= 1}
                      title="Remove condition"
                    >
                      ×
                    </button>
                  </div>
                  <div className="field-hint">{def.hint}</div>
                </div>
              );
            })}
          </div>
          <button
            className="btn-sm"
            onClick={() => setAllOf((prev) => [...prev, newCond()])}
          >
            + Add condition
          </button>
        </div>

        {error && <p className="pnl-neg rule-error">{error}</p>}

        <button
          className="primary save-rule-btn"
          onClick={handleSave}
          disabled={saving}
          data-testid="save-rule"
        >
          {saving ? "Saving…" : rule ? "Update rule" : "Create rule"}
        </button>
      </div>
    </div>
  );
}
