// Edge Memory — the bot's self-learning conviction engine, surfaced.
//
// Shows the bot's OWN track record (from logged outcomes) and lets the
// operator turn it into a risk gate: block signals whose historical
// cohort loses money. The gate is FAIL-OPEN — it never blocks on thin
// history — and OFF by default. This is the edge that compounds: no HFT
// firm has your small/mid-cap news-reaction data, so no one can copy it.

import { useEffect, useState } from "react";
import {
  useEdgeBook,
  useGlobalSettings,
  useUpdateSettings,
  type EdgeStats,
} from "../../hooks/useApi";
import { Toggle } from "../common/Toggle";
import type { GlobalSettings as GS } from "../../types";

function pct(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`;
}

function TrackRecord({ label, stats }: { label: string; stats: EdgeStats | null }) {
  if (!stats) {
    return (
      <div className="field">
        <label>{label}</label>
        <span className="field-hint mono">no completed outcomes yet</span>
      </div>
    );
  }
  const good = stats.expectancy >= 0;
  return (
    <div className="field">
      <label>{label}</label>
      <span className={`mono ${good ? "pnl-pos" : "pnl-neg"}`}>
        expectancy {pct(stats.expectancy)} @30m · win {Math.round(stats.win_rate * 100)}% · n={stats.n}
      </span>
    </div>
  );
}

export function EdgeMemory() {
  const { data: settings, isLoading } = useGlobalSettings();
  const { data: edge } = useEdgeBook();
  const update = useUpdateSettings();
  const [values, setValues] = useState<Partial<GS>>({});
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (settings?.global) setValues({ ...settings.global });
  }, [settings]);

  const gateOn = Boolean(values.EDGE_GATE_ENABLED ?? false);

  const handleSave = async () => {
    setError(null);
    const slice: Partial<GS> = {
      EDGE_GATE_ENABLED: gateOn,
      EDGE_GATE_MIN_SAMPLES: Number(values.EDGE_GATE_MIN_SAMPLES ?? 30),
      EDGE_GATE_MIN_EXPECTANCY_PCT: Number(values.EDGE_GATE_MIN_EXPECTANCY_PCT ?? 0),
    };
    try {
      await update.mutateAsync({ global: slice as GS });
      setSaved(true);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="widget" data-testid="edge-memory">
      <h3>Edge Memory — learn from your own trades</h3>
      <p className="empty">
        The bot remembers how signals like each new one actually played out.
        No HFT firm has this data in your stocks — it&apos;s the edge that
        compounds with every trading day.
      </p>
      {isLoading ? (
        <p className="empty">Loading…</p>
      ) : (
        <>
          <TrackRecord label="BUY track record" stats={edge?.book.BUY ?? null} />
          <TrackRecord label="SELL track record" stats={edge?.book.SELL ?? null} />

          <div className="field" style={{ marginTop: 8 }}>
            <label>Use track record as a trade filter</label>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <Toggle
                on={gateOn}
                data-testid="edge-gate-toggle"
                onChange={(next) => {
                  setValues((prev) => ({ ...prev, EDGE_GATE_ENABLED: next }));
                  setSaved(false);
                }}
              />
              <span className="field-hint" style={{ marginTop: 0 }}>
                {gateOn
                  ? "ON — a signal is blocked when its history (this stock, else this action) shows a losing 30-min average over enough samples. Fails open: thin history never blocks."
                  : "OFF (default) — the track record is shown but never blocks a trade. Turn on once you trust the numbers."}
              </span>
            </div>
          </div>

          <div className="field">
            <label htmlFor="edge-min-samples">Minimum samples before it can block</label>
            <input
              id="edge-min-samples"
              type="number"
              min={5}
              max={1000}
              step={5}
              value={values.EDGE_GATE_MIN_SAMPLES ?? 30}
              onChange={(e) => {
                setValues((prev) => ({ ...prev, EDGE_GATE_MIN_SAMPLES: Number(e.target.value) }));
                setSaved(false);
              }}
            />
            <span className="field-hint">Fewer than this = not enough evidence, trade allowed.</span>
          </div>

          <div className="field">
            <label htmlFor="edge-min-exp">Block below this 30-min expectancy (%)</label>
            <input
              id="edge-min-exp"
              type="number"
              min={-50}
              max={50}
              step={0.1}
              value={values.EDGE_GATE_MIN_EXPECTANCY_PCT ?? 0}
              onChange={(e) => {
                setValues((prev) => ({ ...prev, EDGE_GATE_MIN_EXPECTANCY_PCT: Number(e.target.value) }));
                setSaved(false);
              }}
            />
            <span className="field-hint">0 = block only proven money-losers. Raise it to demand a positive edge.</span>
          </div>

          {error && <p className="pnl-neg">{error}</p>}
          {saved && <p className="pnl-pos">✓ Saved — applies to the next signal</p>}
          <button className="primary" onClick={handleSave} disabled={update.isPending}>
            {update.isPending ? "Saving…" : "Save"}
          </button>
        </>
      )}
    </div>
  );
}
