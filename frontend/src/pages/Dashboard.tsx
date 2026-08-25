// Dashboard page: a trading-mode banner, a summary stat row, then the
// live widgets in a responsive grid:
//   - News Pipeline (unified scrape → analyze → signal table)
//   - Active Positions (with close controls) / Risk Metrics / P&L chart

import { NewsPipeline } from "../components/dashboard/NewsPipeline";
import { ActivePositions } from "../components/dashboard/ActivePositions";
import { PipelineLatency } from "../components/dashboard/PipelineLatency";
import { PnLChart } from "../components/dashboard/PnLChart";
import { RiskMetrics } from "../components/dashboard/RiskMetrics";
import { SystemResources } from "../components/dashboard/SystemResources";
import { AccountToggles } from "../components/dashboard/AccountToggles";
import { AiAnalysisToggle } from "../components/dashboard/AiAnalysisToggle";
import { useDashboardSummary, useGlobalSettings } from "../hooks/useApi";

function money(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  const sign = v > 0 ? "+" : "";
  return sign + v.toLocaleString(undefined, { maximumFractionDigits: 0 });
}

function pnlClass(v: number | null | undefined): string {
  if (!v) return "";
  return v > 0 ? "pnl-pos" : v < 0 ? "pnl-neg" : "";
}

function StatRow() {
  const { data } = useDashboardSummary();
  const stats = [
    { label: "Open positions", value: data ? String(data.open_positions) : "—", cls: "" },
    { label: "Realised P&L (today)", value: data ? money(data.todays_realized_pnl) : "—", cls: pnlClass(data?.todays_realized_pnl) },
    { label: "Unrealised P&L", value: data ? money(data.todays_unrealized_pnl) : "—", cls: pnlClass(data?.todays_unrealized_pnl) },
    { label: "Hard risk rules", value: data ? String(data.hard_rules_count) : "—", cls: "" },
  ];
  return (
    <div className="stat-row">
      {stats.map((s) => (
        <div className="stat" key={s.label}>
          <div className="stat-label">{s.label}</div>
          <div className={`stat-value ${s.cls}`}>{s.value}</div>
        </div>
      ))}
    </div>
  );
}

function ModeBanner() {
  const { data } = useGlobalSettings();
  const mode = data?.global?.TRADING_MODE ?? "paper";
  // Only surface the LIVE-trading warning. Paper mode shows no banner
  // (the mode is already indicated in the bottom status bar).
  if (mode !== "live") return null;
  return (
    <div className="mode-banner live">
      <span>LIVE TRADING — real orders are being placed</span>
    </div>
  );
}

export default function Dashboard() {
  return (
    <div>
      <div className="dashboard-head">
        <h1 className="page-title">Dashboard</h1>
        <AiAnalysisToggle />
        <AccountToggles />
      </div>
      <ModeBanner />
      <StatRow />
      <div className="dashboard-grid">
        <NewsPipeline />
        <ActivePositions />
        <RiskMetrics />
        <PipelineLatency />
        <SystemResources />
        <PnLChart />
      </div>
    </div>
  );
}
