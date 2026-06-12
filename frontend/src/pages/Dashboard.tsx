// Dashboard page: 6 widgets in a responsive grid:
//   - Announcements feed       (top-left)
//   - AI Analysis panel        (top-mid)
//   - Trade Signals            (top-right)
//   - Active Positions         (mid-left)
//   - Risk Metrics (wide)      (mid-wide)
//   - P&L chart (wide)         (bot-wide)

import { AnnouncementsFeed } from "../components/dashboard/AnnouncementsFeed";
import { AIAnalysisPanel } from "../components/dashboard/AIAnalysisPanel";
import { TradeSignals } from "../components/dashboard/TradeSignals";
import { ActivePositions } from "../components/dashboard/ActivePositions";
import { PnLChart } from "../components/dashboard/PnLChart";
import { RiskMetrics } from "../components/dashboard/RiskMetrics";

export default function Dashboard() {
  return (
    <div>
      <h1 className="page-title">Dashboard</h1>
      <div className="dashboard-grid">
        <AnnouncementsFeed />
        <AIAnalysisPanel />
        <TradeSignals />
        <ActivePositions />
        <RiskMetrics />
        <PnLChart />
      </div>
    </div>
  );
}
