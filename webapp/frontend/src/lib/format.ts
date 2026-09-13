/* ---------------------------------------------------------------
   展示格式化：千分位金额、两位小数百分比、红涨绿跌类名、时间戳缩写
---------------------------------------------------------------- */

export function fmtMoney(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return v.toLocaleString("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

/** 金额（万元）：大数终端常用口径 */
export function fmtWan(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return fmtMoney(v / 10000, digits);
}

export function fmtPct(v: number | null | undefined, digits = 2, signed = false): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const s = v.toFixed(digits);
  return signed && v > 0 ? `+${s}%` : `${s}%`;
}

export function fmtNum(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return v.toLocaleString("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function fmtInt(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return Math.round(v).toLocaleString("zh-CN");
}

/** 红涨绿跌：v>0 → up（红），v<0 → down（绿），0 → 中性 */
export function pctCls(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v) || v === 0) return "";
  return v > 0 ? "text-up" : "text-down";
}

export function signCls(v: number | null | undefined): string {
  return pctCls(v);
}

/** "2026-09-12T18:01:51" → "09-12 18:01" */
export function fmtTs(iso: string | null | undefined): string {
  if (!iso) return "—";
  const m = iso.match(/\d{4}-(\d{2}-\d{2})[T ](\d{2}:\d{2})/);
  if (m) return `${m[1]} ${m[2]}`;
  return iso;
}

/** 完整时间（Tooltip 用） */
export function fmtTsFull(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.replace("T", " ");
}

export function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

export function fmtMtime(sec: number): string {
  if (!sec) return "—";
  const d = new Date(sec * 1000);
  const p = (x: number) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/* ------------------------- 领域词表 ------------------------- */

export const ACTION_LABEL: Record<string, string> = {
  buy: "买入",
  sell: "卖出",
  hold: "持有",
  watch: "观察",
  reduce: "减仓",
  add: "加仓",
};

export function actionLabel(a: string | null | undefined): string {
  if (!a) return "—";
  return ACTION_LABEL[a] ?? a;
}

export const DECISION_STATUS_LABEL: Record<string, string> = {
  proposed: "待裁决",
  approved: "已批准",
  rejected: "已否决",
  executed: "已执行",
  report_only: "仅报告",
};

export const TRADE_STATUS_LABEL: Record<string, string> = {
  filled: "已成交",
  pending: "待成交",
  canceled: "已撤销",
  rejected: "已拒绝",
};

export const STAGE_STATUS_LABEL: Record<string, string> = {
  ok: "正常",
  warn: "告警",
  fail: "异常",
  idle: "空闲",
};

export function unknown<T extends Record<string, string>>(map: T, k: string | null | undefined): string {
  if (!k) return "—";
  return map[k] ?? k;
}
