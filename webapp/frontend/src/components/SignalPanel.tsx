import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/EmptyState";
import { fmtPct, fmtTs } from "@/lib/format";
import type { SignalRow } from "@/lib/api";
import { cn } from "@/lib/utils";

/** MA 趋势徽章：up 红（多头）/ down 绿（空头）/ flat 灰 */
function MaTrendBadge({ v }: { v: string | undefined }) {
  if (v === "up") return <Badge variant="destructive">MA 多头</Badge>;
  if (v === "down") return <Badge variant="success">MA 空头</Badge>;
  if (v === "flat") return <Badge variant="neutral">MA 走平</Badge>;
  return <Badge variant="neutral">{v ?? "—"}</Badge>;
}

function RsiBadge({ v }: { v: number | undefined }) {
  if (v == null) return <Badge variant="neutral">RSI —</Badge>;
  const label = v >= 70 ? "超买" : v <= 30 ? "超卖" : "中性";
  return (
    <Badge variant={v >= 70 || v <= 30 ? "warn" : "outline"} title="RSI(14)">
      RSI {v.toFixed(1)} · {label}
    </Badge>
  );
}

/**
 * 信号面板：每票一行的紧凑行卡（score 横条 + 徽章 + 收盘涨跌）。
 * 点击行回调 onSelect 加载 K 线。
 */
export function SignalPanel({
  rows,
  activeCode,
  onSelect,
  className,
}: {
  rows: SignalRow[];
  activeCode?: string | null;
  onSelect?: (code: string) => void;
  className?: string;
}) {
  if (rows.length === 0) {
    return (
      <EmptyState
        msg="无信号数据"
        reason="P4 信号计算未产出（自选池为空或数据滞后）"
      />
    );
  }
  return (
    <div className={cn("divide-y divide-border", className)}>
      {rows.map((s) => {
        const sig = s.signals ?? {};
        const active = activeCode === s.code;
        return (
          <button
            key={s.code}
            type="button"
            onClick={() => onSelect?.(s.code)}
            className={cn(
              "block w-full px-3 py-2 text-left transition-colors",
              "hover:bg-accent/60 focus-visible:outline-none focus-visible:bg-accent",
              active && "bg-accent",
            )}
          >
            <div className="flex items-baseline justify-between gap-2">
              <span className="min-w-0 truncate text-[13px] font-medium">
                {s.name}
                <span className="num ml-1.5 text-tiny text-muted-foreground">{s.code}</span>
              </span>
              <span className="shrink-0 text-table">
                <span className="num">{sig.close ?? "—"}</span>{" "}
                <span className={cn("num", (sig.pct_chg ?? 0) > 0 && "text-up", (sig.pct_chg ?? 0) < 0 && "text-down")}>
                  {fmtPct(sig.pct_chg, 2, true)}
                </span>
              </span>
            </div>
            <div className="mt-1.5 flex items-center gap-2">
              <div className="h-1.5 min-w-0 flex-1 bg-track" title={`score ${s.score}`}>
                <div
                  className="h-full bg-primary"
                  style={{ width: `${Math.max(0, Math.min(1, s.score ?? 0)) * 100}%` }}
                />
              </div>
              <span className="num w-10 shrink-0 text-right text-tiny text-muted-foreground">
                {s.score?.toFixed(2) ?? "—"}
              </span>
            </div>
            <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
              <MaTrendBadge v={sig.ma_trend} />
              <RsiBadge v={sig.rsi_14} />
              <Badge
                variant="outline"
                title="20 日动量"
                className={cn(
                  (sig.mom_20d ?? 0) > 0 && "border-up/40 text-up",
                  (sig.mom_20d ?? 0) < 0 && "border-down/40 text-down",
                )}
              >
                动量 {fmtPct((sig.mom_20d ?? 0) * 100, 1, true)}
              </Badge>
              <Badge variant="outline" title="近250日换手率分位（0~100%）">
                换手分位 {sig.turnover_pct == null ? "—" : Math.round(sig.turnover_pct * 100) + "%"}
              </Badge>
              <span className="num ml-auto text-[10px] text-muted-foreground/80">
                {fmtTs(s.as_of?.replace(" ", "T"))}
              </span>
            </div>
          </button>
        );
      })}
    </div>
  );
}
