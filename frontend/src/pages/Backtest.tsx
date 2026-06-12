// Backtest page: kick off runs, view results, equity curve.
import { useState } from "react";
import { RunList } from "../components/backtest/RunList";
import { NewRunForm } from "../components/backtest/NewRunForm";
import { RunDetail } from "../components/backtest/RunDetail";

export default function Backtest() {
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [showNew, setShowNew] = useState(false);

  return (
    <div>
      <h1 className="page-title">Backtest</h1>
      <div className="layout-2">
        <div>
          <RunList
            selectedId={selectedId}
            onSelect={(id) => { setSelectedId(id); setShowNew(false); }}
          />
          <div style={{ marginTop: 16 }}>
            <button
              className="primary"
              onClick={() => { setShowNew(true); setSelectedId(null); }}
            >
              + New Run
            </button>
          </div>
        </div>
        <div>
          {showNew ? (
            <NewRunForm onCreated={(id) => { setSelectedId(id); setShowNew(false); }} />
          ) : (
            <RunDetail runId={selectedId} />
          )}
        </div>
      </div>
    </div>
  );
}
