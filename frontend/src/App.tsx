import { lazy, Suspense, useEffect, useState } from "react";
import { useRouter } from "./router";
import type { TabKey } from "./router";
import { useWebSocket } from "./hooks/useWebSocket";

// Persisted sidebar open/closed state. Default open. Stored in
// localStorage so the choice survives reloads.
const SIDEBAR_KEY = "mavis.sidebarOpen";
function readSidebarOpen(): boolean {
  try {
    const v = localStorage.getItem(SIDEBAR_KEY);
    if (v === null) return true;
    return v === "1";
  } catch {
    return true;
  }
}

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
  const [sidebarOpen, setSidebarOpen] = useState<boolean>(readSidebarOpen);

  // Persist sidebar state on change.
  useEffect(() => {
    try {
      localStorage.setItem(SIDEBAR_KEY, sidebarOpen ? "1" : "0");
    } catch {
      /* localStorage may be unavailable (private mode); ignore */
    }
  }, [sidebarOpen]);

  const toggleSidebar = () => setSidebarOpen((v) => !v);

  return (
    <div className="app">
      <nav className={`sidebar${sidebarOpen ? "" : " collapsed"}`} aria-label="Primary">
        <div className="logo">
          <button
            className="sidebar-toggle"
            onClick={toggleSidebar}
            aria-label={sidebarOpen ? "Collapse sidebar" : "Expand sidebar"}
            title={sidebarOpen ? "Collapse sidebar" : "Expand sidebar"}
            data-testid="sidebar-toggle"
          >
            {sidebarOpen ? "◀" : "▶"}
          </button>
          {sidebarOpen && <span className="logo-text">TradeBot</span>}
        </div>
        <ul className="nav-list">
          {TABS.map(({ key, label, emoji }) => (
            <li key={key}>
              <button
                className={`nav-item${tab === key ? " active" : ""}`}
                onClick={() => navigate(key)}
                data-testid={`nav-${key}`}
                title={label}
              >
                <span className="nav-emoji">{emoji}</span>
                {sidebarOpen && <span className="nav-label">{label}</span>}
              </button>
            </li>
          ))}
        </ul>
        <div className="sidebar-footer">
          <span
            className={`ws-status ${status}`}
            title={`WebSocket: ${status}`}
          >
            {status === "open" ? "●" : status === "connecting" ? "◌" : "○"}
            {sidebarOpen && <span className="nav-label">{status}</span>}
          </span>
        </div>
      </nav>

      <main className="content">
        {!sidebarOpen && (
          <button
            className="sidebar-toggle-floating"
            onClick={toggleSidebar}
            aria-label="Expand sidebar"
            title="Expand sidebar"
            data-testid="sidebar-toggle-floating"
          >
            ☰
          </button>
        )}
        <Suspense fallback={<div className="empty loading">Loading page…</div>}>
          <PageContent tab={tab} />
        </Suspense>
      </main>
    </div>
  );
}
