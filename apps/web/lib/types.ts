export interface OpenPosition {
  symbol: string;
  qty: number;
  entry_price?: number;
  stop_price?: number;
  take_profit_price?: number;
  entry_timestamp?: string;
  entry_tier?: string;
}

export interface BotEvent {
  timestamp_utc: string;
  event_type: string;
  severity: string;
  symbol?: string;
  message: string;
  score?: number;
  grade?: string;
  blocked_by?: string;
}

export interface DashboardData {
  bot_running: boolean;
  market_open: boolean | null;
  current_et_time: string;
  last_scan_time: string | null;
  open_positions: OpenPosition[];
  realized_pnl: number;
  unrealized_pnl: number;
  daily_pnl: number;
  recent_events: BotEvent[];
  alerts_enabled: boolean;
  daily_loss_shutdown: boolean;
  dry_run: boolean;
  paper_mode: boolean;
}

export interface Account {
  equity?: string;
  buying_power?: string;
  cash?: string;
  portfolio_value?: string;
  error?: string;
}

export interface SessionMiniStats {
  total_symbols_evaluated: number;
  entered: number;
  near_misses: number;
  errors: number;
}

export interface SafetyFlags {
  flatten_at_window_end: boolean;
  session_flattened: boolean;
  observe_only_mode: boolean;
  disable_new_entries: boolean;
  alpaca_paper: boolean;
  allow_live_trading: boolean;
  dry_run: boolean;
}

export interface RuntimeStats {
  current_et_time: string;
  market_is_open: boolean | null;
  inside_trading_window: boolean;
  trading_window: string;
  opening_momentum_mode: boolean;
  next_action: string;
  reason_not_scanning: string | null;
  last_scan_at_et: string | null;
  total_scan_cycles: number;
  scan_cadence_seconds: number;
  uptime_minutes: number;
  watchlist: string[];
  session_mini_stats: SessionMiniStats;
  safety_flags: SafetyFlags;
}

export interface HermesSessionStats {
  entries_today: number;
  exits_today: number;
  errors_today: number;
  buy_signals_seen: number;
  entries_blocked: number;
  near_misses: number;
  scan_cycles: number;
  market_open: boolean | null;
  daily_shutdown: boolean;
  session_flattened: boolean;
  data_source: string;
}

export interface Blocker {
  blocker: string;
  count: number;
}

export interface HermesTrade {
  symbol: string;
  time: string;
  message: string;
}

export interface HermesWarning {
  type: string;
  severity: string;
  message: string;
  time: string;
}

export interface HermesSummary {
  safety_note: string;
  session_date: string;
  what_happened: string[];
  why_no_trades: string | null;
  best_near_miss: string | null;
  main_blockers: Blocker[];
  bot_behaved_correctly: boolean;
  correctness_notes: string[];
  trades_entered: HermesTrade[];
  trades_exited: HermesTrade[];
  warnings_errors: HermesWarning[];
  needs_review: string[];
  session_stats: HermesSessionStats;
}

export interface EventsResponse {
  count: number;
  events: BotEvent[];
}

export interface ScanStateSummary {
  total_scan_cycles: number;
  total_symbols_evaluated: number;
  last_scan_at_utc: string | null;
  buy_signals_seen: number;
  entries_taken: number;
  entries_blocked: number;
  near_misses: number;
  errors: number;
  avg_symbols_per_cycle: number;
}

export interface DailySessionStats {
  total_scans: number;
  buy_signals: number;
  entered: number;
  exited: number;
  blocked_total: number;
  near_miss_count: number;
  best_near_miss: string | null;
  errors: number;
  avg_entry_score: number | null;
  session_realized_pnl: number;
}

export interface DailyReport {
  report_time_utc: string;
  stale_data_warning: string | null;
  scan_state_summary: ScanStateSummary;
  session_stats: DailySessionStats;
}

export interface EodTradeSummary {
  entries: number;
  exits: number;
  closed_trades: number;
  winning_trades: number;
  win_rate_pct: number | null;
  realized_pnl: number;
  open_positions_count: number;
}

export interface EodBestOpportunity {
  symbol: string;
  score?: number;
  grade?: string;
  gaps?: string;
  realized_pnl?: number;
}

export interface EodBlocker {
  blocker: string;
  count: number;
}

export interface EodStrategyVerdict {
  verdict: "NO_CHANGE" | "REVIEW_AFTER_MORE_DATA";
  reason: string;
  note: string;
}

export interface EndOfDayReview {
  safety_note: string;
  session_date: string;
  generated_at_utc: string;
  executive_summary: string;
  trade_summary: EodTradeSummary;
  no_trade_analysis: string | null;
  best_opportunity: EodBestOpportunity | null;
  blocker_breakdown: EodBlocker[];
  lessons_learned: string[];
  recommended_next_steps: string[];
  strategy_change_recommendation: EodStrategyVerdict;
}

export interface WatchlistEntry {
  symbol: string;
  qty: number;
  side: string;
  market_value: number;
  unrealized_pl: number;
  error?: string;
}

export interface WatchlistResponse {
  results: WatchlistEntry[];
}
