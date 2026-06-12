// Placeholder for the 7 tabs the user said they'll add later. We
// don't want them to 404 — show a clear "Not built yet" message
// instead. Tells the user the route is real, the page is coming, and
// points at the right sibling tasks.

import type { TabKey } from "../router";

const COPY: Record<TabKey, { title: string; why: string }> = {
  dashboard: { title: "Dashboard", why: "Already built." },
  prompts: { title: "Prompts", why: "Already built." },
  accounts: { title: "Broker Accounts", why: "Manage Fyers paper/live accounts and OAuth flow." },
  notifications: { title: "Notifications", why: "Telegram / Discord / email / webhook-out channels." },
  webhooks: { title: "Webhooks", why: "Inbound + outbound webhook registrations and delivery log." },
  backtest: { title: "Backtest", why: "Run, view, and compare historical backtests." },
  rules: { title: "Signal Rules", why: "Author the rule engine conditions and priorities." },
  strategies: { title: "Strategies", why: "Group rules into named, toggleable strategies." },
  settings: { title: "Settings", why: "Global settings (TRADING_MODE, risk caps, poll interval)." },
};

export default function NotBuiltYet({ tab }: { tab: TabKey }) {
  const c = COPY[tab];
  return (
    <div>
      <h1 className="page-title">{c.title}</h1>
      <div className="placeholder" data-testid="not-built-yet">
        <h2>Not built yet</h2>
        <p>{c.why}</p>
        <p style={{ marginTop: 12 }}>
          Switch to <a href="#/dashboard">Dashboard</a> or <a href="#/prompts">Prompts</a> in the meantime.
        </p>
      </div>
    </div>
  );
}
