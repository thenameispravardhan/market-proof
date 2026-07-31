// TypeScript shapes mirroring the backend DB models. The backend
// exposes /api/* — these types are the contract the UI expects.

export type Sentiment = "positive" | "neutral" | "negative";
export type Action = "BUY" | "SELL" | "HOLD" | "BLOCK";
export type Side = "BUY" | "SELL";

export interface Announcement {
  id: number;
  symbol: string;
  exchange: "BSE" | "NSE";
  event_type: string;
  headline: string;
  body?: string | null;
  pdf_url?: string | null;
  source?: string | null;
  filed_at?: string | null;
  received_at: string;
}

export interface Analysis {
  id: number;
  announcement_id: number;
  model: string;
  sentiment: Sentiment | null;
  sentiment_score: number | null;
  confidence: number | null;
  recommendation: Action | null;
  rationale: string | null;
  deal_value_inr_crore: number | null;
  stake_change_pct: number | null;
  dividend_per_share: number | null;
  buyback_value_inr_crore: number | null;
  created_at: string;
}

export interface Signal {
  id: number;
  analysis_id: number | null;
  strategy_id: number | null;
  rule_id: number | null;
  symbol: string;
  action: Action;
  confidence: number | null;
  position_size_pct: number | null;
  rationale: string | null;
  status: "pending" | "placed" | "filled" | "rejected" | "blocked";
  created_at: string;
}

export interface Position {
  id: number;
  symbol: string;
  quantity: number;
  average_price: number;
  last_price: number | null;
  unrealized_pnl: number | null;
  strategy_id: number | null;
  opened_at: string;
  updated_at: string;
}

export interface Trade {
  id: number;
  signal_id: number | null;
  broker_account_id: number | null;
  symbol: string;
  side: Side;
  quantity: number;
  price: number;
  order_type: string;
  status: string;
  broker_order_id: string | null;
  pnl: number | null;
  executed_at: string | null;
  created_at: string;
}

// DeepSeek v4 reasoning depth — only used when thinking is enabled.
export type ReasoningEffort = "low" | "medium" | "high";

export interface PromptTemplate {
  id: number;
  event_type: string;
  system_prompt: string;
  user_template: string;
  model: string;
  temperature: number;
  max_tokens: number;
  // v4 inference controls (see PromptEditor).
  reasoning_effort: ReasoningEffort;
  thinking_enabled: boolean;
  stream: boolean;
  version: number;
  updated_at: string;
  updated_by: string | null;
}

export interface PromptHistoryEntry {
  id: number;
  template_id: number;
  version: number;
  system_prompt: string;
  user_template: string;
  model: string;
  temperature: number;
  max_tokens: number;
  reasoning_effort: ReasoningEffort;
  thinking_enabled: boolean;
  stream: boolean;
  changed_at: string;
  change_note: string | null;
}

export interface DashboardSummary {
  open_positions: number;
  hard_rules_count: number;
  todays_realized_pnl: number;
  todays_unrealized_pnl: number;
  pnl_series: { date: string; realized: number; cumulative: number }[];
}

export interface ApiError {
  status: number;
  message: string;
  detail?: unknown;
}

export interface AuditLogEntry {
  id: number;
  actor: string;
  action: string;
  target: string | null;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  created_at: string;
}

// ---- T5 types ----

export interface Strategy {
  id: number;
  name: string;
  description: string | null;
  enabled: boolean;
  config: Record<string, unknown> | null;
  created_at: string;
}

export interface SignalCondition {
  field: string;
  op: string;
  value: unknown;
}

export interface SignalConditions {
  all_of?: SignalCondition[];
  any_of?: SignalCondition[];
}

export interface SignalRule {
  id: number;
  strategy_id: number;
  name: string;
  priority: number;
  conditions: SignalConditions;
  action: Action;
  action_params: Record<string, unknown> | null;
  enabled: boolean;
  created_at: string;
}

export interface BrokerAccount {
  id: number;
  name: string;
  broker: string;
  app_id: string | null;
  secret_key: string | null; // always "***" in responses
  access_token: string | null; // always "***" in responses
  redirect_uri: string | null;
  paper_mode: boolean;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

// ---- T6 types ----

export interface NotificationChannel {
  id: number;
  name: string;
  kind: "telegram" | "discord" | "email" | "webhook";
  config: Record<string, unknown>;
  events_filter: string | null;
  enabled: boolean;
  created_at: string;
}

export interface NotificationLog {
  id: number;
  channel_id: number;
  event_type: string;
  payload: Record<string, unknown> | null;
  status: "sent" | "failed" | "skipped";
  error: string | null;
  sent_at: string;
}

export interface Webhook {
  id: number;
  name: string;
  direction: "in" | "out";
  event_filter: string | null;
  url: string;
  secret: string | null; // "***" when set
  enabled: boolean;
  created_at: string;
}

export interface WebhookDelivery {
  id: number;
  webhook_id: number;
  direction: string;
  event_type: string | null;
  payload: Record<string, unknown> | null;
  status_code: number | null;
  response_body: string | null;
  error: string | null;
  attempted_at: string;
}

// ---- T7 types ----

export interface BacktestRun {
  id: number;
  name: string | null;
  strategy_id: number | null;
  broker_account_id: number | null;
  start_date: string;
  end_date: string;
  initial_capital: number;
  status: "pending" | "running" | "done" | "failed";
  created_at: string;
  finished_at: string | null;
  config?: Record<string, unknown> | null;
  metrics?: Record<string, unknown> | null;
  announcements_processed?: number | null;
  signals_generated?: number | null;
  blocked_trades?: number | null;
}

export interface BacktestTrade {
  symbol: string;
  side: string;
  quantity: number;
  price: number;
  pnl: number;
  executed_at: string;
}

export interface EquityPoint {
  date: string;
  equity: number;
}

// ---- Settings ----

// ---- Self-describing settings schema (GET /api/settings/schema) ----------
// Mirrors app/api/settings_schema.py. The All Settings card renders these
// generically, so a knob added to config.py needs no change on this side.
export interface SettingsField {
  key: string;
  type: "bool" | "int" | "float" | "str";
  default: unknown;
  value: unknown;
  label: string;
  widget: "number" | "toggle" | "time" | "text" | "select";
  min: number | null;
  max: number | null;
  step: number | null;
  choices: string[] | null;
  group: string;
  read_only: boolean;
  restart_required: boolean;
  // True when an explicit override row exists — NOT merely "differs from
  // default". A key pinned to its own default is still overridden, and still
  // blocks a future default change.
  overridden: boolean;
}

export interface SettingsGroup {
  id: string;
  title: string;
  note: string;
  fields: SettingsField[];
}

export interface SettingsSchema {
  groups: SettingsGroup[];
  overridden_count: number;
  version: string;
}

export interface SettingsExport {
  version: string;
  exported_at: string;
  overrides: Record<string, unknown>;
}

export interface GlobalSettings {
  TRADING_MODE: "paper" | "live";
  MAX_CAPITAL_RISK_PCT: number;
  DAILY_MAX_LOSS_PCT: number;
  MAX_CONCURRENT_POSITIONS: number;
  MAX_SINGLE_POSITION_PCT: number;
  MIN_LIQUIDITY_CRORE: number;
  MAX_SIGNALS_PER_DAY: number;
  POLL_INTERVAL_SECONDS: number;
  PORTFOLIO_VALUE: number;
  DEFAULT_SL_PCT: number;
  DEFAULT_TARGET_RR: number;
  QUOTE_REFRESH_SECONDS: number;
  // ---- News sources (multi-channel detection racing) --------------------
  // Each source is an independent monitor; toggles apply live within one
  // poll interval. Detection speed = fastest enabled source.
  NSE_API_ENABLED: boolean;
  BSE_API_ENABLED: boolean;
  NSE_RSS_ENABLED: boolean;
  NSE_RSS_POLL_SECONDS: number;
  // ---- Edge Memory (self-learning conviction gate) ----------------------
  EDGE_GATE_ENABLED: boolean;
  EDGE_GATE_MIN_SAMPLES: number;
  EDGE_GATE_MIN_EXPECTANCY_PCT: number;
  // ---- Exit Manager (Exits page) ----------------------------------------
  // Every exit-engine knob. Defaults mirror the backend; the Exits page is
  // the intended editor. See app/execution/trade_manager.py for behavior.
  ENTRY_MAX_DRIFT_PCT: number;
  DEFAULT_SL_MIN_PCT: number;
  DEFAULT_SL_SMALLCAP_PCT: number;
  ATR_ENABLED: boolean;
  ATR_PERIOD: number;
  ATR_STOP_MULT: number;
  ATR_MAX_STOP_PCT: number;
  BREAKEVEN_ENABLED: boolean;
  BREAKEVEN_AT_PCT: number;
  BREAKEVEN_LOCK_PCT: number;
  SCALE_OUT_ENABLED: boolean;
  SCALE_OUT_R: number;
  TRAIL_ACTIVATE_R: number;
  TRAIL_DISTANCE_R: number;
  CONSOLIDATION_EXIT_ENABLED: boolean;
  CONSOLIDATION_WINDOW_SECONDS: number;
  CONSOLIDATION_RANGE_PCT: number;
  CONSOLIDATION_MIN_PROFIT_PCT: number;
  CONSOLIDATION_MAX_PROFIT_PCT: number;
  STALL_EXIT_ENABLED: boolean;
  STALL_WINDOW_SECONDS: number;
  STALL_ROC_PCT: number;
  STALL_MIN_PROFIT_PCT: number;
  STALL_MAX_PROFIT_PCT: number;
  MAX_HOLD_SECONDS: number;
  SQUARE_OFF_TIME_IST: string;
  // When ON, clearly-administrative filings are skipped before the LLM
  // call. OFF sends every filing to the AI (more cost, slower queue).
  PRE_LLM_FILTER_ENABLED: boolean;
  // Master switch for AI news analysis (Dashboard toggle). OFF = news is
  // still collected but never sent to the LLM — no signals / auto trades.
  AI_ANALYSIS_ENABLED: boolean;
  // Extracted-text mode. OFF (default) = legacy behavior: the AI gets the
  // PDF URL + headline metadata only. ON = the filing PDF is downloaded,
  // the relevant pages are extracted (Hindi half dropped) and sent as
  // real text. Extraction failures always fall back to the legacy path.
  SEND_EXTRACTED_TEXT: boolean;
  // Deterministic fast track. OFF (default) = every filing goes to the AI.
  // ON = unambiguous high-conviction headlines (order win / buyback with
  // explicit ₹-crore value, KMP resignation) skip the AI and hit the rules
  // engine in milliseconds. Non-matches still go to the AI.
  FAST_TRACK_ENABLED: boolean;
  // Cap on AI completion tokens — shorter output generates faster; a full
  // signal JSON measured ~165 tokens, so keep at least ~2× headroom.
  LLM_MAX_TOKENS: number;
  // Staleness gate: filings older than this (seconds) skip the AI entirely.
  MAX_NEWS_AGE_SECONDS: number;
  // WHICH clock the staleness gate measures. OFF (default) = time since the
  // exchange's stated filing time, which includes the exchange's OWN publish
  // lag (measured median ~35s) — so filings get dropped for a delay the bot
  // didn't cause. ON = time since the bot first SAW the filing; alpha decays
  // from publication, not submission.
  NEWS_AGE_FROM_RECEIPT: boolean;
  // Absolute age ceiling (seconds since filing), used only when
  // NEWS_AGE_FROM_RECEIPT is on — stops a monitor outage from making a whole
  // stale backlog look "just received". 0 = no ceiling.
  MAX_NEWS_AGE_ABSOLUTE_SECONDS: number;
  // Hard end-to-end deadline (seconds from filing to signal). Late signals
  // are stored but blocked. 0 = disabled.
  PIPELINE_DEADLINE_SECONDS: number;
  // Passive price tracking (+5m/+30m) for every signal into
  // signal_outcomes — the win-rate report and future ML dataset.
  OUTCOME_LOGGER_ENABLED: boolean;
  // Intraday buying-power multiplier (Fyers MIS ~5x). Notional caps only —
  // risk-per-trade and loss limits always stay on real equity.
  INTRADAY_LEVERAGE: number;
}

export interface ManagedPosition {
  symbol: string;
  quantity: number;
  entry: number;
  stop_loss: number | null;
  target: number | null;
  signal_id: number | null;
  strategy_id: number | null;
  opened_at: string;
}

// ---- Trade page ----

export type OrderType = "MARKET" | "LIMIT" | "STOP_LOSS" | "SL-M";
// Intraday-only bot — the backend 422s anything else, so there is one value.
export type ProductType = "INTRADAY";

export interface InstrumentHit {
  symbol: string;          // "NSE:SBIN-EQ" or "NSE:NIFTY2561424500CE"
  short_name: string;
  exchange: string;
  segment: string;         // "EQ" / "FO" / "COM" / "CD" / "INDEX"
  instrument_type: string; // "EQ" / "FUT" / "CE" / "PE" / "IND"
  lot_size: number;
  tick_size: number;
  expiry: string | null;   // YYYY-MM-DD
  strike: number | null;
  underlying: string | null;
  display: string;
}

export interface SearchResponse {
  ok: boolean;
  count: number;
  hits: InstrumentHit[];
}

export interface OptionLeg {
  symbol: string;
  ltp: number | null;
  bid: number | null;
  ask: number | null;
  oi: number | null;
  volume: number | null;
  ltpch: number | null;
  lot_size: number;
  tick_size: number;
}

export interface OptionExpiry {
  label: string; // human label, e.g. "16-06-2026"
  ts: string; // epoch string passed back as `expiry` to re-fetch
}

export interface OptionChainResponse {
  ok: boolean;
  underlying: string;
  symbol: string;
  spot: number | null;
  expiries: OptionExpiry[];
  selected_expiry: string | null;
  strikes: {
    strike: number;
    ce: OptionLeg | null;
    pe: OptionLeg | null;
  }[];
  // "fyers" (live prices) or "master" (static ladder, no prices).
  source: string;
  reason: string | null;
}

export interface PlaceOrderRequest {
  account_id: number;
  symbol: string;
  side: "BUY" | "SELL";
  quantity: number;
  order_type: OrderType;
  limit_price?: number | null;
  stop_price?: number | null;
  product_type: ProductType;
  bypass_risk?: boolean;
  operator?: string;
}

export interface PlaceOrderResponse {
  ok: boolean;
  blocked?: boolean;
  bypassed_risk?: boolean;
  risk_codes: string[];
  risk_message: string;
  risk_warning?: string | null;
  broker_order_id: string | null;
  status: string;
  error: string | null;
  // Diagnostic marker from the Fyers backend (e.g. "ip_whitelist"
  // when Fyers rejects with the misleading "Algo orders are not
  // allowed from this app" message that's actually about the
  // server's IP not being on the app's whitelist). Lets the UI
  // render a custom banner with a copyable public IP.
  reason?: string | null;
  entry_used?: number;
  stop_loss_used?: number;
  target_used?: number;
  entry_is_synthetic?: boolean;
}

export interface PendingOrder {
  id: number;
  broker_order_id: string | null;
  broker_account_id: number | null;
  symbol: string;
  side: Side;
  quantity: number;
  price: number;
  order_type: string;
  status: string;
  created_at: string | null;
}

export interface PendingOrdersResponse {
  ok: boolean;
  count: number;
  orders: PendingOrder[];
}

export interface QuoteResponse {
  ok: boolean;
  symbol: string;
  last_price?: number;
  bid?: number | null;
  ask?: number | null;
  volume?: number | null;
  as_of?: string;
  reason?: string;
}

export interface HistoryResponse {
  ok: boolean;
  symbol?: string;
  resolution?: string;
  // Fyers candle rows: [epoch_s, open, high, low, close, volume]
  candles?: number[][];
  reason?: string | null;
}
