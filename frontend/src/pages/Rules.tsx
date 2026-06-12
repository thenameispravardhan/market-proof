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

  return (
    <div>
      <h1 className="page-title">Signal Rules</h1>
      <div style={{ marginBottom: 16, display: "flex", gap: 12, alignItems: "center" }}>
        <label htmlFor="strategy-filter">Strategy:</label>
        <select
          id="strategy-filter"
          value={strategyId ?? ""}
          onChange={(e) => {
            const v = e.target.value;
            setStrategyId(v ? Number(v) : null);
            setSelectedId(null);
          }}
        >
          <option value="">All strategies</option>
          {(strategies ?? []).map((s) => (
            <option key={s.id} value={s.id}>{s.name}</option>
          ))}
        </select>
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
