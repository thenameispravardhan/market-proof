// Model page — the mover model's control room.
//
// The model is an offline-trained logistic regression exported to
// AIdataset/model/live_model.json and scored in pure Python at signal time
// (app/services/mover_model.py). Everything about it is operator-controlled
// from here, per the frontend-only-control invariant: which variant scores,
// whether it can veto, and at what threshold.
//
// The page is laid out as the decision an operator actually has to make:
//
//   1. Is the model loaded, and what was it trained on?
//   2. Which variant — the holdout AUC / lift table, side by side.
//   3. What would the current threshold have DONE — replayed over real
//      recorded outcomes, before the gate is armed.
//   4. Two independent switches: score-only, and score-can-block.
//
// Step 3 is the point of the page. The offline verdict (plan.txt, Phase 5)
// is that pooled accuracy is pinned by the base rate; the model's value is
// ranking, not filtering. So the UI refuses to make arming the gate a
// one-click act — the replay is right above the switch.

import { useEffect, useState } from "react";
import {
  useGlobalSettings,
  useModelPreview,
  useModelReload,
  useModelStatus,
  useUpdateSettings,
  type ModelVariant,
} from "../hooks/useApi";
import { Toggle } from "../components/common/Toggle";

const pct = (v: number | null | undefined, dp = 1) =>
  v === null || v === undefined || Number.isNaN(v) ? "—" : `${(v * 100).toFixed(dp)}%`;

function Metric({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div style={{ minWidth: 120 }} title={hint}>
      <div className="meta">{label}</div>
      <div className="mono" style={{ fontSize: 18 }}>{value}</div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 1 + 2: artifact state and the variant picker
// ---------------------------------------------------------------------------

function VariantTable({
  variants,
  selected,
  onSelect,
}: {
  variants: ModelVariant[];
  selected: string;
  onSelect: (key: string) => void;
}) {
  return (
    <table>
      <thead>
        <tr>
          <th>Use</th>
          <th>Variant</th>
          <th>Features</th>
          <th title="Rows in the held-out (most recent) quarter">Test rows</th>
          <th title="Share of test filings that actually moved &gt;1.5%">Base rate</th>
          <th title="Ranking quality. 0.5 = coin flip, 1.0 = perfect">ROC-AUC</th>
          <th title="Mover rate in the top-scoring 10% over the base rate. This is the number that matters.">
            Top-decile lift
          </th>
        </tr>
      </thead>
      <tbody>
        {variants.map((v) => (
          <tr
            key={v.key}
            style={v.key === selected ? { background: "var(--row-hover, rgba(127,127,127,.12))" } : undefined}
          >
            <td>
              <input
                type="radio"
                name="model-variant"
                checked={v.key === selected}
                onChange={() => onSelect(v.key)}
                aria-label={`Select ${v.label}`}
              />
            </td>
            <td className="mono">{v.label}</td>
            <td className="mono">{v.n_features}</td>
            <td className="mono">{v.metrics.n_test?.toLocaleString()}</td>
            <td className="mono">{pct(v.metrics.base_rate)}</td>
            <td className="mono">{v.metrics.roc_auc?.toFixed(4)}</td>
            <td className="mono">{v.metrics.top_decile_lift?.toFixed(2)}×</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// ---------------------------------------------------------------------------
// 3: what the threshold would have done, on real recorded outcomes
// ---------------------------------------------------------------------------

function Replay({
  variant,
  minProbability,
  minCoverage,
}: {
  variant: string;
  minProbability: number;
  minCoverage: number;
}) {
  const { data, isFetching } = useModelPreview({
    variant: variant || undefined,
    min_probability: minProbability,
    min_coverage: minCoverage,
    limit: 2000,
  });

  if (!data) {
    return <div className="empty">{isFetching ? "Replaying…" : "No replay available."}</div>;
  }
  if (data.n_scored === 0) {
    return (
      <div className="empty">
        No recorded outcomes to replay yet — the model can only be validated once
        signal_outcomes has completed 30-minute samples.
      </div>
    );
  }

  const base = data.base_mover_rate ?? 0;
  const blockedRate = data.blocked.mover_rate;
  // The whole question in one number: of what the gate would have thrown
  // away, how much of it actually moved, relative to everything.
  const wouldHaveCostMovers =
    blockedRate !== null && base > 0 ? blockedRate / base : null;

  return (
    <>
      <div style={{ display: "flex", gap: 24, flexWrap: "wrap", padding: "8px 12px" }}>
        <Metric label="Scored" value={data.n_scored.toLocaleString()} />
        <Metric label="Base mover rate" value={pct(base)} hint="|30m move| ≥ 1.5% across all replayed signals" />
        <Metric label="Would allow" value={`${data.allowed.n} · ${pct(data.allowed.mover_rate)}`} hint="count · mover rate among allowed" />
        <Metric label="Would block" value={`${data.blocked.n} · ${pct(blockedRate)}`} hint="count · mover rate among blocked" />
        <Metric label="Abstains" value={String(data.insufficient.n)} hint="score built on too little live data to gate on" />
      </div>

      <div className="meta" style={{ padding: "0 12px 10px" }}>
        {wouldHaveCostMovers === null ? (
          "Nothing would be blocked at this threshold."
        ) : wouldHaveCostMovers < 0.85 ? (
          <>
            The blocked set moved <b>{wouldHaveCostMovers.toFixed(2)}×</b> as often as
            average — the gate is discarding mostly non-movers, which is what you want.
          </>
        ) : (
          <>
            The blocked set moved <b>{wouldHaveCostMovers.toFixed(2)}×</b> as often as
            average. At this threshold the gate is not separating movers from
            non-movers — it is just cutting volume. Raise the threshold or leave the
            gate off.
          </>
        )}{" "}
        Replay uses the RAW 30-minute move (signal_outcomes has no index leg), so read
        it as the direction of the effect, not a reproduction of the offline AUC.
      </div>

      <table>
        <thead>
          <tr>
            <th>Symbol</th>
            <th>Action</th>
            <th>P(mover)</th>
            <th>Pctile</th>
            <th>Coverage</th>
            <th>Verdict</th>
            <th>Actual 30m</th>
          </tr>
        </thead>
        <tbody>
          {data.rows.map((r, i) => (
            <tr key={`${r.symbol}-${i}`}>
              <td className="mono">{r.symbol}</td>
              <td className="mono">{r.action}</td>
              <td className="mono">{pct(r.probability)}</td>
              <td className="mono">{r.percentile ?? "—"}</td>
              <td className="mono">{pct(r.coverage, 0)}</td>
              <td className="mono">{r.verdict}</td>
              <td className="mono" style={{ color: r.mover ? "var(--accent, inherit)" : undefined }}>
                {r.move_30m_pct?.toFixed(2)}%
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

// ---------------------------------------------------------------------------

export default function Model() {
  const { data: status, isLoading } = useModelStatus();
  const { data: settingsData } = useGlobalSettings();
  const update = useUpdateSettings();
  const reload = useModelReload();

  const saved = settingsData?.global;
  const [variant, setVariant] = useState("");
  const [minProb, setMinProb] = useState(0.15);
  const [minCov, setMinCov] = useState(0.5);
  const [dirty, setDirty] = useState(false);

  // Seed the local editor from saved settings once they arrive, and re-seed
  // after a save — but never stomp on values the operator is mid-edit.
  useEffect(() => {
    if (!saved || dirty) return;
    setVariant(saved.MODEL_VARIANT ?? "");
    setMinProb(saved.MODEL_MIN_PROBABILITY ?? 0.15);
    setMinCov(saved.MODEL_MIN_COVERAGE ?? 0.5);
  }, [saved, dirty]);

  if (isLoading) return <div className="empty">Loading model…</div>;

  const effectiveVariant = variant || status?.default_variant || "";
  const selected = status?.variants.find((v) => v.key === effectiveVariant);

  const save = (patch: Record<string, unknown>) => {
    update.mutate({ global: patch }, { onSuccess: () => setDirty(false) });
  };

  return (
    <div>
      <div className="dashboard-head">
        <h1 className="page-title">Mover Model</h1>
        <button className="chart-btn" onClick={() => reload.mutate()} disabled={reload.isPending}>
          {reload.isPending ? "Reloading…" : "Reload artifact"}
        </button>
      </div>

      {/* ---- 1. artifact ---- */}
      <div className="widget widget-wide" style={{ marginBottom: 12 }}>
        <h3>
          Artifact <span className="meta">{status?.path}</span>
        </h3>
        {!status?.available ? (
          <div className="empty">
            No model loaded{status?.error ? ` — ${status.error}` : ""}. Build one with{" "}
            <code>python AIdataset/model/export_live.py</code>, then hit Reload artifact.
          </div>
        ) : (
          <div style={{ display: "flex", gap: 24, flexWrap: "wrap", padding: "8px 12px" }}>
            <Metric label="Built" value={status.built_at?.slice(0, 16).replace("T", " ") ?? "—"} />
            <Metric label="Target" value={status.target ?? "—"} hint="What the model predicts" />
            <Metric label="Variants" value={String(status.variants.length)} />
            <Metric label="Symbol priors" value={status.n_symbols.toLocaleString()} />
            <Metric label="Category priors" value={String(status.n_categories)} />
          </div>
        )}
      </div>

      {status?.available && (
        <>
          {/* ---- 2. variant picker ---- */}
          <div className="widget widget-wide" style={{ marginBottom: 12 }}>
            <h3>
              Variant{" "}
              <span className="meta">
                trained offline on the announcement corpus, held out on the most recent
                quarter
              </span>
            </h3>
            <VariantTable
              variants={status.variants}
              selected={effectiveVariant}
              onSelect={(k) => {
                setVariant(k);
                setDirty(true);
              }}
            />
            <div style={{ padding: "8px 12px", display: "flex", gap: 8, alignItems: "center" }}>
              <button
                className="chart-btn"
                disabled={!dirty || update.isPending}
                onClick={() => save({ MODEL_VARIANT: variant })}
              >
                {update.isPending ? "Saving…" : "Save variant"}
              </button>
              <span className="meta">
                Live: <code>{saved?.MODEL_VARIANT || status.default_variant}</code>
              </span>
            </div>
          </div>

          {/* ---- 3. thresholds + replay ---- */}
          <div className="widget widget-wide" style={{ marginBottom: 12 }}>
            <h3>
              Threshold{" "}
              <span className="meta">
                what this setting would have done to real recorded signals
              </span>
            </h3>
            <div style={{ padding: "8px 12px", display: "flex", gap: 32, flexWrap: "wrap" }}>
              <label style={{ display: "block", minWidth: 260 }}>
                <div className="meta">
                  Min P(mover) to allow — <span className="mono">{pct(minProb)}</span>
                </div>
                <input
                  type="range"
                  min={0}
                  max={0.9}
                  step={0.01}
                  value={minProb}
                  style={{ width: "100%" }}
                  onChange={(e) => {
                    setMinProb(Number(e.target.value));
                    setDirty(true);
                  }}
                />
                {selected && (
                  <div className="meta">
                    Holdout percentiles:{" "}
                    {Object.entries(selected.percentiles)
                      .map(([q, v]) => `${q}=${pct(v)}`)
                      .join("  ")}
                  </div>
                )}
              </label>

              <label style={{ display: "block", minWidth: 260 }}>
                <div className="meta">
                  Min feature coverage to gate — <span className="mono">{pct(minCov, 0)}</span>
                </div>
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={minCov}
                  style={{ width: "100%" }}
                  onChange={(e) => {
                    setMinCov(Number(e.target.value));
                    setDirty(true);
                  }}
                />
                <div className="meta">
                  Below this, the score abstains instead of blocking. 0 = never abstain.
                </div>
              </label>

              <button
                className="chart-btn"
                disabled={!dirty || update.isPending}
                onClick={() =>
                  save({
                    MODEL_MIN_PROBABILITY: minProb,
                    MODEL_MIN_COVERAGE: minCov,
                    MODEL_VARIANT: variant,
                  })
                }
              >
                {update.isPending ? "Saving…" : "Save thresholds"}
              </button>
            </div>

            <Replay variant={effectiveVariant} minProbability={minProb} minCoverage={minCov} />
          </div>

          {/* ---- 4. the two switches ---- */}
          <div className="widget widget-wide" style={{ marginBottom: 12 }}>
            <h3>Switches</h3>
            <div style={{ padding: "8px 12px", display: "grid", gap: 14 }}>
              <div style={{ display: "flex", gap: 12, alignItems: "flex-start" }}>
                <Toggle
                  on={!!saved?.MODEL_ENABLED}
                  onChange={(next) => save({ MODEL_ENABLED: next })}
                  data-testid="model-enabled"
                />
                <div>
                  <div>Score every signal</div>
                  <div className="meta">
                    Attaches P(mover) to the decision context and to this page's replay.
                    Pure telemetry — with this on and the gate off, nothing is ever
                    blocked.
                  </div>
                </div>
              </div>

              <div style={{ display: "flex", gap: 12, alignItems: "flex-start" }}>
                <Toggle
                  on={!!saved?.MODEL_GATE_ENABLED}
                  disabled={!saved?.MODEL_ENABLED}
                  onChange={(next) => save({ MODEL_GATE_ENABLED: next })}
                  data-testid="model-gate-enabled"
                />
                <div>
                  <div>
                    Let a low score BLOCK a trade{" "}
                    {!saved?.MODEL_ENABLED && <span className="meta">(needs scoring on)</span>}
                  </div>
                  <div className="meta">
                    Arms the veto at the threshold above. Check the replay first: the
                    offline measurement (plan.txt, Phase 5) puts pooled headroom over
                    the base rate under 1pp, so this model earns its keep by RANKING,
                    not by filtering. A gate that blocks movers as often as non-movers
                    is only cutting volume.
                  </div>
                </div>
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
