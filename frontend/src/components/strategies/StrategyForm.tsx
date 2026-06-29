// StrategyForm — create or edit a strategy.
import { useEffect, useState } from "react";
import { useStrategies, useCreateStrategy, useUpdateStrategy } from "../../hooks/useApi";
import { Toggle } from "../common/Toggle";

interface Props {
  strategyId: number | null;
  onSaved?: () => void;
}

export function StrategyForm({ strategyId, onSaved }: Props) {
  const { data: strategies } = useStrategies();
  const strategy = strategies?.find((s) => s.id === strategyId) ?? null;
  const create = useCreateStrategy();
  const update = useUpdateStrategy(strategyId);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [configJson, setConfigJson] = useState("{}");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (strategy) {
      setName(strategy.name);
      setDescription(strategy.description ?? "");
      setEnabled(strategy.enabled);
      setConfigJson(JSON.stringify(strategy.config ?? {}, null, 2));
    } else {
      setName("");
      setDescription("");
      setEnabled(true);
      setConfigJson("{}");
    }
    setError(null);
  }, [strategy]);

  const handleSave = async () => {
    setError(null);
    if (!name.trim()) {
      setError("Give the strategy a name.");
      return;
    }
    let config: Record<string, unknown> = {};
    try {
      config = JSON.parse(configJson);
    } catch {
      setError("Risk config must be valid JSON.");
      return;
    }
    const body = { name: name.trim(), description: description || undefined, enabled, config };
    try {
      if (strategy) {
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
    <div className="widget" data-testid="strategy-form">
      <div className="widget-header">
        <h3>
          {strategy ? `Edit — ${strategy.name}` : "New strategy"}
          <Toggle on={enabled} onChange={setEnabled} size="sm" label="Active" />
        </h3>
      </div>
      <div className="widget-body">
        <div className="field">
          <label htmlFor="strat-name">Name</label>
          <input
            id="strat-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Aggressive Earnings"
          />
        </div>
        <div className="field">
          <label htmlFor="strat-desc">Description</label>
          <textarea
            id="strat-desc"
            rows={3}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Optional — what this strategy is for"
          />
        </div>

        <details className="advanced-block">
          <summary>Advanced — risk config overrides (JSON)</summary>
          <div className="field" style={{ marginTop: 8 }}>
            <textarea
              id="strat-config"
              rows={5}
              className="mono"
              value={configJson}
              onChange={(e) => setConfigJson(e.target.value)}
            />
            <div className="field-hint">
              Optional. Keys: max_capital_risk_pct, daily_max_loss_pct,
              max_concurrent_positions, broker_account_id, …
            </div>
          </div>
        </details>

        {error && <p className="pnl-neg rule-error">{error}</p>}

        <button
          className="primary save-rule-btn"
          onClick={handleSave}
          disabled={saving || !name.trim()}
          data-testid="save-strategy"
        >
          {saving ? "Saving…" : strategy ? "Update strategy" : "Create strategy"}
        </button>
      </div>
    </div>
  );
}
