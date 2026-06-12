// Strategies page: list + create/edit strategies.
import { useState } from "react";
import { StrategyList } from "../components/strategies/StrategyList";
import { StrategyForm } from "../components/strategies/StrategyForm";

export default function Strategies() {
  const [selectedId, setSelectedId] = useState<number | null>(null);

  return (
    <div>
      <h1 className="page-title">Strategies</h1>
      <div className="layout-2">
        <StrategyList
          selectedId={selectedId}
          onSelect={setSelectedId}
          onNew={() => setSelectedId(null)}
        />
        <StrategyForm
          strategyId={selectedId}
          onSaved={() => setSelectedId(null)}
        />
      </div>
    </div>
  );
}
