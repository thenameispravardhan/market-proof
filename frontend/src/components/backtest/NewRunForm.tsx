// NewRunForm — create a new backtest run.
import { useState } from "react";
import { useCreateBacktestRun, useStrategies, useBrokerAccounts } from "../../hooks/useApi";

interface Props {
  onCreated?: (id: number) => void;
}

function todayMinus(n: number): string {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return d.toISOString().slice(0, 10);
}

export function NewRunForm({ onCreated }: Props) {
  const { data: strategies } = useStrategies();
  const { data: accounts } = useBrokerAccounts();
  const create = useCreateBacktestRun();

  const [name, setName] = useState("");
  const [startDate, setStartDate] = useState(todayMinus(30));
  const [endDate, setEndDate] = useState(todayMinus(1));
  const [capital, setCapital] = useState("100000");
  const [strategyId, setStrategyId] = useState<string>("");
  const [accountId, setAccountId] = useState<string>("");
  const [error, setError] = useState<string | null>(null);

  const handleCreate = async () => {
    setError(null);
    try {
      const run = await create.mutateAsync({
        name: name || undefined,
        strategy_id: strategyId ? Number(strategyId) : undefined,
        broker_account_id: accountId ? Number(accountId) : undefined,
        start_date: startDate,
        end_date: endDate,
        initial_capital: Number(capital),
      });
      onCreated?.(run.id);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="widget" data-testid="new-run-form">
      <h3>New Backtest Run</h3>
      <div className="field">
        <label htmlFor="bt-name">Label (optional)</label>
        <input id="bt-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Q1 2026 test" />
      </div>
      <div className="field-row">
        <div className="field">
          <label htmlFor="bt-start">Start date</label>
          <input id="bt-start" type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="bt-end">End date</label>
          <input id="bt-end" type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
        </div>
      </div>
      <div className="field">
        <label htmlFor="bt-capital">Initial capital (₹)</label>
        <input id="bt-capital" type="number" value={capital} onChange={(e) => setCapital(e.target.value)} />
      </div>
      <div className="field">
        <label htmlFor="bt-strategy">Strategy (optional)</label>
        <select id="bt-strategy" value={strategyId} onChange={(e) => setStrategyId(e.target.value)}>
          <option value="">— default —</option>
          {(strategies ?? []).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
        </select>
      </div>
      <div className="field">
        <label htmlFor="bt-account">Broker account (optional)</label>
        <select id="bt-account" value={accountId} onChange={(e) => setAccountId(e.target.value)}>
          <option value="">— default —</option>
          {(accounts ?? []).map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
        </select>
      </div>
      {error && <p className="pnl-neg">{error}</p>}
      <button
        className="primary"
        onClick={handleCreate}
        disabled={create.isPending}
        data-testid="create-run-btn"
      >
        {create.isPending ? "Creating…" : "Start Backtest"}
      </button>
    </div>
  );
}
