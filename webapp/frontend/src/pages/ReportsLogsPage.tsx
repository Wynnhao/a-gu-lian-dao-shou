import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";
import { EmptyState } from "@/components/EmptyState";
import { ErrorBar } from "@/components/ErrorBar";
import { MarkdownView } from "@/components/MarkdownView";
import { Panel } from "@/components/Panel";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  apiGet,
  type BacktestData,
  type HealthData,
  type LogData,
  type ReportContent,
  type ReportFile,
} from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { useRefresh } from "@/lib/refresh";
import { LOG_NAMES } from "@/lib/api";
import { fmtBytes, fmtMtime } from "@/lib/format";
import { cn } from "@/lib/utils";

/* 报告与日志页：左列表右渲染 + 日志查看器 + 回测 JSON 折叠 */

export function ReportsLogsPage() {
  const { manualTick } = useRefresh();
  const reports = useApi(() => apiGet<ReportFile[]>("/api/reports"), [manualTick]);
  const files = reports.data ?? [];
  const [picked, setPicked] = useState<string | null>(null);
  const active = picked ?? files[0]?.file ?? null;

  const report = useApi(
    () => (active ? apiGet<ReportContent>(`/api/report?file=${encodeURIComponent(active)}`) : Promise.resolve(null)),
    [active],
    { enabled: Boolean(active) },
  );

  return (
    <div className="space-y-3">
      <Panel
        title="复盘报告"
        caliber={<>logs/reports · {files.length} 份 · 新→旧</>}
        bodyClassName="p-0"
      >
        {reports.err ? (
          <div className="p-3">
            <ErrorBar msg={reports.err} onRetry={reports.refetch} />
          </div>
        ) : files.length === 0 ? (
          <EmptyState msg="暂无复盘报告" reason="P10 盘后复盘未生成（logs/reports/ 为空）" />
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-[260px_minmax(0,1fr)]">
            <ul className="max-h-[460px] divide-y divide-border overflow-y-auto border-b md:border-b-0 md:border-r">
              {files.map((f) => (
                <li key={f.file}>
                  <button
                    type="button"
                    onClick={() => setPicked(f.file)}
                    className={cn(
                      "block w-full px-3 py-1.5 text-left hover:bg-accent/60 focus-visible:outline-none",
                      active === f.file && "bg-accent",
                    )}
                  >
                    <div className="num truncate text-[12px] font-medium" title={f.file}>
                      {f.file}
                    </div>
                    <div className="num text-[10px] text-muted-foreground">
                      {fmtBytes(f.size)} · {fmtMtime(f.mtime)}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
            <div className="max-h-[460px] min-w-0 overflow-y-auto px-3 py-2.5">
              {report.err ? (
                <ErrorBar msg={report.err} onRetry={report.refetch} />
              ) : report.data?.markdown ? (
                <MarkdownView markdown={report.data.markdown} />
              ) : (
                <EmptyState msg={active ? "报告加载中…" : "未选择报告"} />
              )}
            </div>
          </div>
        )}
      </Panel>

      <div className="grid grid-cols-1 gap-3 2xl:grid-cols-[minmax(0,1fr)_460px]">
        <LogViewer />
        <BacktestCard />
      </div>

      <Panel title="数据健康" caliber="/api/health · fetch_log 与各票行数" bodyClassName="p-3">
        <HealthPanel />
      </Panel>
    </div>
  );
}

function LogViewer() {
  const [name, setName] = useState<string>("exec");
  const [lines, setLines] = useState<string>("200");
  const [autoScroll, setAutoScroll] = useState(true);
  const { manualTick } = useRefresh();
  const log = useApi(
    () => apiGet<LogData>(`/api/logs?name=${name}&lines=${lines}`),
    [name, lines, manualTick],
  );
  const preRef = useRef<HTMLPreElement>(null);

  useEffect(() => {
    if (autoScroll && preRef.current) {
      preRef.current.scrollTop = preRef.current.scrollHeight;
    }
  }, [log.data, autoScroll]);

  const text = (log.data?.lines ?? []).join("\n");

  return (
    <Panel
      title="运行日志"
      caliber={<>logs/*.log · 7 个白名单文件 · 尾部 {lines} 行</>}
      actions={
        <div className="flex items-center gap-1.5">
          <Select value={name} onValueChange={setName}>
            <SelectTrigger className="h-5 min-w-[104px] text-[11px]" aria-label="日志文件">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {LOG_NAMES.map((n) => (
                <SelectItem key={n} value={n} className="num">
                  {n}.log
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select value={lines} onValueChange={setLines}>
            <SelectTrigger className="h-5 min-w-[80px] text-[11px]" aria-label="行数">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {["100", "200", "500", "1000", "2000"].map((n) => (
                <SelectItem key={n} value={n} className="num">
                  {n} 行
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <label className="flex cursor-pointer select-none items-center gap-1 text-[11px] text-muted-foreground">
            <input
              type="checkbox"
              checked={autoScroll}
              onChange={(e) => setAutoScroll(e.target.checked)}
              className="h-3 w-3 accent-[rgb(var(--primary))]"
            />
            自动滚底
          </label>
        </div>
      }
      bodyClassName="p-0"
    >
      {log.err ? (
        <div className="p-3">
          <ErrorBar msg={log.err} onRetry={log.refetch} />
        </div>
      ) : text === "" ? (
        <EmptyState msg={`${name}.log 无内容`} reason="该环节尚未产生日志（文件为空或不存在）" />
      ) : (
        <pre
          ref={preRef}
          className="num h-[300px] overflow-auto whitespace-pre px-3 py-2 text-[11px] leading-[1.6] text-muted-foreground"
        >
          {text}
        </pre>
      )}
    </Panel>
  );
}

function BacktestCard() {
  const [open, setOpen] = useState(false);
  const bt = useApi(() => apiGet<BacktestData>("/api/backtest"), []);
  const pretty = useMemo(() => {
    if (!bt.data) return "";
    if (bt.data.missing) return "";
    try {
      return JSON.stringify(bt.data, null, 2);
    } catch {
      return String(bt.data);
    }
  }, [bt.data]);

  if (bt.err) return <ErrorBar msg={bt.err} onRetry={bt.refetch} />;

  return (
    <Panel
      title="回测结果"
      caliber={bt.data?.missing ? "backtest_result.json 不存在" : "logs/backtest_result.json 原文"}
      actions={
        pretty ? (
          <Button variant="ghost" size="sm" className="h-5 px-1.5" onClick={() => setOpen((o) => !o)}>
            {open ? (
              <>
                收起 <ChevronUp className="h-3 w-3" />
              </>
            ) : (
              <>
                展开 <ChevronDown className="h-3 w-3" />
              </>
            )}
          </Button>
        ) : undefined
      }
      bodyClassName="p-0"
    >
      {bt.data?.missing ? (
        <EmptyState msg="暂无回测结果" reason="signals/backtest.py 尚未产出 backtest_result.json" />
      ) : (
        <>
          {/* 关键指标行 */}
          <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 px-3 py-2.5 text-table sm:grid-cols-3">
            <Metric label="策略年化" value={fmtRate(bt.data?.strategy?.annual_return)} />
            <Metric label="策略累计" value={fmtRate(bt.data?.strategy?.total_return)} />
            <Metric label="最大回撤" value={fmtRate(bt.data?.strategy?.max_drawdown)} tone="down" />
            <Metric label="基准年化" value={fmtRate(bt.data?.benchmark_hs300?.annual_return)} />
            <Metric label="基准累计" value={fmtRate(bt.data?.benchmark_hs300?.total_return)} />
            <Metric label="调仓次数" value={String(bt.data?.rebalance_count ?? "—")} />
          </div>
          {open && (
            <pre className="num max-h-[260px] overflow-auto border-t whitespace-pre px-3 py-2 text-[11px] leading-[1.6] text-muted-foreground">
              {pretty}
            </pre>
          )}
        </>
      )}
    </Panel>
  );
}

function Metric({ label, value, tone }: { label: string; value: string; tone?: "down" }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <span className="text-muted-foreground">{label}</span>
      <span className={cn("num font-medium", tone === "down" && "text-down")}>{value}</span>
    </div>
  );
}

function fmtRate(v: unknown): string {
  const n = typeof v === "number" ? v : Number(v);
  if (Number.isNaN(n)) return "—";
  return (n * 100).toFixed(2) + "%";
}

/** 数据健康面板（报告页底部：数据源各票行数——此前自递归 bug 且从未挂载） */
export function HealthPanel() {
  const health = useApi(() => apiGet<HealthData>("/api/health"), []);
  if (health.err) return <ErrorBar msg={health.err} onRetry={health.refetch} />;
  const d = health.data;
  if (!d) return null;
  return (
    <div className="text-table text-muted-foreground">
      数据源各票行数：{d.codes.map((c) => `${c.code}=${c.rows}`).join(" · ")}
    </div>
  );
}
