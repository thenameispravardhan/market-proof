// useApi — TanStack Query wrappers for every backend endpoint.
// All hooks are named consistently: use<Resource>[s]() for lists,
// use<Resource>(<id>) for single items, use<Verb><Resource>() for mutations.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type {
  Analysis,
  Announcement,
  AuditLogEntry,
  BacktestRun,
  BrokerAccount,
  DashboardSummary,
  GlobalSettings,
  SettingsSchema,
  NotificationChannel,
  OptionChainResponse,
  PendingOrder,
  PendingOrdersResponse,
  PlaceOrderRequest,
  PlaceOrderResponse,
  Position,
  PromptHistoryEntry,
  PromptTemplate,
  QuoteResponse,
  SearchResponse,
  Signal,
  SignalRule,
  Strategy,
  Trade,
  Webhook,
} from "../types";

// ---- Dashboard / core ----

export function useAnnouncements(limit = 20) {
  return useQuery<Announcement[]>({
    queryKey: ["announcements", limit],
    queryFn: () => api.get(`/api/announcements/recent?limit=${limit}`),
    refetchInterval: 5000,
  });
}

export function useAnalyses(limit = 10) {
  return useQuery<Analysis[]>({
    queryKey: ["analyses", limit],
    queryFn: () => api.get(`/api/analyses/recent?limit=${limit}`),
    refetchInterval: 5000,
  });
}

export function useSignals(limit = 20) {
  return useQuery<Signal[]>({
    queryKey: ["signals", limit],
    queryFn: () => api.get(`/api/signals/recent?limit=${limit}`),
    refetchInterval: 5000,
  });
}

// ---- Market data ----

export interface IndexQuote {
  key: string;
  name: string;
  symbol: string;
  last_price: number | null;
  change: number | null;
  change_pct: number | null;
}

export interface MarketIndices {
  ok: boolean;
  configured: boolean;
  reason: string | null;
  indices: IndexQuote[];
  fetched_at: number | null;
}

// Polls /api/market/indices. The backend caches for 5s, so a 4s
// client poll keeps the ticker fresh without thrashing the upstream.
export function useMarketIndices() {
  return useQuery<MarketIndices>({
    queryKey: ["market-indices"],
    queryFn: () => api.get("/api/market/indices"),
    refetchInterval: 4000,
    refetchIntervalInBackground: false,
    staleTime: 3000,
  });
}


export function usePositions() {
  return useQuery<Position[]>({
    queryKey: ["positions"],
    queryFn: () => api.get("/api/positions"),
    refetchInterval: 5000,
  });
}

export function useManagedPositions() {
  return useQuery<import("../types").ManagedPosition[]>({
    queryKey: ["managed-positions"],
    queryFn: () => api.get("/api/positions/managed"),
    refetchInterval: 4000,
    // Trade manager only runs outside TESTING; tolerate 503.
    retry: false,
  });
}

export function useClosePosition() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (symbol: string) =>
      api.post(`/api/positions/${encodeURIComponent(symbol)}/close`, {}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["positions"] });
      qc.invalidateQueries({ queryKey: ["managed-positions"] });
      qc.invalidateQueries({ queryKey: ["trades"] });
      qc.invalidateQueries({ queryKey: ["dashboard-summary"] });
    },
  });
}

// Edit an open position's stop-loss / target. Pass null for a level to
// clear it. Refreshes the managed book + positions so both the
// dashboard and Trade History reflect the new levels immediately.
export function useUpdatePositionLevels() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      symbol,
      stop_loss,
      target,
    }: {
      symbol: string;
      stop_loss: number | null;
      target: number | null;
    }) =>
      api.post(`/api/positions/${encodeURIComponent(symbol)}/levels`, {
        stop_loss,
        target,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["managed-positions"] });
      qc.invalidateQueries({ queryKey: ["positions"] });
    },
  });
}

export function useCloseAllPositions() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post("/api/positions/close-all", {}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["positions"] });
      qc.invalidateQueries({ queryKey: ["managed-positions"] });
      qc.invalidateQueries({ queryKey: ["trades"] });
      qc.invalidateQueries({ queryKey: ["dashboard-summary"] });
    },
  });
}

export function useTrades(limit = 200, status?: string) {
  const qs = status ? `?limit=${limit}&status=${status}` : `?limit=${limit}`;
  return useQuery<Trade[]>({
    queryKey: ["trades", limit, status ?? null],
    queryFn: () => api.get(`/api/trades${qs}`),
    refetchInterval: 10000,
  });
}

export function useDashboardSummary() {
  return useQuery<DashboardSummary>({
    queryKey: ["dashboard-summary"],
    queryFn: () => api.get("/api/dashboard/summary"),
    refetchInterval: 10000,
  });
}

// ---- Prompts (T5) ----

export function usePrompts() {
  return useQuery<PromptTemplate[]>({
    queryKey: ["prompts"],
    queryFn: async () => {
      const r = await api.get<{ prompts: PromptTemplate[] }>("/api/prompts");
      return r.prompts;
    },
  });
}

export function usePromptHistory(eventType: string | null) {
  return useQuery<PromptHistoryEntry[]>({
    queryKey: ["prompt-history", eventType],
    queryFn: async () => {
      if (!eventType) return [];
      const r = await api.get<{ history: PromptHistoryEntry[] }>(
        `/api/prompts/${eventType}/history`
      );
      return r.history;
    },
    enabled: Boolean(eventType),
  });
}

export function useUpdatePrompt(eventType: string | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Partial<PromptTemplate> & { change_note?: string }) =>
      api.post<PromptTemplate>(`/api/prompts/${eventType}`, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["prompts"] });
      qc.invalidateQueries({ queryKey: ["prompt-history", eventType] });
    },
  });
}

// Fetches the factory-default field values for a template WITHOUT
// persisting. Backs the editor's "Reset to default" button — the UI fills
// the form from this; the operator must Save to change the active version.
export function useDefaultPrompt(eventType: string | null) {
  return useMutation<Partial<PromptTemplate>, Error, void>({
    mutationFn: () =>
      api.get<Partial<PromptTemplate>>(`/api/prompts/${eventType}/default`),
  });
}

export function useRestorePrompt(eventType: string | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (version: number) =>
      api.post<PromptTemplate>(`/api/prompts/${eventType}/restore/${version}`, {}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["prompts"] });
      qc.invalidateQueries({ queryKey: ["prompt-history", eventType] });
    },
  });
}

export function usePreviewPrompt(eventType: string | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (pdf_url: string) =>
      api.post<{ rendered_user_template: string; system_prompt: string }>(
        `/api/prompts/${eventType}/preview`,
        { pdf_url }
      ),
  });
}

// ---- Strategies (T5) ----

export function useStrategies() {
  return useQuery<Strategy[]>({
    queryKey: ["strategies"],
    queryFn: async () => {
      const r = await api.get<{ strategies: Strategy[] }>("/api/strategies");
      return r.strategies;
    },
  });
}

export function useCreateStrategy() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Partial<Strategy>) => api.post<Strategy>("/api/strategies", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["strategies"] }),
  });
}

export function useUpdateStrategy(id: number | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Partial<Strategy>) => api.put<Strategy>(`/api/strategies/${id}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["strategies"] }),
  });
}

export function useDeleteStrategy() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.delete(`/api/strategies/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["strategies"] });
      qc.invalidateQueries({ queryKey: ["rules"] });
    },
  });
}

// Flip a strategy's `enabled` flag — backs the on/off switch in the
// Strategies list. A disabled strategy's rules are excluded from the
// live analyzer (see load_rules_for_enabled_strategies). Works for any
// id (unlike useUpdateStrategy, which binds one id at hook-call time).
export function useToggleStrategy() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) =>
      api.put<Strategy>(`/api/strategies/${id}`, { enabled }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["strategies"] }),
  });
}

// ---- Signal Rules (T5) ----

export function useRules(strategyId?: number | null) {
  return useQuery<SignalRule[]>({
    queryKey: ["rules", strategyId],
    queryFn: async () => {
      const url = strategyId ? `/api/rules?strategy_id=${strategyId}` : "/api/rules";
      const r = await api.get<{ rules: SignalRule[] }>(url);
      return r.rules;
    },
  });
}

export function useCreateRule() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Partial<SignalRule>) => api.post<SignalRule>("/api/rules", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["rules"] }),
  });
}

export function useUpdateRule(id: number | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Partial<SignalRule>) => api.put<SignalRule>(`/api/rules/${id}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["rules"] }),
  });
}

export function useDeleteRule() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.delete(`/api/rules/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["rules"] }),
  });
}

// Flip a single rule's `enabled` flag — backs the on/off switch in the
// rule list. Works for any id (unlike useUpdateRule, which binds one id).
export function useToggleRule() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) =>
      api.put<SignalRule>(`/api/rules/${id}`, { enabled }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["rules"] }),
  });
}

export function useDryRunRule() {
  return useMutation({
    mutationFn: (body: { analysis: Record<string, unknown>; strategy_id?: number }) =>
      api.post<{
        matched: boolean;
        matched_rule: SignalRule | null;
        action: string;
        action_params: Record<string, unknown> | null;
      }>("/api/rules/dry-run", body),
  });
}

export function useReorderRules() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { strategy_id: number; ordered_ids: number[] }) =>
      api.post("/api/rules/reorder", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["rules"] }),
  });
}

// ---- Broker Accounts (T5) ----

export function useBrokerAccounts() {
  return useQuery<BrokerAccount[]>({
    queryKey: ["broker-accounts"],
    queryFn: async () => {
      const r = await api.get<{ accounts: BrokerAccount[] }>("/api/broker-accounts");
      return r.accounts;
    },
  });
}

// Flip a broker account's `enabled` flag — backs the dashboard
// Paper / Fyers on-off switches. A disabled account is skipped by the
// execution manager (signals route to it are blocked).
export function useToggleBrokerAccount() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) =>
      api.put<BrokerAccount>(`/api/broker-accounts/${id}`, { enabled }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["broker-accounts"] });
      // Toggling the REAL account flips TRADING_MODE (paper/live) on the
      // backend — refresh settings so the LIVE banner reacts instantly.
      qc.invalidateQueries({ queryKey: ["settings"] });
    },
  });
}

// ---- Fyers OAuth ----

export interface FyersStatus {
  // True if FYERS_APP_ID + FYERS_SECRET_KEY are loaded in .env
  // AND a Fyers broker account exists in the DB with a valid
  // access_token. The "fully wired up" state.
  connected: boolean;
  // True if OAuth is done and a valid token is held — regardless of the
  // trade on/off switch. The banner keys on this (not `connected`) so a
  // switched-OFF account isn't mislabelled "Token Expired".
  authorized?: boolean;
  // True if the account row holds a non-empty access_token.
  has_token?: boolean;
  // True if the Fyers account's trade switch is ON (`enabled`).
  enabled?: boolean;
  // True if creds are in .env but no OAuth has run yet (or token expired).
  credentials_set: boolean;
  // True if a Fyers broker account row exists in the DB (regardless of token).
  account_present: boolean;
  // The configured app ID (for display), if known.
  app_id: string | null;
  // The configured OAuth Redirect URI (FYERS_REDIRECT_URI). Shown in
  // the Fyers App Activation card so the operator can copy the exact
  // value into the Fyers dashboard.
  redirect_uri: string | null;
  // True if TRADING_MODE is currently 'live'.
  live_mode: boolean;
  // Reason string when not connected.
  reason: string | null;
}

// Risk-engine snapshot (kill switch, breaker halts, today's entry
// budget). Powers the WorkflowBar RISK chip.
export interface RiskStateSnapshot {
  trading_disabled: boolean;
  disabled_reason: string | null;
  halted_until: string | null;
  current_risk_pct: number | null;
  equity: number;
  trades_today: number;
  consecutive_losers: number;
  entries_allowed: boolean;
  entry_block_reason: string | null;
}

export function useRiskState() {
  return useQuery<RiskStateSnapshot>({
    queryKey: ["risk-state"],
    queryFn: () => api.get("/api/risk/state"),
    refetchInterval: 15000,
    refetchIntervalInBackground: false,
    retry: false,
  });
}

// Polls the Fyers connection status. Used by the "Connect Fyers"
// banner and the dashboard's status bar.
export function useFyersStatus() {
  return useQuery<FyersStatus>({
    queryKey: ["fyers-status"],
    queryFn: () => api.get("/api/fyers/status"),
    refetchInterval: 5000,
    refetchIntervalInBackground: false,
  });
}

// Fetches the OAuth URL and returns it. The caller (UI) opens a
// popup window with this URL. The popup will redirect to
// /api/fyers/callback after the user authorises the app.
export function useFyersAuthorizeUrl() {
  return useMutation<{ url: string; configured: boolean; reason: string | null }, void>({
    mutationFn: () => api.get("/api/fyers/authorize-url"),
  });
}

// Server identity (public IP, etc.) used by the Trade page to
// show a copyable IP hint when Fyers rejects with the IP-
// whitelist error. Cached server-side for 5 min.
export interface ServerInfo {
  public_ip: string | null;
  ip_source: string;
}
export function useServerInfo() {
  return useQuery<ServerInfo>({
    queryKey: ["server-info"],
    queryFn: () => api.get("/api/server-info"),
    // IP is stable across the session; a longer refetch is fine.
    refetchInterval: 5 * 60_000,
    refetchIntervalInBackground: false,
    staleTime: 60_000,
  });
}

// Disconnects Fyers by clearing the access token (and revoking the
// session server-side if possible). The creds in .env stay put.
export function useFyersDisconnect() {
  const qc = useQueryClient();
  return useMutation<{ ok: boolean; reason?: string }, void>({
    mutationFn: () => api.post("/api/fyers/disconnect", {}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["fyers-status"] });
      qc.invalidateQueries({ queryKey: ["broker-accounts"] });
      qc.invalidateQueries({ queryKey: ["market-indices"] });
    },
  });
}

// ---- Fyers / DeepSeek credentials (editable from the UI) ----

export interface FyersCredentials {
  fyers_app_id: string;
  fyers_redirect_uri: string;
  fyers_secret_set: boolean;
  fyers_secret_masked: string | null;
  deepseek_key_set: boolean;
  deepseek_key_masked: string | null;
}

export interface CredentialsUpdate {
  fyers_app_id?: string;
  fyers_secret_key?: string;
  fyers_redirect_uri?: string;
  deepseek_api_key?: string;
}

export function useFyersCredentials() {
  return useQuery<FyersCredentials>({
    queryKey: ["fyers-credentials"],
    queryFn: () => api.get("/api/settings/credentials"),
  });
}

export function useUpdateCredentials() {
  const qc = useQueryClient();
  return useMutation<FyersCredentials, Error, CredentialsUpdate>({
    mutationFn: (body) => api.put("/api/settings/credentials", body),
    onSuccess: () => {
      // Creds change ripples through status, settings, and the postback
      // config (the secret is shared) — refresh all of them.
      qc.invalidateQueries({ queryKey: ["fyers-credentials"] });
      qc.invalidateQueries({ queryKey: ["fyers-status"] });
      qc.invalidateQueries({ queryKey: ["settings"] });
    },
  });
}

// ---- Fyers order postback (webhook) config ----

export interface FyersPostbackConfig {
  public_base_url: string;
  secret: string | null;
  path: string;
  url: string | null;
  preference: string;
  configured: boolean;
  secret_present: boolean;
}

export function useFyersPostbackConfig() {
  return useQuery<FyersPostbackConfig>({
    queryKey: ["fyers-postback-config"],
    queryFn: () => api.get("/api/fyers/postback/config"),
  });
}

export function useUpdateFyersPostbackConfig() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { public_base_url?: string; regenerate_secret?: boolean }) =>
      api.post<FyersPostbackConfig>("/api/fyers/postback/config", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["fyers-postback-config"] }),
  });
}

// ---- Notifications (T6) ----

export function useNotificationChannels() {
  return useQuery<NotificationChannel[]>({
    queryKey: ["notification-channels"],
    queryFn: async () => {
      const r = await api.get<{ channels: NotificationChannel[] }>("/api/notifications/channels");
      return r.channels;
    },
  });
}

export function useCreateNotificationChannel() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Partial<NotificationChannel>) =>
      api.post<NotificationChannel>("/api/notifications/channels", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["notification-channels"] }),
  });
}

export function useUpdateNotificationChannel(id: number | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Partial<NotificationChannel>) =>
      api.put<NotificationChannel>(`/api/notifications/channels/${id}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["notification-channels"] }),
  });
}

export function useDeleteNotificationChannel() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.delete(`/api/notifications/channels/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["notification-channels"] }),
  });
}

export function useTestNotificationChannel() {
  return useMutation({
    mutationFn: (id: number) =>
      api.post<{ ok: boolean; error?: string }>(`/api/notifications/channels/${id}/test`, {}),
  });
}

// ---- Webhooks (T6) ----

export function useWebhooks() {
  return useQuery<Webhook[]>({
    queryKey: ["webhooks"],
    queryFn: async () => {
      const r = await api.get<{ webhooks: Webhook[] }>("/api/webhooks");
      return r.webhooks;
    },
  });
}

export function useCreateWebhook() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Partial<Webhook>) => api.post<Webhook>("/api/webhooks", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["webhooks"] }),
  });
}

export function useUpdateWebhook(id: number | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Partial<Webhook>) => api.put<Webhook>(`/api/webhooks/${id}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["webhooks"] }),
  });
}

export function useDeleteWebhook() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.delete(`/api/webhooks/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["webhooks"] }),
  });
}

// ---- Backtest (T7) ----

export function useBacktestRuns() {
  return useQuery<BacktestRun[]>({
    queryKey: ["backtest-runs"],
    queryFn: () => api.get("/api/backtest/runs"),
    refetchInterval: 5000,
  });
}

export function useBacktestRun(id: number | null) {
  return useQuery<BacktestRun>({
    queryKey: ["backtest-run", id],
    queryFn: () => api.get(`/api/backtest/runs/${id}`),
    enabled: id !== null,
    refetchInterval: (q) =>
      q.state.data && ["pending", "running"].includes(q.state.data.status) ? 2000 : false,
  });
}

export function useCreateBacktestRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: {
      name?: string;
      strategy_id?: number;
      broker_account_id?: number;
      start_date: string;
      end_date: string;
      initial_capital: number;
    }) => api.post<BacktestRun>("/api/backtest/runs", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["backtest-runs"] }),
  });
}

export function useDeleteBacktestRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.delete(`/api/backtest/runs/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["backtest-runs"] }),
  });
}

export function useBacktestEquityCurve(id: number | null) {
  return useQuery<{ curve: Array<{ date: string; equity: number }> }>({
    queryKey: ["backtest-equity", id],
    queryFn: () => api.get(`/api/backtest/runs/${id}/equity-curve`),
    enabled: id !== null,
  });
}

export function useBacktestTrades(id: number | null) {
  return useQuery<{ trades: Array<Record<string, unknown>> }>({
    queryKey: ["backtest-trades", id],
    queryFn: () => api.get(`/api/backtest/runs/${id}/trades`),
    enabled: id !== null,
  });
}

// ---- Settings ----

export function useGlobalSettings() {
  return useQuery<{ global: GlobalSettings }>({
    queryKey: ["settings"],
    queryFn: () => api.get("/api/settings"),
  });
}

// The full settings surface, grouped, straight from the backend registry.
// Nothing here names a setting: adding a knob in config.py makes it appear
// on the All Settings card with no frontend change.
export function useSettingsSchema() {
  return useQuery<SettingsSchema>({
    queryKey: ["settings", "schema"],
    queryFn: () => api.get("/api/settings/schema"),
  });
}

// Drop one override so the key falls back to the config.py default.
export function useResetSetting() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (key: string) => api.delete(`/api/settings/overrides/${key}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["settings"] }),
  });
}

// Reset a whole group, or everything when `group` is omitted.
export function useResetSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { group?: string; confirm: boolean }) =>
      api.post("/api/settings/reset", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["settings"] }),
  });
}

export function useImportSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { overrides: Record<string, unknown>; replace?: boolean }) =>
      api.post<{ imported: string[]; ignored: string[]; reset: string[] }>(
        "/api/settings/import",
        body,
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["settings"] }),
  });
}

export function useUpdateSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { global: Partial<GlobalSettings> }) =>
      api.put<{ global: GlobalSettings }>("/api/settings", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["settings"] }),
  });
}

// ---- Audit log ----

export function useAuditLog(params?: { action?: string; target?: string; limit?: number }) {
  const qs = new URLSearchParams();
  if (params?.action) qs.set("action", params.action);
  if (params?.target) qs.set("target", params.target);
  if (params?.limit) qs.set("limit", String(params.limit));
  const url = `/api/audit-log${qs.toString() ? `?${qs.toString()}` : ""}`;
  return useQuery<{ entries: AuditLogEntry[]; count: number }>({
    queryKey: ["audit-log", params],
    queryFn: () => api.get(url),
    refetchInterval: 10000,
  });
}

// ---- Trade page (manual orders) ----

export function useSearchSymbols(query: string, segment?: string) {
  const trimmed = query.trim();
  return useQuery<SearchResponse>({
    queryKey: ["search-symbols", trimmed, segment],
    queryFn: () => {
      const qs = new URLSearchParams();
      qs.set("q", trimmed);
      if (segment) qs.set("segment", segment);
      qs.set("limit", "20");
      return api.get<SearchResponse>(`/api/search/symbols?${qs.toString()}`);
    },
    enabled: trimmed.length >= 1,
    staleTime: 30_000,
  });
}

// Downloads the full NSE/BSE cash scrip master from Fyers and reloads the
// backend's instrument index, so every listed stock becomes searchable.
export function useRefreshInstruments() {
  const qc = useQueryClient();
  return useMutation<
    { ok: boolean; instrument_count: number; files: Record<string, unknown> },
    Error,
    void
  >({
    mutationFn: () => api.post("/api/search/refresh", {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["search-symbols"] }),
  });
}

export function useOptionChain(
  symbol: string,
  underlying: string,
  expiry?: string | null,
  strikecount = 12,
) {
  return useQuery<OptionChainResponse>({
    queryKey: ["option-chain", symbol, underlying, expiry, strikecount],
    queryFn: () => {
      const qs = new URLSearchParams();
      // `symbol` (e.g. NSE:RELIANCE-EQ or NSE:NIFTY50-INDEX) is the
      // authoritative underlying for Fyers; `underlying` (short name) is
      // the master-fallback key. Pass both.
      if (symbol) qs.set("symbol", symbol);
      if (underlying) qs.set("underlying", underlying);
      if (expiry) qs.set("expiry", expiry);
      qs.set("strikecount", String(strikecount));
      return api.get<OptionChainResponse>(`/api/options/chain?${qs.toString()}`);
    },
    enabled: symbol.length >= 1 || underlying.length >= 1,
    // Poll live prices only while there's an actual chain (F&O symbols);
    // a non-F&O stock returns no strikes, so stop polling after the first.
    refetchInterval: (q) =>
      q.state.data && q.state.data.strikes.length > 0 ? 6000 : false,
    staleTime: 4000,
  });
}

export function useQuote(symbol: string) {
  return useQuery<QuoteResponse>({
    queryKey: ["order-quote", symbol],
    queryFn: () => api.get<QuoteResponse>(`/api/orders/quote?symbol=${encodeURIComponent(symbol)}`),
    enabled: symbol.length >= 1,
    refetchInterval: 3000, // tight polling so the ticket price stays live
  });
}

export function usePendingOrders(accountId?: number | null) {
  return useQuery<PendingOrdersResponse>({
    queryKey: ["pending-orders", accountId],
    queryFn: () => {
      const qs = new URLSearchParams();
      if (accountId) qs.set("account_id", String(accountId));
      qs.set("limit", "50");
      return api.get<PendingOrdersResponse>(`/api/orders/pending?${qs.toString()}`);
    },
    refetchInterval: 5000,
  });
}

export function usePlaceOrder() {
  const qc = useQueryClient();
  return useMutation<PlaceOrderResponse, Error, PlaceOrderRequest>({
    mutationFn: (body) => api.post<PlaceOrderResponse>("/api/orders", body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["pending-orders"] });
      qc.invalidateQueries({ queryKey: ["trades"] });
    },
  });
}

export interface CancelOrderResponse {
  ok: boolean;
  broker_order_id: string;
  /** Optional human-readable reason for the cancel outcome
   *  (e.g. "cancelled", "broker_rejected_already_gone"). */
  reason?: string;
}

export function useCancelOrder() {
  const qc = useQueryClient();
  return useMutation<
    CancelOrderResponse,
    Error,
    { account_id: number; broker_order_id: string }
  >({
    mutationFn: (body) =>
      api.post<CancelOrderResponse>("/api/orders/cancel", body),
    onSuccess: () => {
      // Invalidate everything that might show a stale view of the
      // order we just cancelled: the pending list, the full trade
      // history (status went to "cancelled"), and the open-positions
      // panel (the cancel might have been a hedge against a position
      // that was about to flip, or the broker might have already
      // filled it server-side).
      qc.invalidateQueries({ queryKey: ["pending-orders"] });
      qc.invalidateQueries({ queryKey: ["trades"] });
      qc.invalidateQueries({ queryKey: ["positions"] });
    },
  });
}

// ---- Observability: pipeline latency + outcomes + breaker history ----

export interface LatencyStats {
  count: number;
  p50: number | null;
  p95: number | null;
  p99: number | null;
  min: number | null;
  max: number | null;
}

export interface PipelineLatency {
  window: number;
  counts: { total: number; fast_track: number; llm: number };
  fast_track_pct: number | null;
  latency: {
    pdf_fetch_ms: LatencyStats;
    pdf_extract_ms: LatencyStats;
    llm_ms: LatencyStats;
    total_ms: LatencyStats;
  };
  news_age_s_at_start: LatencyStats & {
    histogram: { le: number | null; count: number }[];
  };
  deadline: {
    blocked: number;
    total_signals: number;
    hit_rate_pct: number | null;
  };
  series: {
    analysis_id: number;
    created_at: string | null;
    total_ms: number | null;
    llm_ms: number | null;
    track: "fast" | "llm";
  }[];
}

export function usePipelineLatency(window = 100) {
  return useQuery<PipelineLatency>({
    queryKey: ["pipeline-latency", window],
    queryFn: () => api.get(`/api/metrics/pipeline-latency?window=${window}`),
    refetchInterval: 15000,
  });
}

export interface ExecutionLatencyRow {
  trade_id: number;
  symbol: string;
  side: string;
  executed_at: string | null;
  detection_ms: number | null;
  analysis_ms: number | null;
  signal_to_order_ms: number | null;
  order_to_fill_ms: number | null;
}

export interface MonitorTickStats {
  last_tick_ms: number;
  avg_tick_ms: number;
  ticks_sampled: number;
  last_gap_s: number | null;
  updated_at: string;
}

export interface ExecutionLatency {
  window: number;
  stages: {
    detection_ms: LatencyStats;
    analysis_ms: LatencyStats;
    signal_to_order_ms: LatencyStats;
    order_to_fill_ms: LatencyStats;
  };
  detection_by_exchange: Record<string, LatencyStats>;
  detection_by_source: Record<string, LatencyStats>;
  detection_samples: number;
  monitor_ticks: Record<string, MonitorTickStats>;
  series: ExecutionLatencyRow[];
}

export function useExecutionLatency(window = 100) {
  return useQuery<ExecutionLatency>({
    queryKey: ["execution-latency", window],
    queryFn: () => api.get(`/api/metrics/execution-latency?window=${window}`),
    refetchInterval: 15000,
  });
}

export interface EdgeStats {
  basis: "symbol" | "action";
  symbol: string | null;
  action: string;
  n: number;
  win_rate: number;
  avg_move_5m: number;
  expectancy: number;
  best: number;
  worst: number;
}

export interface EdgeBook {
  book: { BUY: EdgeStats | null; SELL: EdgeStats | null };
}

export function useEdgeBook() {
  return useQuery<EdgeBook>({
    queryKey: ["edge-book"],
    queryFn: () => api.get("/api/metrics/edge/book"),
    refetchInterval: 30000,
  });
}

export interface OutcomeRow {
  outcome_id: number;
  signal_id: number | null;
  symbol: string;
  action: string;
  event_type: string | null;
  headline: string | null;
  sentiment: string | null;
  sentiment_score: number | null;
  confidence: number | null;
  price_at_signal: number | null;
  price_5m: number | null;
  price_30m: number | null;
  move_5m_pct: number | null;
  move_30m_pct: number | null;
  ai_was_correct: boolean | null;
  trade_taken: boolean;
  signal_status: string | null;
  note: string | null;
  created_at: string | null;
}

export interface OutcomesSummary {
  total: number;
  by_event_type: {
    event_type: string;
    samples: number;
    with_5m_data: number;
    avg_move_5m_pct: number | null;
    avg_move_30m_pct: number | null;
    accuracy_5m_pct: number | null;
    taken: number;
    blocked: number;
  }[];
}

export function useOutcomes(limit = 200, eventType?: string) {
  const qs = eventType
    ? `?limit=${limit}&event_type=${encodeURIComponent(eventType)}`
    : `?limit=${limit}`;
  return useQuery<OutcomeRow[]>({
    queryKey: ["outcomes", limit, eventType ?? null],
    queryFn: () => api.get(`/api/outcomes${qs}`),
    refetchInterval: 30000,
  });
}

export function useOutcomesSummary(limit = 1000) {
  return useQuery<OutcomesSummary>({
    queryKey: ["outcomes-summary", limit],
    queryFn: () => api.get(`/api/outcomes/summary?limit=${limit}`),
    refetchInterval: 30000,
  });
}

// ---- Dataset (the ML training-set builder) ----

export interface DatasetColumn {
  key: string;
  label: string;
  category: string;
  role: "meta" | "feature" | "target";
  description: string;
  source: string;
}

export interface DatasetColumns {
  columns: DatasetColumn[];
  presets: Record<string, string[]>;
}

export interface DatasetStats {
  total_rows: number;
  sources: { signal: number; shadow: number };
  enrichment: Record<string, number>;
  label_balance: Record<string, number>;
  by_event_type: { event_type: string; samples: number }[];
  first_row_at: string | null;
  last_row_at: string | null;
  column_count: number;
}

export interface DatasetRowsResponse {
  total: number;
  count: number;
  rows: Record<string, unknown>[];
}

export interface DatasetFilters {
  event_type?: string;
  action?: string;
  symbol?: string;
  taken?: string;
  label?: string;
  enriched_only?: boolean;
  source?: string; // all | signal | shadow
  since?: string;
  until?: string;
}

export function datasetQueryString(
  filters: DatasetFilters,
  columns?: string[],
  extra?: Record<string, string | number>
): string {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) {
    if (v !== undefined && v !== "" && v !== false) params.set(k, String(v));
  }
  if (columns && columns.length > 0) params.set("columns", columns.join(","));
  for (const [k, v] of Object.entries(extra ?? {})) params.set(k, String(v));
  return params.toString();
}

// `source` picks which dataset the page is showing: the per-SIGNAL training set
// or the merged per-ANNOUNCEMENT dataset. They are different grains with
// different catalogs, so it is part of every dataset query key.
export type DatasetSource = "signals" | "announcements";

export function useDatasetColumns(source: DatasetSource = "signals") {
  return useQuery<DatasetColumns>({
    queryKey: ["dataset-columns", source],
    queryFn: () => api.get(`/api/dataset/columns?source=${source}`),
    staleTime: Infinity, // the catalog is static per build
  });
}

export function useDatasetStats(source: DatasetSource = "signals") {
  return useQuery<DatasetStats>({
    queryKey: ["dataset-stats", source],
    queryFn: () => api.get(`/api/dataset/stats?source=${source}`),
    refetchInterval: 30000,
  });
}

export function useDatasetRows(
  filters: DatasetFilters,
  columns: string[] | undefined,
  limit = 50
) {
  const qs = datasetQueryString(filters, columns, { limit });
  return useQuery<DatasetRowsResponse>({
    queryKey: ["dataset-rows", qs],
    queryFn: () => api.get(`/api/dataset?${qs}`),
    refetchInterval: 30000,
  });
}

export interface DatasetBackfillResult {
  ok: boolean;
  full: boolean;
  processed: number;
  shadow: number;
  complete: number;
  partial: number;
  no_candles: number;
  after_hours: number;
  too_old: number;
  errors: number;
}

export interface DatasetBackfillProgress {
  running: boolean;
  batches?: number;
  processed?: number;
  shadow?: number;
  complete?: number;
  partial?: number;
  no_candles?: number;
  after_hours?: number;
  too_old?: number;
  errors?: number;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface DatasetCollector {
  running: boolean;
  last_run_at: string | null;
  next_run_at: string | null;
  interval_s: number | null;
  runs: number;
  enriched_session: number;
  last_counts: Record<string, number | boolean>;
}

export interface DatasetBackfillStatus {
  progress: DatasetBackfillProgress;
  remaining: { signal: number; shadow: number };
  collector?: DatasetCollector;
}

export function useDatasetBackfillStatus() {
  return useQuery<DatasetBackfillStatus>({
    queryKey: ["dataset-backfill-status"],
    queryFn: () => api.get("/api/dataset/backfill/status"),
    // Poll while the full backfill runs, lazily otherwise. Even the
    // running poll stays modest — the bot is busy enriching.
    refetchInterval: (query) =>
      query.state.data?.progress?.running ? 5000 : 30000,
  });
}

export interface DatasetHealthColumn {
  key: string;
  role: "meta" | "feature" | "target";
  category: string;
  null_rate: number | null;
  numeric: boolean;
  mean?: number;
  std?: number;
  min?: number;
  max?: number;
  corr_target?: number | null;
  corr_n?: number;
  leak_warning?: boolean;
  distinct?: number;
}

export interface DatasetHealth {
  target: string;
  rows_sampled: number;
  columns: DatasetHealthColumn[];
}

export function useDatasetHealth(target: string, enabled: boolean) {
  return useQuery<DatasetHealth>({
    queryKey: ["dataset-health", target],
    queryFn: () =>
      api.get(`/api/dataset/health?target=${encodeURIComponent(target)}`),
    enabled,
    refetchInterval: 60000,
  });
}

// ---- HOLD calibration: are we passing on movers? ----------------------

export interface CalibrationBucket {
  key: string;
  n: number;
  movers: number;
  mover_rate: number;
  big_movers: number;
  big_mover_rate: number;
  avg_abs_excursion: number | null;
  median_abs_excursion: number | null;
  avg_ret_15m: number | null;
  up: number;
  down: number;
  flat: number;
}

export interface HoldCalibration {
  thresholds: { move_pct: number; big_pct: number };
  sample: { total_complete: number; hold: number; taken: number; declined: number };
  hold_overall: CalibrationBucket;
  by_event_type: CalibrationBucket[];
  by_confidence: CalibrationBucket[];
  by_sentiment: CalibrationBucket[];
  comparison: { taken: CalibrationBucket; declined: CalibrationBucket };
  top_missed: {
    symbol: string | null;
    event_type: string | null;
    confidence: number | null;
    sentiment: string | null;
    abs_excursion: number;
    ret_15m_pct: number | null;
    label_15m: string | null;
  }[];
  verdict: string;
  rows_analysed: number;
}

export function useDatasetCalibration(
  movePct: number,
  enabled: boolean,
) {
  return useQuery<HoldCalibration>({
    queryKey: ["dataset-calibration", movePct],
    queryFn: () =>
      api.get(`/api/dataset/calibration?move_threshold_pct=${movePct}`),
    enabled,
    refetchInterval: 120000,
  });
}

export function useDatasetBackfill() {
  const qc = useQueryClient();
  return useMutation<
    DatasetBackfillResult,
    Error,
    { limit?: number; full?: boolean; retryFailed?: boolean; rebuild?: boolean } | undefined
  >({
    mutationFn: (opts) =>
      api.post(
        `/api/dataset/backfill?limit=${opts?.limit ?? 100}${
          opts?.full ? "&full=true" : ""
        }${opts?.retryFailed ? "&retry_failed=true" : ""}${
          opts?.rebuild ? "&rebuild=true" : ""
        }`,
        {}
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dataset-stats"] });
      qc.invalidateQueries({ queryKey: ["dataset-rows"] });
      qc.invalidateQueries({ queryKey: ["dataset-backfill-status"] });
    },
  });
}

export interface BreakerEvent {
  id: number;
  event_type: string;
  severity: string;
  message: string;
  threshold_value: number | null;
  current_value: number | null;
  action_taken: string | null;
  halted: boolean;
  created_at: string | null;
}

export function useBreakerHistory(limit = 100) {
  return useQuery<BreakerEvent[]>({
    queryKey: ["breaker-history", limit],
    queryFn: () => api.get(`/api/risk/breaker-history?limit=${limit}`),
    refetchInterval: 30000,
  });
}
