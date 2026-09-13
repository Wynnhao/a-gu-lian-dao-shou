import { useMemo } from "react";
import { ExternalLink } from "lucide-react";
import { ChartCard } from "@/components/ChartCard";
import { DecisionTable, TradeMiniCard } from "@/components/DecisionTable";
import { EChart } from "@/components/EChart";
import { EmptyState } from "@/components/EmptyState";
import { ErrorBar } from "@/components/ErrorBar";
import { MarkdownView } from "@/components/MarkdownView";
import { Panel } from "@/components/Panel";
import { PipelineFlow } from "@/components/PipelineFlow";
import { StatTile } from "@/components/StatTile";
import { Badge } from "@/components/ui/badge";
import { apiGet, type Decision, type EquityCurve, type SignalRow } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { useLive, useRefresh } from "@/lib/refresh";
import { chartPalette, useTheme } from "@/lib/theme";
import { fmtMoney, fmtPct, fmtTs, pctCls } from "@/lib/format";
import { cn } from "@/lib/utils";

/* ============================== 决策工作流（第 1 个标签） ============================== */

export function WorkflowPage() {
  const { workflow } = useLive();
  const doc = useApi(() => apiGet<{ name: string; markdown?: string; missing?: boolean }>("/api/doc?name=decision_playbook"), []);
  const signals = useApi(() => apiGet<SignalRow[]>("/api/signals"), []);
  const wf = workflow.data;

  // traces 不带 name，用 /api/signals 的 stock_info 名称回填
  const nameByCode = useMemo(() => {
    const m = new Map<string, string>();
    for (const s of signals.data ?? []) m.set(s.code, s.name);
    return m;
  }, [signals.data]);

  const traces = useMemo(() => {
    if (!wf) return [];
    return wf.traces.map((t): Decision & { risk_events: typeof t.risk_events; trade: typeof t.trade; pending: boolean } => ({
      id: t.id,
      run_date: wf.run_date ?? "",
      code: t.code,
      name: nameByCode.get(t.code) ?? t.code,
      action: t.action,
      target_weight: t.target_weight ?? 0,
      confidence: t.confidence ?? 0,
      status: t.status,
      reasons: t.reasons ?? [],
      risk_notes: [],
      created_at: t.created_at,
      risk_events: t.risk_events,
      trade: t.trade,
      pending: t.pending,
    }));
  }, [wf, nameByCode]);

  const stages = wf?.stages ?? [];
  const failCount = stages.filter((s) => s.status === "fail").length;
  const warnCount = stages.filter((s) => s.status === "warn").length;

  return (
    <div className="space-y-3">
      {/* 十段流水线 */}
      {workflow.err ? (
        <ErrorBar msg={workflow.err} onRetry={workflow.refetch} />
      ) : (
        <Panel
          title={
            <span className="flex items-center gap-2">
              十段流水线
              {failCount > 0 && <Badge variant="destructive">异常 {failCount} 段</Badge>}
              {warnCount > 0 && <Badge variant="warn">告警 {warnCount} 段</Badge>}
            </span>
          }
          caliber={
            <>
              运行日 <span className="num">{wf?.run_date ?? "—"}</span> · 生成于{" "}
              <span className="num">{fmtTs(wf?.generated_at)}</span>
            </>
          }
          bodyClassName="p-0"
        >
          {stages.length > 0 ? (
            <PipelineFlow stages={stages} className="px-1.5 py-1" />
          ) : (
            !workflow.loading && <EmptyState msg="流水线状态不可用" reason="/api/workflow 未返回阶段数据" />
          )}
        </Panel>
      )}

      <div className="grid grid-cols-1 gap-3 xl:grid-cols-[minmax(0,1fr)_460px]">
        {/* 当日决策追踪链 */}
        <Panel
          title="当日决策追踪链"
          caliber={<>决策 {traces.length} 条 · 行展开看理由/风控/成交</>}
          bodyClassName="p-0"
        >
          {workflow.err ? (
            <div className="p-3">
              <ErrorBar msg={workflow.err} onRetry={workflow.refetch} />
            </div>
          ) : (
            <DecisionTable rows={traces} showRunDate={false} />
          )}
        </Panel>

        {/* 策略说明（decision_playbook markdown） */}
        <Panel
          title="策略说明"
          caliber="决策策略与工作流 · docs"
          actions={
            doc.data?.markdown ? (
              <span className="text-[10px] text-muted-foreground/70">GFM 表格已适配</span>
            ) : undefined
          }
          bodyClassName="p-0"
        >
          {doc.err ? (
            <div className="p-3">
              <ErrorBar msg={doc.err} onRetry={doc.refetch} />
            </div>
          ) : doc.data?.missing ? (
            <EmptyState msg="策略说明文档缺失" reason="docs/决策策略与工作流.md 不存在" />
          ) : doc.data?.markdown ? (
            <div className="max-h-[560px] overflow-y-auto px-3 py-2.5">
              <MarkdownView markdown={doc.data.markdown} />
            </div>
          ) : (
            <EmptyState msg="加载中…" />
          )}
        </Panel>
      </div>
    </div>
  );
}

/* ============================== 总览 ============================== */

export function OverviewPage({ onNavigate }: { onNavigate?: (p: "workflow") => void }) {
  const { overview, workflow } = useLive();
  const { dark } = useTheme();
  const { manualTick } = useRefresh();
  const ov = overview.data;
  const curve = useApi(() => apiGet<EquityCurve>("/api/equity_curve"), [manualTick]);

  const option = useMemo(() => {
    const c = chartPalette(dark);
    if (!curve.data || curve.data.dates.length === 0) return null;
    const { dates, total, benchmark, drawdown } = curve.data;
    return {
      animation: false,
      textStyle: { fontSize: 11, color: c.text },
      legend: {
        top: 4,
        right: 8,
        itemWidth: 14,
        itemHeight: 2,
        icon: "rect",
        textStyle: { color: c.text, fontSize: 11 },
        data: ["组合", "沪深300"],
      },
      axisPointer: { link: [{ xAxisIndex: "all" }], lineStyle: { color: c.line } },
      tooltip: {
        trigger: "axis",
        backgroundColor: c.tooltipBg,
        borderColor: c.tooltipBorder,
        textStyle: { color: c.text === "#9a9a94" ? "#e2e2de" : "#1c1c1a", fontSize: 11 },
        valueFormatter: (v: number) => (v == null ? "—" : fmtMoney(v, 0) + " 元"),
      },
      grid: [
        { left: 64, right: 16, top: 28, height: "56%" },
        { left: 64, right: 16, top: "74%", height: "18%" },
      ],
      xAxis: [
        {
          type: "category",
          gridIndex: 0,
          data: dates,
          axisLine: { lineStyle: { color: c.line } },
          axisTick: { show: false },
          axisLabel: { show: false },
        },
        {
          type: "category",
          gridIndex: 1,
          data: dates,
          axisLine: { lineStyle: { color: c.line } },
          axisTick: { show: false },
          axisLabel: { color: c.text, fontSize: 10, margin: 8 },
        },
      ],
      yAxis: [
        {
          gridIndex: 0,
          scale: true,
          splitLine: { lineStyle: { color: c.split } },
          axisLabel: {
            color: c.text,
            fontSize: 10,
            formatter: (v: number) => (v / 10000).toFixed(0) + "万",
          },
        },
        {
          gridIndex: 1,
          min: (v: { min: number }) => Math.floor(v.min) - 0.5,
          max: 0,
          splitLine: { show: false },
          axisLabel: { color: c.textDim, fontSize: 10, formatter: (v: number) => v + "%" },
        },
      ],
      series: [
        {
          name: "组合",
          type: "line",
          xAxisIndex: 0,
          yAxisIndex: 0,
          data: total,
          showSymbol: false,
          lineStyle: { width: 1.5, color: c.accent },
          itemStyle: { color: c.accent },
          emphasis: { disabled: false },
        },
        {
          name: "沪深300",
          type: "line",
          xAxisIndex: 0,
          yAxisIndex: 0,
          data: benchmark,
          showSymbol: false,
          lineStyle: { width: 1, color: c.gray, type: "solid" },
          itemStyle: { color: c.gray },
        },
        {
          name: "回撤",
          type: "line",
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: drawdown.map((v) => -Math.abs(v)),
          showSymbol: false,
          lineStyle: { width: 1, color: c.warn },
          itemStyle: { color: c.warn },
          areaStyle: { color: c.warn, opacity: 0.10 },
        },
      ],
    };
  }, [curve.data, dark]);

  const positions = ov?.positions ?? [];
  const blocked = (ov?.blacklist ?? []).filter((b) => !b.ok);
  const passed = (ov?.blacklist ?? []).filter((b) => b.ok);

  return (
    <div className="space-y-3">
      {overview.err && <ErrorBar msg={overview.err} onRetry={overview.refetch} />}

      {/* 核心数字行 */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
        <StatTile
          label="总资产"
          value={fmtMoney(ov?.total)}
          unit="元"
          sub={<>期初 {fmtMoney(ov?.start_cash, 0)}</>}
          note="盯市 · 最新收盘"
        />
        <StatTile
          label="现金"
          value={fmtMoney(ov?.cash)}
          unit="元"
          sub="可用资金（PaperBroker）"
          note="未含在途委托"
        />
        <StatTile
          label="持仓市值"
          value={fmtMoney(ov?.market_value)}
          unit="元"
          sub={positions.length > 0 ? `${positions.length} 只持仓` : "空仓"}
          note={`行情日 ${ov?.as_of || "—"}`}
        />
        <StatTile
          label="累计收益"
          value={fmtPct(ov?.cum_return_pct, 2, true)}
          sub={
            <>
              超额{" "}
              <span className={cn(pctCls(ov?.excess_pct))}>{fmtPct(ov?.excess_pct, 2, true)}</span>{" "}
              vs {(ov?.benchmark.name ?? "沪深300")}
            </>
          }
          subTone={(ov?.excess_pct ?? 0) >= 0 ? "up" : "down"}
          note="相对基准同起点"
        />
        <StatTile
          label="当前回撤"
          value={fmtPct(-(ov?.drawdown ?? 0) * 100, 2)}
          sub={(ov?.drawdown ?? 0) > 0.1 ? "接近熔断观察线" : "回撤可控"}
          subTone={(ov?.drawdown ?? 0) > 0.1 ? "warn" : "neutral"}
          note="相对净值高点"
        />
      </div>

      {/* 流水线状态速览 */}
      <Panel
        title="流水线状态"
        caliber={<>运行日 <span className="num">{workflow.data?.run_date ?? "—"}</span></>}
        actions={
          onNavigate && (
            <button
              type="button"
              onClick={() => onNavigate("workflow")}
              className="inline-flex items-center gap-1 text-tiny text-primary hover:underline focus-visible:outline-none"
            >
              查看十段流水线 <ExternalLink className="h-3 w-3" />
            </button>
          )
        }
        bodyClassName="p-0"
      >
        <PipelineStrip />
      </Panel>

      {/* 权益曲线 */}
      {curve.err ? (
        <ErrorBar msg={curve.err} onRetry={curve.refetch} />
      ) : option ? (
        <ChartCard
          title="权益曲线"
          caliber={<>组合 vs 沪深300 · 归一至期初 · 副图为回撤 · <span className="num">{curve.data?.dates.length ?? 0}</span> 个交易日</>}
          footnote="口径：portfolio_state 逐日盯市净值；基准为 index_daily 沪深300 收盘，同起点归一。"
          height={330}
        >
          <EChart option={option} height={330} />
        </ChartCard>
      ) : (
        !curve.loading && (
          <Panel title="权益曲线" bodyClassName="p-0">
            <EmptyState msg="权益曲线数据不足" reason="portfolio_state 不足 1 个交易日（P6 模拟盘未开始）" />
          </Panel>
        )
      )}

      <div className="grid grid-cols-1 gap-3 xl:grid-cols-[minmax(0,1fr)_420px]">
        {/* 持仓表 */}
        <Panel
          title="持仓明细"
          caliber={<>盯市口径 · 现价为日线收盘 · T+1 可卖</>}
          bodyClassName="p-0"
        >
          {positions.length === 0 ? (
            <EmptyState msg="无持仓" reason="P6 模拟盘未开始，或当日决策未产生建仓" />
          ) : (
            <table className="w-full text-table">
              <thead>
                <tr className="border-b text-left text-[11px] font-semibold text-muted-foreground [&>th]:px-2 [&>th]:py-1.5 [&>th]:font-semibold">
                  <th>代码</th>
                  <th>名称</th>
                  <th className="text-right">持股</th>
                  <th className="text-right">可卖</th>
                  <th className="text-right">成本</th>
                  <th className="text-right">现价</th>
                  <th className="text-right">市值</th>
                  <th className="text-right">浮盈</th>
                  <th className="text-right">浮盈%</th>
                  <th className="text-right">权重</th>
                  <th className="text-right">行情日</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((p) => (
                  <tr key={p.code} className="border-b border-border/70 hover:bg-muted/40">
                    <td className="num px-2 py-1.5">{p.code}</td>
                    <td className="px-2 py-1.5 font-medium">{p.name}</td>
                    <td className="num px-2 py-1.5 text-right">{p.shares.toLocaleString("zh-CN")}</td>
                    <td className="num px-2 py-1.5 text-right text-muted-foreground">{p.avail_shares.toLocaleString("zh-CN")}</td>
                    <td className="num px-2 py-1.5 text-right">{fmtMoney(p.cost)}</td>
                    <td className="num px-2 py-1.5 text-right">{fmtMoney(p.latest_price)}</td>
                    <td className="num px-2 py-1.5 text-right">{fmtMoney(p.market_value)}</td>
                    <td className={cn("num px-2 py-1.5 text-right", pctCls(p.unrealized_pnl))}>
                      {fmtMoney(p.unrealized_pnl)}
                    </td>
                    <td className={cn("num px-2 py-1.5 text-right", pctCls(p.unrealized_pnl))}>
                      {p.latest_price != null && p.cost > 0
                        ? fmtPct(((p.latest_price - p.cost) / p.cost) * 100, 2, true)
                        : "—"}
                    </td>
                    <td className="num px-2 py-1.5 text-right">{fmtPct(p.pct_of_total)}</td>
                    <td className="num px-2 py-1.5 text-right text-tiny text-muted-foreground">{p.latest_date || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>

        {/* 黑名单 / 数据健康 */}
        <Panel
          title="数据健康与黑名单"
          caliber={<>PASS {passed.length} · 拦截 {blocked.length}</>}
          bodyClassName="p-0"
        >
          <div className="space-y-2 px-3 py-2.5">
            {(ov?.health_issues ?? []).length > 0 ? (
              <ul className="space-y-0.5 text-table text-warn">
                {(ov?.health_issues ?? []).map((x, i) => (
                  <li key={i}>· {x}</li>
                ))}
              </ul>
            ) : (
              <p className="text-table text-muted-foreground">数据健康：OK（无告警）</p>
            )}
          </div>
          <div className="max-h-[280px] overflow-y-auto border-t">
            <table className="w-full text-table">
              <thead className="sticky top-0 bg-card">
                <tr className="border-b text-left text-[11px] font-semibold text-muted-foreground [&>th]:px-2 [&>th]:py-1.5">
                  <th>代码</th>
                  <th>名称</th>
                  <th>状态</th>
                  <th>原因</th>
                </tr>
              </thead>
              <tbody>
                {(ov?.blacklist ?? []).map((b) => (
                  <tr key={b.code} className="border-b border-border/70">
                    <td className="num px-2 py-1">{b.code}</td>
                    <td className="px-2 py-1">{b.name}</td>
                    <td className="px-2 py-1">
                      {b.ok ? (
                        <Badge variant="success">PASS</Badge>
                      ) : (
                        <Badge variant="destructive">BLOCK</Badge>
                      )}
                    </td>
                    <td className="px-2 py-1 text-muted-foreground">{b.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      </div>
    </div>
  );
}

/** 总览页内嵌的流水线速览（复用 /api/workflow 数据） */
function PipelineStrip() {
  const { workflow } = useLive();
  const stages = workflow.data?.stages ?? [];
  if (workflow.err) return <div className="p-3"><ErrorBar msg={workflow.err} onRetry={workflow.refetch} /></div>;
  if (stages.length === 0) return <EmptyState msg="流水线状态不可用" reason={workflow.loading ? "加载中…" : "/api/workflow 无数据"} />;
  return <PipelineFlow stages={stages} className="px-1.5 py-1" />;
}
