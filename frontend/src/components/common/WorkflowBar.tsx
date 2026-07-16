// WorkflowBar — the pipeline at a glance, clickable end to end.
//
// One chip per workflow stage, in the order a filing actually travels:
//   NEWS (monitors) → AI (analyzer) → RULES (decision) → RISK (gates)
//   → EXEC (broker) → DATA (dataset collector)
//
// Each chip carries a status dot (ok / warn / err / off) and a one-word
// detail, and clicking it jumps to the page that controls that stage.
// The bar renders on EVERY page, so the operator always sees where the
// pipeline is healthy and where it is switched off — e.g. the AI master
// toggle silently OFF (which once cost 3,386 skipped filings) now shows
// as a red chip on every screen.
//
// Data comes from existing polling hooks only — no new backend load
// beyond what the Dashboard already generates.

import {
  useAnnouncements,
  useDatasetBackfillStatus,
  useFyersStatus,
  useGlobalSettings,
  useRiskState,
  useRules,
  useStrategies,
} from "../../hooks/useApi";
import type { TabKey } from "../../router";

type ChipState = "ok" | "warn" | "err" | "off";

function minutesSince(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const t = new Date(iso.endsWith("Z") ? iso : iso + "Z").getTime();
  if (Number.isNaN(t)) return null;
  return Math.max(0, Math.round((Date.now() - t) / 60000));
}

interface Chip {
  key: string;
  label: string;
  state: ChipState;
  detail: string;
  title: string;
  tab: TabKey;
}

export function WorkflowBar({
  tab,
  navigate,
}: {
  tab: TabKey;
  navigate: (t: TabKey) => void;
}) {
  const { data: settings } = useGlobalSettings();
  const { data: anns, isError: annsError } = useAnnouncements(1);
  const { data: strategies } = useStrategies();
  const { data: rules } = useRules();
  const { data: risk, isError: riskError } = useRiskState();
  const { data: fyers } = useFyersStatus();
  const { data: dsStatus } = useDatasetBackfillStatus();

  const g = settings?.global;

  // -- NEWS: is fresh material still flowing in? -----------------------
  const lastAge = minutesSince(anns?.[0]?.received_at ?? null);
  const news: Chip = {
    key: "news",
    label: "NEWS",
    tab: "dashboard",
    ...(annsError
      ? { state: "err" as ChipState, detail: "API?", title: "Announcements API unreachable" }
      : lastAge === null
        ? { state: "warn" as ChipState, detail: "NONE", title: "No filings recorded yet" }
        : lastAge <= 15
          ? {
              state: "ok" as ChipState,
              detail: `${lastAge}M AGO`,
              title: `Last filing received ${lastAge} min ago`,
            }
          : {
              state: "warn" as ChipState,
              detail: lastAge < 600 ? `${lastAge}M AGO` : "QUIET",
              title:
                `Last filing received ${lastAge} min ago — normal outside ` +
                "market hours; during the session this may mean the monitors are blocked",
            }),
  };

  // -- AI: the analyzer master switch ----------------------------------
  const aiOn = g?.AI_ANALYSIS_ENABLED ?? true;
  const fastTrack = g?.FAST_TRACK_ENABLED ?? false;
  const ai: Chip = {
    key: "ai",
    label: "AI",
    tab: "dashboard",
    state: aiOn ? "ok" : "err",
    detail: aiOn ? (fastTrack ? "LLM+FAST" : "LLM") : "OFF",
    title: aiOn
      ? `AI analysis ON${fastTrack ? " with deterministic fast track" : ""}`
      : "AI analysis is OFF — filings are collected but never analyzed, so no signals fire",
  };

  // -- RULES: can anything actually trade? ------------------------------
  const enabledStrategyIds = new Set(
    (strategies ?? []).filter((s) => s.enabled).map((s) => s.id)
  );
  const liveRules = (rules ?? []).filter(
    (r) => r.enabled && enabledStrategyIds.has(r.strategy_id)
  ).length;
  const rulesChip: Chip = {
    key: "rules",
    label: "RULES",
    tab: "rules",
    state: liveRules > 0 ? "ok" : "warn",
    detail: liveRules > 0 ? `${liveRules} LIVE` : "NONE",
    title:
      liveRules > 0
        ? `${liveRules} enabled rule(s) in enabled strategies`
        : "No live rules — every analysis falls back to HOLD and nothing trades",
  };

  // -- RISK: are entries allowed right now? ------------------------------
  const riskChip: Chip = {
    key: "risk",
    label: "RISK",
    tab: "settings",
    ...(riskError || !risk
      ? { state: "off" as ChipState, detail: "—", title: "Risk state unavailable" }
      : risk.entries_allowed
        ? {
            state: "ok" as ChipState,
            detail: `${risk.trades_today} TODAY`,
            title: `Entries allowed — ${risk.trades_today} trade(s) today`,
          }
        : {
            state: "err" as ChipState,
            detail: (risk.entry_block_reason ?? "BLOCKED").toUpperCase().slice(0, 14),
            title: `Entries BLOCKED: ${risk.entry_block_reason ?? risk.disabled_reason ?? "unknown"}`,
          }),
  };

  // -- EXEC: which broker, and is it usable? -----------------------------
  const mode = (g?.TRADING_MODE ?? "paper").toString().toLowerCase();
  const fyersReady = Boolean(fyers?.has_token);
  const exec: Chip = {
    key: "exec",
    label: "EXEC",
    tab: "accounts",
    ...(mode === "live"
      ? fyersReady
        ? {
            state: "ok" as ChipState,
            detail: "LIVE",
            title: `LIVE mode — Fyers ${fyers?.enabled ? "enabled" : "authorised (trading switch OFF)"}`,
          }
        : {
            state: "err" as ChipState,
            detail: "LIVE NO-BROKER",
            title: "LIVE mode but Fyers has no token — orders will fail",
          }
      : {
          state: "ok" as ChipState,
          detail: "PAPER",
          title: `Paper trading${fyersReady ? " (Fyers feed available)" : ""}`,
        }),
  };

  // -- DATA: is the dataset collector alive? ----------------------------
  const collector = dsStatus?.collector;
  const data: Chip = {
    key: "data",
    label: "DATA",
    tab: "dataset",
    ...(collector?.running
      ? {
          state: "ok" as ChipState,
          detail: `+${collector.enriched_session ?? 0}`,
          title: `Dataset collector active — ${collector.enriched_session ?? 0} rows enriched this session`,
        }
      : {
          state: "off" as ChipState,
          detail: "OFF",
          title: "Dataset collector not running",
        }),
  };

  const chips = [news, ai, rulesChip, riskChip, exec, data];

  return (
    <div className="workflowbar" data-testid="workflow-bar">
      {chips.map((c, i) => (
        <span key={c.key} style={{ display: "inline-flex", alignItems: "center" }}>
          <button
            className={`wf-chip ${c.state}${tab === c.tab ? " active" : ""}`}
            onClick={() => navigate(c.tab)}
            title={c.title}
            data-testid={`wf-${c.key}`}
          >
            <span className="wf-dot" />
            <span className="wf-label">{c.label}</span>
            <span className="wf-detail">{c.detail}</span>
          </button>
          {i < chips.length - 1 && <span className="wf-arrow">›</span>}
        </span>
      ))}
    </div>
  );
}
