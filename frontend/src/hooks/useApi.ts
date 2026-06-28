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

export function useCreateBrokerAccount() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Partial<BrokerAccount>) =>
      api.post<BrokerAccount>("/api/broker-accounts", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["broker-accounts"] }),
  });
}

export function useUpdateBrokerAccount(id: number | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Partial<BrokerAccount>) =>
      api.put<BrokerAccount>(`/api/broker-accounts/${id}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["broker-accounts"] }),
  });
}

export function useDeleteBrokerAccount() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.delete(`/api/broker-accounts/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["broker-accounts"] }),
  });
}

export function useTestBrokerAccount() {
  return useMutation({
    mutationFn: (id: number) =>
      api.post<{ ok: boolean; message: string }>(`/api/broker-accounts/${id}/test`, {}),
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
    onSuccess: () => qc.invalidateQueries({ queryKey: ["broker-accounts"] }),
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

export function useUpdateSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { global: Partial<GlobalSettings> }) =>
      api.put<{ global: GlobalSettings }>("/api/settings", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["settings"] }),
  });
}

export function useSetTradingMode() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { mode: "paper" | "live"; confirm: boolean }) =>
      api.post("/api/settings/trading-mode", body),
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
