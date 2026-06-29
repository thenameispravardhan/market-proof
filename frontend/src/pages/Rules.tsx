// Rules page: visual rule builder with drag-and-drop reorder and dry-run.
import { useState } from "react";
import { RuleList } from "../components/rules/RuleList";
import { RuleEditor } from "../components/rules/RuleEditor";
import { RuleDryRun } from "../components/rules/RuleDryRun";
import { useStrategies } from "../hooks/useApi";

export default function Rules() {
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const { data: strategies } = useStrategies();
  const [strategyId, setStrategyId] = useState<number | null>(null);

  const hasStrategies = (strategies ?? []).length > 0;

  return (
    <div>
      <h1 className="page-title">Signal Rules</h1>
      <div className="toolbar">
        <span className="label">Strategy</span>
        <select
          id="strategy-filter"
          value={strategyId ?? ""}
          onChange={(e) => {
            const v = e.target.value;
            setStrategyId(v ? Number(v) : null);
            setSelectedId(null);
          }}
          style={{ width: "auto", minWidth: 200 }}
        >
          <option value="">All strategies</option>
          {(strategies ?? []).map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}{s.enabled ? "" : " (off)"}
            </option>
          ))}
        </select>
        <span className="spacer" />
        <span className="label">
          {strategyId
            ? "Editing rules for this strategy"
            : hasStrategies
              ? "Pick a strategy to add rules"
              : "Create a strategy first"}
        </span>
      </div>
      <div className="layout-2">
        <RuleList
          strategyId={strategyId}
          selectedId={selectedId}
          onSelect={setSelectedId}
        />
        <div>
          <RuleEditor
            ruleId={selectedId}
            strategyId={strategyId}
            onSaved={() => setSelectedId(null)}
          />
          <div style={{ marginTop: 16 }}>
            <RuleDryRun strategyId={strategyId} />
          </div>
        </div>
      </div>
    </div>
  );
}
