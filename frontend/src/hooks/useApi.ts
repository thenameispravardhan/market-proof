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
  Position,
  PromptHistoryEntry,
  PromptTemplate,
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

export function usePositions() {
  return useQuery<Position[]>({
    queryKey: ["positions"],
    queryFn: () => api.get("/api/positions"),
    refetchInterval: 5000,
  });
}

export function useTrades(limit = 200) {
  return useQuery<Trade[]>({
    queryKey: ["trades", limit],
    queryFn: () => api.get(`/api/trades?limit=${limit}`),
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
