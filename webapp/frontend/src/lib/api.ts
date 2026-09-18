/* ---------------------------------------------------------------
   API 契约类型 + fetch 封装（对齐 webapp/server.py 路由）
---------------------------------------------------------------- */

export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  let json: any = null;
  try {
    json = JSON.parse(await res.text());
  } catch {
    throw new Error(`非 JSON 响应（HTTP ${res.status}）`);
  }
  if (!res.ok || (json && typeof json === "object" && json.error)) {
    throw new Error(String(json?.error || `HTTP ${res.status}`));
  }
  return json as T;
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let json: any = null;
  try {
    json = JSON.parse(await res.text());
  } catch {
    throw new Error(`非 JSON 响应（HTTP ${res.status}）`);
  }
  if (!res.ok || (json && typeof json === "object" && json.error)) {
    throw new Error(String(json?.error || `HTTP ${res.status}`));
  }
  return json as T;
}

/* ------------------------------ 类型 ------------------------------ */

export interface Position {
  code: string;
  name: string;
  shares: number;
  avail_shares: number;
  cost: number;
  latest_price: number | null;
  latest_date: string;
  market_value: number | null;
  unrealized_pnl: number | null;
  pct_of_total: number;
}

export interface BlacklistRow {
  code: string;
  name: string;
  ok: boolean;
  reason: string;
}

export interface Overview {
  as_of: string;
  cash: number;
  market_value: number;
  total: number;
  drawdown: number; // 0~1 小数
  kill_switch: number;
  start_cash: number;
  cum_return_pct: number;
  benchmark: { name: string; close: number | null; cum_return_pct: number };
  excess_pct: number;
  positions: Position[];
  health_issues: string[];
  blacklist: BlacklistRow[];
  kill_switch_active?: boolean;
}

export interface EquityCurve {
  dates: string[];
  total: (number | null)[];
  benchmark: (number | null)[];
  drawdown: number[]; // 百分数
}

export interface Candles {
  code: string;
  name: string;
  dates: string[];
  kline: {
    open: number[];
    high: number[];
    low: number[];
    close: number[];
    volume: number[];
  };
  pct_chg?: number[];
  ma5: (number | null)[];
  ma20: (number | null)[];
  ma60: (number | null)[];
}

export interface SignalRow {
  code: string;
  name: string;
  as_of: string;
  signals: {
    ma_trend?: "up" | "flat" | "down" | string;
    ma5?: number;
    ma20?: number;
    ma60?: number;
    rsi_14?: number;
    atr_14?: number;
    mom_20d?: number;
    turnover_pct?: number;
    close?: number;
    pct_chg?: number;
    above_ma60?: boolean;
  };
  score: number;
}


export interface ConceptStock {
  code: string;
  name: string;
  tradable?: boolean;          // 是否在可交易池（watchlist_core）内
  close: number | null;
  pct_chg: number | null;
  bar_date: string | null;
  amount?: number | null;
  source?: string | null;
  score: number | null;
  ma_trend?: string;
  rsi_14?: number | null;
  mom_20d?: number | null;
  blacklisted: boolean;
  blacklist_reason: string;
}

export interface ConceptGroup {
  name: string;
  stocks: ConceptStock[];
  tradable_count?: number;     // 组内可交易只数
  observation_only?: boolean;  // 整组仅观察（不可 buy/sell）
}

export interface ConceptsData {
  total: number;
  core_total?: number;         // 可交易池规模
  concepts: ConceptGroup[];
}


export interface DynPoolRow {
  code: string;
  name: string;
  reasons: string[];
  strength: number;
  added_date: string;
  mode?: string;
}

export interface DynamicPools {
  movers: DynPoolRow[];
  hot_theme: DynPoolRow[];
  hot_stock: DynPoolRow[];
  boards: { board: string; pct_chg: number }[];
  dates: Record<string, string[]>;
  updated_at: string | null;
}

export interface DataStatus {
  generated_at: string;
  latest_bar_date: string | null;
  watchlist_total: number;
  watchlist_lagging: { code: string; name: string; latest_bar_date: string | null }[];
  indexes: { index_code: string; latest_date: string | null }[];
  pools: { pool: string; added_date: string | null; mode: string; count: number }[];
  signal: { as_of: string | null; rows: number };
  decision: { run_date: string | null; rows_latest: number };
  sources_30d: { source: string | null; rows: number; latest_date: string | null }[];
  recent_fails: { code: string; run_at: string; detail: string }[];
  audit: {
    total?: number;
    kinds?: Record<string, number>;
    sample?: { kind: string; code: string; date: string; detail: string }[];
    checked_at?: string;
    error?: string;
  };
  session: { run_date: string | null; bundle_mtime: string | null; decision: boolean };
  quotes_audit: { latest_file: string | null; age_min: number | null };
}

export interface Decision {
  id: number;
  run_date: string;
  code: string;
  name: string;
  action: string;
  target_weight: number;
  confidence: number;
  status: "proposed" | "approved" | "rejected" | "executed" | "report_only" | string;
  reasons: string[];
  risk_notes: string[];
  created_at: string;
}

export interface Trade {
  id: number;
  trade_date: string;
  code: string;
  name: string;
  side: string; // buy / sell
  price: number;
  shares: number;
  amount: number;
  order_id: string | null;
  status: string;
  decision_id: number | null;
  confirmed_by: string | null;
  created_at: string;
}

export interface RiskEvent {
  id: number;
  ts: string;
  rule: string;
  detail: string;
  decision_id: number | null;
}

export interface WorkflowStage {
  id: string;
  name: string;
  desc: string;
  status: "ok" | "warn" | "fail" | "idle" | string;
  detail: string;
  ts: string | null;
}

export interface Trace {
  id: number;
  code: string;
  action: string;
  target_weight: number;
  confidence: number;
  status: string;
  created_at: string;
  reasons: string[];
  risk_events: { ts: string; rule: string; detail: string }[];
  trade: Pick<Trade, "id" | "side" | "price" | "shares" | "amount" | "status" | "confirmed_by"> | null;
  pending: boolean;
}

export interface Workflow {
  run_date: string | null;
  generated_at: string;
  kill_switch: boolean;
  health_issues: string[];
  stages: WorkflowStage[];
  traces: Trace[];
}

export interface NewsItem {
  title: string;
  source: string;
  published_at: string;
  url: string;
  content: string;
}

export interface NewsData {
  market: NewsItem[];
  by_code: Record<string, NewsItem[]>;
}

export interface MacroIndex {
  index_code: string;
  name: string;
  trade_date: string;
  pe: number;
  pe_pct: number; // 0~1
  pb: number;
  pb_pct: number; // 0~1
  close: number;
}

export interface MacroData {
  indices: MacroIndex[];
}

export interface MacroHistory {
  index: string;
  name: string;
  dates: string[];
  pe: (number | null)[];
  pe_pct: (number | null)[]; // 0~1 原值
}

export interface HealthData {
  health_issues: string[];
  fetch_log: { code: string; run_at: string; status: string; rows: number; detail: string }[];
  codes: { code: string; name: string; latest_bar_date: string | null; rows: number }[];
}

export interface ReportFile {
  file: string;
  size: number;
  mtime: number;
}

export interface ReportContent {
  name: string;
  markdown: string;
}

export interface LogData {
  name: string;
  lines: string[];
}

export interface PendingItem {
  decision_id: number;
  path: string;
  decision: Partial<Decision> & Record<string, unknown>;
  verdict: {
    approved: boolean;
    violations: string[];
    warnings: string[];
    adjusted_order?: unknown;
    [k: string]: unknown;
  };
  confirm_hint: string;
  reject_hint: string;
  created_at: string;
}

export interface SessionInfo {
  date: string;
  has_bundle: boolean;
  has_decision: boolean;
}

export interface SessionContent {
  date: string;
  kind: string;
  file: string;
  content: string;
}

export interface DocResp {
  name: string;
  markdown?: string;
  missing?: boolean;
}

export interface BacktestStrategy {
  total_return?: number;
  annual_return?: number;
  max_drawdown?: number;
  [k: string]: unknown;
}

export interface BacktestProfile {
  strategy?: string;
  params?: Record<string, unknown>;
  window?: { start?: string; end?: string; trading_days?: number; [k: string]: unknown };
  strategy_perf?: BacktestStrategy & { per_year?: Record<string, number> };
  benchmark_hs300?: BacktestStrategy & { per_year?: Record<string, number> };
  rebalance_count?: number;
  final_holdings?: {
    as_of?: string;
    codes?: string[];
    weights?: Record<string, number>;
    cash_weight?: number;
    [k: string]: unknown;
  };
  pass?: boolean;
  slippage_sensitivity_annual?: Record<string, number>;
  [k: string]: unknown;
}

export interface BacktestData {
  missing?: boolean;
  generated_at?: string;
  universe?: { codes?: number; codes_with_qfq?: number; [k: string]: unknown };
  profiles?: Record<string, BacktestProfile>;
  notes?: string[];
  selected_profile?: string;
  [k: string]: unknown;
}

export interface ConfirmResp {
  ok: boolean;
  output: string;
}

export const LOG_NAMES = ["fetch", "news", "macro", "signal", "ai", "pipeline", "exec"] as const;
export type LogName = (typeof LOG_NAMES)[number];
