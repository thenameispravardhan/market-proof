import { lazy, Suspense, useEffect, useState } from "react";
import { useRouter } from "./router";
import type { TabKey } from "./router";
import { useWebSocket } from "./hooks/useWebSocket";
import {
  useFyersAuthorizeUrl,
  useGlobalSettings,
  useMarketIndices,
} from "./hooks/useApi";
import { useTheme } from "./hooks/useTheme";

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

const Trade = lazy(() => import("./pages/Trade"));
const TradeHistory = lazy(() => import("./pages/TradeHistory"));
const Rules = lazy(() => import("./pages/Rules"));
const Strategies = lazy(() => import("./pages/Strategies"));
const Accounts = lazy(() => import("./pages/Accounts"));
const Notifications = lazy(() => import("./pages/Notifications"));
const Webhooks = lazy(() => import("./pages/Webhooks"));
const Backtest = lazy(() => import("./pages/Backtest"));
const Settings = lazy(() => import("./pages/Settings"));

const TABS: { key: TabKey; label: string; emoji: string; group: "MAIN" | "OPS" | "CFG" }[] = [
  { key: "dashboard",      label: "Dashboard",      emoji: "▤", group: "MAIN" },
  { key: "trade",          label: "Trade",          emoji: "▶", group: "MAIN" },
  { key: "trades",         label: "Trade History",  emoji: "₹", group: "MAIN" },
  { key: "prompts",        label: "Prompts",        emoji: "✎", group: "MAIN" },
  { key: "rules",          label: "Rules",          emoji: "≡", group: "MAIN" },
  { key: "strategies",     label: "Strategies",     emoji: "◈", group: "MAIN" },
  { key: "backtest",       label: "Backtest",       emoji: "↻", group: "OPS"  },
  { key: "accounts",       label: "Accounts",       emoji: "▦", group: "OPS"  },
  { key: "notifications",  label: "Notifications",  emoji: "◉", group: "OPS"  },
  { key: "webhooks",       label: "Webhooks",       emoji: "⇄", group: "OPS"  },
  { key: "settings",       label: "Settings",       emoji: "⚙", group: "CFG"  },
];

function PageContent({ tab }: { tab: TabKey }) {
  switch (tab) {
    case "dashboard": return <Dashboard />;
    case "trade": return <Trade />;
    case "trades": return <TradeHistory />;
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

// Bloomberg-style status bar at the bottom of the screen. Shows
// WS state, trading mode, capital, a live ticker of NSE indices
// (sourced from Fyers via /api/market/indices), and the IST clock.
function StatusBar({ wsStatus }: { wsStatus: string }) {
  const { data: settings } = useGlobalSettings();
  const { data: market } = useMarketIndices();
  const authorize = useFyersAuthorizeUrl();
  const mode = settings?.global?.TRADING_MODE ?? "paper";
  const capital = settings?.global?.PORTFOLIO_VALUE;

  // Local clock, ticking every second.
  const [now, setNow] = useState(new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);
  const istTime = now.toLocaleTimeString("en-IN", {
    timeZone: "Asia/Kolkata",
    hour12: false,
  });

  // Format a number in Indian style (24,856.40).
  const fmt = (v: number | null | undefined): string => {
    if (v === null || v === undefined) return "—";
    return v.toLocaleString("en-IN", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  };
  const fmtPct = (v: number | null | undefined): string => {
    if (v === null || v === undefined) return "—";
    const sign = v > 0 ? "+" : "";
    return `${sign}${v.toFixed(2)}%`;
  };

  return (
    <div className="statusbar" data-testid="statusbar">
      <div className="seg">
        <span className="key">WS</span>
        <span className={`ws-status ${wsStatus}`}>
          <span className="dot" />
          <span className="ws-status-text">{wsStatus.toUpperCase()}</span>
        </span>
      </div>
      <div className="seg">
        <span className="key">MODE</span>
        <span className={`val ${mode === "live" ? "down" : "up"}`}>
          {mode.toUpperCase()}
        </span>
      </div>
      {capital !== undefined && capital !== null && (
        <div className="seg">
          <span className="key">CAP</span>
          <span className="val">₹{Number(capital).toLocaleString("en-IN")}</span>
        </div>
      )}

      <div className="spacer" />

      <div className="ticker" title={market?.ok ? "Live Fyers data" : (market?.reason ?? "loading")}>
        {(market?.indices ?? []).map((idx) => {
          const up = (idx.change ?? 0) >= 0;
          return (
            <span key={idx.key} className="seg">
              <span className="sym">{idx.key}</span>
              <span className="px">{fmt(idx.last_price)}</span>
              <span className={`ch ${up ? "up" : "dn"}`}>
                {fmtPct(idx.change_pct)}
              </span>
            </span>
          );
        })}
        {market && !market.ok && market.configured === false && (
          <span className="seg ticker-empty">
            <span className="sym">MARKET</span>
            <span className="px">FYERS NOT CONFIGURED</span>
          </span>
        )}
        {market && !market.ok && market.configured === false && (
          <button
            className="seg ticker-connect"
            onClick={async () => {
              try {
                const resp = await authorize.mutateAsync();
                if (resp.configured && resp.url) {
                  window.open(
                    resp.url,
                    "fyers-oauth",
                    "width=600,height=700,left=200,top=100"
                  );
                }
              } catch {
                /* swallow — the Accounts page has the full UI */
              }
            }}
            data-testid="fyers-connect-statusbar"
            title="Click to connect Fyers in a popup"
          >
            <span className="sym">FYERS</span>
            <span className="px">CLICK TO CONNECT →</span>
          </button>
        )}
        {market && !market.ok && market.configured === true && (
          <span className="seg ticker-empty">
            <span className="sym">MARKET</span>
            <span className="px">{market.reason ?? "FETCH FAILED"}</span>
          </span>
        )}
      </div>

      <div className="spacer" />

      <div className="seg">
        <span className="key">IST</span>
        <span className="val">{istTime}</span>
      </div>
    </div>
  );
}

export default function App() {
  const [tab, navigate] = useRouter();
  const { status } = useWebSocket({ channels: ["signals", "trades", "positions"] });
  const [sidebarOpen, setSidebarOpen] = useState<boolean>(readSidebarOpen);

  // Apply theme on mount + on change. The hook writes data-theme to
  // <html> and persists the choice in localStorage.
  useTheme();

  // Persist sidebar state on change.
  useEffect(() => {
    try {
      localStorage.setItem(SIDEBAR_KEY, sidebarOpen ? "1" : "0");
    } catch {
      /* localStorage may be unavailable (private mode); ignore */
    }
  }, [sidebarOpen]);

  const toggleSidebar = () => setSidebarOpen((v) => !v);

  // Group the nav by section for the Bloomberg-style grouped sidebar.
  const sections: ("MAIN" | "OPS" | "CFG")[] = ["MAIN", "OPS", "CFG"];

  return (
    <div className="app">
      <nav className={`sidebar${sidebarOpen ? "" : " collapsed"}`} aria-label="Primary">
        <div className="sidebar-header">
          <div className="logo">
            <span className="logo-icon">◆</span>
            {sidebarOpen && (
              <span className="logo-text">
                TRADE<span className="accent">BOT</span>
              </span>
            )}
          </div>
          <button
            className="sidebar-toggle"
            onClick={toggleSidebar}
            aria-label={sidebarOpen ? "Collapse sidebar" : "Expand sidebar"}
            title={sidebarOpen ? "Collapse sidebar" : "Expand sidebar"}
            data-testid="sidebar-toggle"
          >
            {sidebarOpen ? "◀" : "▶"}
          </button>
        </div>

        <ul className="nav-list">
          {sections.map((section) => {
            const items = TABS.filter((t) => t.group === section);
            if (items.length === 0) return null;
            return (
              <li key={section}>
                {sidebarOpen && <div className="nav-section">{section}</div>}
                <ul className="nav-list" style={{ padding: 0 }}>
                  {items.map(({ key, label, emoji }) => (
                    <li key={key}>
                      <button
                        className={`nav-item${tab === key ? " active" : ""}`}
                        onClick={() => navigate(key)}
                        data-testid={`nav-${key}`}
                        title={label}
                      >
                        <span className="nav-emoji">{emoji}</span>
                        {sidebarOpen && <span className="nav-label">{label}</span>}
                        {sidebarOpen && tab === key && <span className="nav-key">●</span>}
                      </button>
                    </li>
                  ))}
                </ul>
              </li>
            );
          })}
        </ul>

        <div className="sidebar-footer">
          <span className={`ws-status ${status}`} title={`WebSocket: ${status}`}>
            <span className="dot" />
            {sidebarOpen && <span className="ws-status-text">{status.toUpperCase()}</span>}
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
        <div className="content-scroll">
          <Suspense fallback={<div className="empty loading">Loading…</div>}>
            <PageContent tab={tab} />
          </Suspense>
        </div>
        <StatusBar wsStatus={status} />
      </main>
    </div>
  );
}
