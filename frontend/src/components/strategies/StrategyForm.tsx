// StrategyForm — create or edit a strategy.
import { useEffect, useState } from "react";
import { useStrategies, useCreateStrategy, useUpdateStrategy } from "../../hooks/useApi";

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
    let config: Record<string, unknown> = {};
    try {
      config = JSON.parse(configJson);
    } catch {
      setError("Config must be valid JSON");
      return;
    }
    const body = { name, description: description || undefined, enabled, config };
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

  return (
    <div className="widget" data-testid="strategy-form">
      <h3>{strategy ? `Edit — ${strategy.name}` : "New Strategy"}</h3>
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
          placeholder="Optional description"
        />
      </div>
      <div className="field">
        <label>
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
          />{" "}
          Enabled
        </label>
      </div>
      <div className="field">
        <label htmlFor="strat-config">Risk config overrides (JSON)</label>
        <textarea
          id="strat-config"
          rows={5}
          className="mono"
          value={configJson}
          onChange={(e) => setConfigJson(e.target.value)}
        />
        <div style={{ color: "var(--text-dim)", fontSize: 11 }}>
          Keys: max_capital_risk_pct, daily_max_loss_pct, max_concurrent_positions, etc.
        </div>
      </div>
      {error && <p className="pnl-neg">{error}</p>}
      <button
        className="primary"
        onClick={handleSave}
        disabled={create.isPending || update.isPending || !name}
        data-testid="save-strategy"
      >
        {create.isPending || update.isPending ? "Saving…" : strategy ? "Update" : "Create"}
      </button>
    </div>
  );
}
