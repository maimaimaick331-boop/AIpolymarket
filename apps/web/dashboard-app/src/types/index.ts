export type RiskStatus = 'normal' | 'warning' | 'danger';

export type StrategyStatus = 'running' | 'paused' | 'stopped';

export interface StrategyRecentSignal {
  time_utc: string;
  side: string;
  signal_type: string;
  status: string;
  reason: string;
}

export interface StrategyRow {
  strategy_id: string;
  name: string;
  strategy_type: string;
  params: Record<string, unknown>;
  enabled: boolean;
  source: string;
  created_at_utc: string;
  status: StrategyStatus;
  total_pnl: number;
  win_rate: number;
  trade_count: number;
  max_drawdown_pct: number;
  equity: number;
  open_orders: number;
  runtime_hours?: number;
  running_since_utc?: string;
  pause_reason?: string;
  stop_reason?: string;
  recent_signals?: StrategyRecentSignal[];
  llm_warning?: boolean;
  llm_status?: LlmHealth;
}

export interface AccountSummary {
  balance_usdc: number;
  today_pnl: number;
  active_strategies: number;
  total_strategies: number;
  risk_status: RiskStatus;
  alerts_today: number;
  updated_at_utc: string;
}

export interface PnlPoint {
  time_utc: string;
  value: number;
  delta: number;
  strategy_id: string;
}

export interface StrategyPnlItem {
  strategy_id: string;
  pnl: number;
}

export interface PnlHistoryResponse {
  count: number;
  rows: PnlPoint[];
  by_strategy: StrategyPnlItem[];
  updated_at_utc: string;
}

export interface RecentTrade {
  time_utc: string;
  strategy_id: string;
  side: 'BUY' | 'SELL' | string;
  market_name: string;
  market_name_en?: string;
  token_id: string;
  price: number;
  quantity: number;
  signal_type?: string;
  signal_status?: string;
  decision_reason?: string;
}

export interface ProviderRow {
  provider_id: string;
  name: string;
  endpoint: string;
  adapter: string;
  model: string;
  enabled: boolean;
  available?: boolean;
  health_status?: string;
  health_error?: string;
  latency_ms?: number;
  weight: number;
  priority: number;
  company: string;
  has_api_key?: boolean;
  api_key_masked?: string;
}

export interface ProviderErrorRow {
  provider_id: string;
  error: string;
}

export interface ProviderPoolRow {
  provider_id: string;
  company?: string;
  model?: string;
  endpoint?: string;
  priority?: number;
  enabled: boolean;
  available: boolean;
  status: string;
  error?: string;
  latency_ms?: number;
  models_count?: number;
  enabled_before?: boolean;
}

export interface ProviderPoolState {
  current_provider_id: string;
  mode: string;
  rows: ProviderPoolRow[];
  updated_at_utc: string;
  reason: string;
}

export interface CompanyPreset {
  company: string;
  name: string;
  adapter: string;
  default_endpoint: string;
  requires_api_key: boolean;
  supports_catalog: boolean;
  docs_url: string;
}

export interface CatalogModel {
  id: string;
  name: string;
  context_length?: number | null;
  prompt_price?: string;
  completion_price?: string;
}

export interface GenerateJob {
  job_id: string;
  status: string;
  stage?: string;
  progress_pct?: number;
  message?: string;
}

export interface MarketMonitorRow {
  market_id: string;
  name: string;
  name_zh?: string;
  name_en?: string;
  mid_price: number;
  spread: number;
  spread_pct: number;
  volume_24h: number;
  depth_usdc: number;
  yes_no_sum: number;
  mm_opportunity: boolean;
  arb_opportunity: boolean;
  updated_at_utc: string;
}

export interface AiEvalRow {
  market_id: string;
  name: string;
  name_zh?: string;
  name_en?: string;
  market_yes_mid: number;
  ai_probability: number;
  deviation: number;
  confidence: number;
  triggered: boolean;
  reason: string;
  model: string;
  evaluated_at_utc: string;
}

export interface LlmHealth {
  ok: boolean;
  status: string;
  provider_id?: string;
  company?: string;
  model?: string;
  error?: string;
  provider_errors?: ProviderErrorRow[];
  provider_pool?: ProviderPoolState;
  detail?: string;
  latency_ms?: number;
  checked_at_utc?: string;
}

export interface QuantParams {
  arb_buy_threshold: number;
  arb_sell_threshold: number;
  fee_buffer: number;
  mm_liq_min: number;
  mm_liq_max: number;
  mm_min_spread: number;
  mm_min_volume: number;
  mm_min_depth_usdc: number;
  mm_min_market_count: number;
  mm_target_market_count: number;
  mm_max_single_side_position_usdc: number;
  mm_max_position_per_market_usdc: number;
  mm_inventory_skew_strength: number;
  mm_allow_short_sell: boolean;
  mm_taker_rebalance: boolean;
  ai_deviation_threshold: number;
  ai_min_confidence: number;
  ai_eval_interval_sec: number;
  ai_max_markets_per_cycle: number;
  enable_arb: boolean;
  enable_mm: boolean;
  enable_ai: boolean;
  updated_at_utc?: string;
}

export interface WorkshopMessage {
  role: 'user' | 'assistant';
  content: string;
}

export interface WorkshopTriggerCondition {
  type: 'spread_threshold' | 'ai_deviation' | 'arb_gap' | 'volume_filter' | 'price_range' | string;
  operator: '>=' | '<=' | '>' | '<' | '==' | string;
  value: number | string;
  description: string;
}

export interface WorkshopDraft {
  name: string;
  description: string;
  type: 'ai_probability' | 'arbitrage' | 'market_making' | 'spread_capture' | 'custom' | string;
  direction: 'buy_yes' | 'buy_no' | 'both' | 'market_make' | string;
  trigger_conditions: WorkshopTriggerCondition[];
  position_sizing: {
    per_trade_usdc: number;
    max_total_usdc: number;
  };
  risk_management: {
    stop_loss_total: number;
    stop_loss_per_trade_pct: number;
    take_profit_per_trade_pct: number;
    max_consecutive_losses: number;
  };
  market_filter: {
    min_volume_24h: number;
    min_liquidity: number;
    keywords: string[] | 'all';
  };
  check_interval_minutes: number;
}

export interface WorkshopChatResponse {
  ok: boolean;
  provider_id: string;
  selected_provider_id?: string;
  fallback_from_provider_id?: string;
  fallback_to_provider_id?: string;
  fallback_reason?: string;
  provider_errors?: ProviderErrorRow[];
  source: string;
  assistant: string;
  llm_error?: string;
  format_error?: boolean;
  draft: WorkshopDraft;
  updated_at_utc: string;
}

export interface StrategyInsightRow {
  time_utc: string;
  market_id: string;
  market_name: string;
  market_name_en?: string;
  source_title: string;
  source_url: string;
  source_name: string;
  ai_probability: number;
  confidence: number;
  market_yes_price: number;
  deviation: number;
  decision: string;
  decision_reason: string;
  execution: string;
  triggered: boolean;
  signal_status: string;
  model: string;
  signal_type?: string;
  signal_id?: number;
}

export interface StrategyOverviewMetrics {
  total_pnl: number;
  today_pnl: number;
  win_rate: number;
  profit_factor: number;
  max_drawdown: number;
  max_drawdown_pct?: number;
  trade_count: number;
  runtime_hours?: number;
  running_since_utc?: string;
}

export interface StrategyOverviewResponse {
  strategy: StrategyRow;
  metrics: StrategyOverviewMetrics;
  pnl_rows: PnlPoint[];
  trades: StrategyTradeRow[];
  insights: StrategyInsightRow[];
  param_history: StrategyParamHistoryRow[];
  versions: StrategyVersionRow[];
  updated_at_utc: string;
}

export interface StrategyTradeRow {
  id: number;
  time_utc: string;
  side: string;
  market: string;
  market_en?: string;
  market_id?: string;
  price: number;
  quantity: number;
  cost_usdc: number;
  pnl: number;
  decision_reason: string;
  signal_id?: number;
  signal_type?: string;
  signal_source_text?: string;
  signal_source_url?: string;
  archived?: boolean;
}

export interface StrategyParamHistoryRow {
  id: number;
  changed_at: string;
  changed_by: string;
  note: string;
  change: Record<string, { before: unknown; after: unknown }>;
}

export interface StrategyVersionRow {
  id: number;
  strategy_id: string;
  version_no: number;
  label: string;
  note: string;
  source: string;
  created_by: string;
  created_at: string;
  summary?: {
    strategy_type?: string;
    enabled?: boolean;
    param_count?: number;
    param_keys?: string[];
  };
}

export interface LiveStatus {
  live_trading_enabled: boolean;
  live_force_ack: boolean;
  live_max_order_usdc: number;
  has_private_key: boolean;
  has_funder: boolean;
  has_api_creds: boolean;
  host: string;
  chain_id: number;
  signature_type: number;
  paper_use_market_ws: boolean;
  market_ws_endpoint: string;
  quant_running: boolean;
  quant_cycle: number;
  quant_phase: string;
}

export interface LiveGateRow {
  strategy_id: string;
  eligible: boolean;
  runtime_hours: number;
  fills_count: number;
  win_rate: number;
  pnl_total: number;
  reasons: string[];
}

export interface LiveGateResponse {
  eligible_count: number;
  total_count: number;
  rows: LiveGateRow[];
  thresholds: {
    min_hours: number;
    min_win_rate: number;
    min_pnl: number;
    min_fills: number;
  };
}

export interface LiveOrderRow {
  id?: string;
  order_id?: string;
  time?: string;
  timestamp?: string;
  side: string;
  price: number | string;
  size: number | string;
  original_size?: number | string;
  status: string;
  token_id?: string;
  market?: string;
  asset_id?: string;
  type?: string;
}

export interface LiveTradeRow {
  id?: string;
  time?: string;
  timestamp?: string;
  side: string;
  price: number | string;
  size: number | string;
  token_id?: string;
  asset_id?: string;
  market?: string;
  fee_rate_bps?: number;
  status?: string;
}

export interface BotStatus {
  running: boolean;
  token_id: string;
  interval_sec: number;
  tick: number;
}
