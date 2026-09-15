import { Panel } from "@/components/Panel";
import { ErrorBar } from "@/components/ErrorBar";
import { EmptyState } from "@/components/EmptyState";
import { Inspect, type InspectInfo } from "@/components/Inspector";
import { apiGet, type DataStatus } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { useRefresh } from "@/lib/refresh";
import { cn } from "@/lib/utils";

/* 数据状态面板：数据新鲜度/数据源用量/体检摘要一屏可见。
   对应 /api/data_status——把"这数是几点、从哪来、体检过没"从事后翻日志
   变成看板常驻信息（口径：一切新鲜度以 daily_bar 最新交易日为锚）。 */

const POOL_NAMES: Record<string, string> = {
  movers: "异动池",
  hot_theme: "热门题材",
  hot_stock: "个股热榜",
};

function Chip({
  label,
  info,
  tone = "neutral",
}: {
  label: string;
  info: InspectInfo;
  tone?: "neutral" | "warn" | "bad";
}) {
  return (
    <div
      className={cn(
        "min-w-0 rounded border px-2 py-1.5 text-xs",
        tone === "bad"
          ? "border-destructive/40 bg-destructive/5"
          : tone === "warn"
            ? "border-amber-500/40 bg-amber-500/5"
            : "border-line",
      )}
    >
      <div className="mb-0.5 text-[10px] text-muted-foreground">{label}</div>
      <Inspect info={info}>
        <span
          className={cn(
            "font-mono tabular-nums",
            tone === "bad" && "text-destructive",
            tone === "warn" && "text-amber-600 dark:text-amber-400",
          )}
        >
          {info.value}
        </span>
      </Inspect>
    </div>
  );
}

export function DataStatusPanel({ className }: { className?: string }) {
  const { manualTick } = useRefresh();
  const st = useApi(() => apiGet<DataStatus>("/api/data_status"), [manualTick]);
  const d = st.data;

  const auditTotal = d?.audit.total;
  const freshN = d ? d.watchlist_total - (d.watchlist_lagging?.length ?? 0) : 0;

  return (
    <Panel
      title="数据状态"
      caliber={
        <>
          生成 {d?.generated_at ?? "—"} · 锚定日线 {d?.latest_bar_date ?? "—"}
        </>
      }
      className={className}
      bodyClassName="p-0"
    >
      {st.err && <ErrorBar msg={st.err} onRetry={st.refetch} />}
      {!st.err && !d && !st.loading && <EmptyState msg="数据状态不可用" />}
      {d && (
        <>
          <div className="grid grid-cols-2 gap-2 p-3 md:grid-cols-4 xl:grid-cols-7">
            <Chip
              label="自选池日线"
              info={{
                title: "自选池日线新鲜度",
                value: `${freshN}/${d.watchlist_total}`,
                asOf: d.latest_bar_date ?? "—",
                source: "daily_bar · fetcher",
                caliber: "各票最新 bar 是否对齐全局最新交易日",
                extra: (d.watchlist_lagging ?? [])
                  .slice(0, 10)
                  .map((x) => `${x.code} ${x.name} 停在 ${x.latest_bar_date ?? "—"}`),
              }}
              tone={(d.watchlist_lagging?.length ?? 0) > 0 ? "warn" : "neutral"}
            />
            <Chip
              label="信号表"
              info={{
                title: "技术信号",
                value: d.signal.as_of ?? "—",
                unit: `· ${d.signal.rows.toLocaleString()} 行`,
                source: "signal 表",
                caliber: "signals.signals 逐日计算；score 口径见 config.signals.profile",
              }}
            />
            <Chip
              label="当日决策"
              info={{
                title: "LLM 决策",
                value: d.decision.run_date ?? "—",
                unit: `· ${d.decision.rows_latest} 条`,
                source: "decision 表 + logs/session",
                caliber: "bundle 生成后由 LLM 产出，pending 单走人工闸门",
              }}
            />
            {d.pools.map((p) => (
              <Chip
                key={p.pool}
                label={POOL_NAMES[p.pool] ?? p.pool}
                info={{
                  title: `${POOL_NAMES[p.pool] ?? p.pool}最新刷新`,
                  value: p.added_date ?? "—",
                  unit: `· ${p.count} 只`,
                  source: "dynamic_pool 表",
                  caliber:
                    p.mode === "all"
                      ? "全市场快照口径（量比/涨幅/成交额，强度上限约3.5）"
                      : "自选/日线兜底口径（五规则，强度上限约7.2）——两口径强度不可比",
                }}
                tone={p.added_date && p.added_date < (d.latest_bar_date ?? "") ? "warn" : "neutral"}
              />
            ))}
            <Chip
              label="指数日线"
              info={{
                title: "指数日线新鲜度",
                value:
                  d.indexes.length > 0
                    ? `${d.indexes.filter((i) => i.latest_date === d.latest_bar_date).length}/${d.indexes.length} 对齐`
                    : "—",
                asOf: d.indexes.map((i) => i.latest_date).sort().pop() ?? "—",
                source: "index_daily 表",
                caliber: "对齐锚=日线最新交易日；缺当日行会导致日报基准失真（沪深300 前科）",
                extra: d.indexes.map((i) => `${i.index_code} → ${i.latest_date ?? "—"}`),
              }}
              tone={d.indexes.some((i) => i.latest_date !== d.latest_bar_date) ? "warn" : "neutral"}
            />
            <Chip
              label="数据体检"
              info={{
                title: "daily_bar 体检摘要",
                value: auditTotal != null ? `${auditTotal} 项` : "—",
                source: "data.audit.check_db（进程内缓存 5 分钟）",
                caliber: "kind 分布与样例见附加；存量量纲修复：python3 -m data.audit --fix",
                extra: [
                  ...Object.entries(d.audit.kinds ?? {}).map(([k, v]) => `${k}: ${v}`),
                  ...(d.audit.error ? [d.audit.error] : []),
                  ...(d.audit.sample ?? []).map(
                    (s) => `样例 ${s.code} ${s.date} ${s.detail}`,
                  ),
                ],
              }}
              tone={auditTotal == null ? "neutral" : auditTotal > 0 ? "bad" : "neutral"}
            />
            <Chip
              label="盘中快照"
              info={{
                title: "盘中实时行情快照审计",
                value:
                  d.quotes_audit.age_min != null ? `${d.quotes_audit.age_min.toFixed(0)} 分钟前` : "—",
                source: `logs/quotes/${d.quotes_audit.latest_file ?? "—"}`,
                caliber: "quotes.py 每次实时取价的落盘审计（jsonl），盘后无新增属正常",
              }}
            />
          </div>
          <div className="grid grid-cols-1 gap-x-6 border-t border-line px-3 py-2 text-[11px] text-muted-foreground md:grid-cols-2">
            <div>
              <span className="mr-2 inline-block">数据源用量（近30天）：</span>
              {d.sources_30d.length === 0
                ? "—"
                : d.sources_30d
                    .map(
                      (s) =>
                        `${s.source ?? "(无来源标记)"} ${s.rows.toLocaleString()} 行→${s.latest_date ?? "—"}`,
                    )
                    .join(" · ")}
            </div>
            <div className="min-w-0 truncate" title={d.recent_fails.map((f) => `${f.run_at} ${f.code} ${f.detail}`).join("\n")}>
              <span className="mr-2 inline-block">近期采集失败：</span>
              {d.recent_fails.length === 0
                ? "无"
                : d.recent_fails
                    .slice(0, 3)
                    .map((f) => `${f.run_at.slice(5, 16)} ${f.code} ${f.detail.slice(0, 40)}`)
                    .join(" ｜ ")}
            </div>
          </div>
        </>
      )}
    </Panel>
  );
}
