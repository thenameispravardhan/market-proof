import { lazy, Suspense } from "react";
import { useRouter } from "./router";
import type { TabKey } from "./router";
import { useWebSocket } from "./hooks/useWebSocket";

// Eager-load pages — the bundle is small enough that lazy() isn't needed,
// but we lazy-load the backtest/settings pages to keep initial paint fast.
import Dashboard from "./pages/Dashboard";
import Prompts from "./pages/Prompts";

const Rules = lazy(() => import("./pages/Rules"));
const Strategies = lazy(() => import("./pages/Strategies"));
const Accounts = lazy(() => import("./pages/Accounts"));
const Notifications = lazy(() => import("./pages/Notifications"));
const Webhooks = lazy(() => import("./pages/Webhooks"));
const Backtest = lazy(() => import("./pages/Backtest"));
const Settings = lazy(() => import("./pages/Settings"));

const TABS: { key: TabKey; label: string; emoji: string }[] = [
  { key: "dashboard", label: "Dashboard", emoji: "📊" },
  { key: "prompts", label: "Prompts", emoji: "✏️" },
  { key: "rules", label: "Rules", emoji: "📋" },
  { key: "strategies", label: "Strategies", emoji: "🎯" },
  { key: "accounts", label: "Accounts", emoji: "🏦" },
  { key: "notifications", label: "Notifications", emoji: "🔔" },
  { key: "webhooks", label: "Webhooks", emoji: "🔗" },
  { key: "backtest", label: "Backtest", emoji: "⚗️" },
  { key: "settings", label: "Settings", emoji: "⚙️" },
];

function PageContent({ tab }: { tab: TabKey }) {
  switch (tab) {
    case "dashboard": return <Dashboard />;
    case "prompts": return <Prompts />;
    case "rules": return <Rules />;
    case "strategies": return <Strategies />;
    case "accounts": return <Accounts />;
    case "notifications": return <Notifications />;
    case "webhooks": return <Webhooks />;
    case "backtest": return <Backtest />;
    case "settings": return <Settings />;
    default: return <Dashboard />;
  }
}

export default function App() {
  const [tab, navigate] = useRouter();
  const { status } = useWebSocket({ channels: ["signals", "trades", "positions"] });

  return (
    <div className="app">
      <nav className="sidebar">
        <div className="logo">
          <span className="logo-icon">🤖</span>
          <span className="logo-text">TradeBot</span>
        </div>
        <ul className="nav-list">
          {TABS.map(({ key, label, emoji }) => (
            <li key={key}>
              <button
                className={`nav-item${tab === key ? " active" : ""}`}
                onClick={() => navigate(key)}
                data-testid={`nav-${key}`}
              >
                <span className="nav-emoji">{emoji}</span>
                <span className="nav-label">{label}</span>
              </button>
            </li>
          ))}
        </ul>
        <div className="sidebar-footer">
          <span className={`ws-status ${status}`} title={`WebSocket: ${status}`}>
            {status === "open" ? "●" : status === "connecting" ? "◌" : "○"}
            {" "}<span className="nav-label">{status}</span>
          </span>
        </div>
      </nav>

      <main className="content">
        <Suspense fallback={<div className="empty loading">Loading page…</div>}>
          <PageContent tab={tab} />
        </Suspense>
      </main>
    </div>
  );
}
