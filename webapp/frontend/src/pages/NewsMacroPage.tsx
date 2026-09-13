import { useEffect, useMemo, useState } from "react";
import { ExternalLink } from "lucide-react";
import { ChartCard } from "@/components/ChartCard";
import { EChart } from "@/components/EChart";
import { EmptyState } from "@/components/EmptyState";
import { ErrorBar } from "@/components/ErrorBar";
import { Panel } from "@/components/Panel";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { apiGet, type MacroData, type MacroHistory, type NewsData } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { useRefresh } from "@/lib/refresh";
import { chartPalette, useTheme } from "@/lib/theme";
import { fmtNum } from "@/lib/format";
import { cn } from "@/lib/utils";

/* 新闻与宏观页：新闻列表（市场/个股）+ 三指数估值卡 + PE 历史折线（30%/70% 分位背景带） */

export function NewsMacroPage() {
  const { manualTick } = useRefresh();
  const news = useApi(() => apiGet<NewsData>("/api/news?limit=30"), [manualTick]);
  const macro = useApi(() => apiGet<MacroData>("/api/macro"), [manualTick]);

  const stockCodes = useMemo(() => Object.keys(news.data?.by_code ?? {}), [news.data]);
  const [tab, setTab] = useState<"market" | "stock">("market");
  const [stockCode, setStockCode] = useState<string | null>(null);
  useEffect(() => {
    if (stockCode == null && stockCodes.length > 0) setStockCode(stockCodes[0]);
  }, [stockCodes, stockCode]);

  const items =
    tab === "market"
      ? (news.data?.market ?? [])
      : stockCode
        ? (news.data?.by_code[stockCode] ?? [])
        : [];

  return (
    <div className="grid grid-cols-1 gap-3 xl:grid-cols-[minmax(0,1fr)_460px]">
      {/* 新闻列表 */}
      <Panel
        title="新闻资讯"
        caliber={`东财/腾讯源 · 最近 ${items.length} 条 · 摘要截前100字`}
        bodyClassName="p-0"
      >
        {news.err ? (
          <div className="p-3">
            <ErrorBar msg={news.err} onRetry={news.refetch} />
          </div>
        ) : (
          <>
            <div className="border-b px-3 pt-1.5">
              <Tabs value={tab} onValueChange={(v) => setTab(v as "market" | "stock")}>
                <TabsList className="h-8">
                  <TabsTrigger value="market" className="px-2">
                    市场新闻
                  </TabsTrigger>
                  <TabsTrigger value="stock" className="px-2">
                    个股新闻
                  </TabsTrigger>
                </TabsList>
              </Tabs>
            </div>
            {tab === "stock" && (
              <div className="flex items-center gap-2 border-b px-3 py-1.5">
                <span className="text-tiny text-muted-foreground">标的</span>
                {stockCodes.length > 0 ? (
                  <Select value={stockCode ?? undefined} onValueChange={setStockCode}>
                    <SelectTrigger className="h-6 min-w-[160px]" aria-label="选择个股">
                      <SelectValue placeholder="选择个股" />
                    </SelectTrigger>
                    <SelectContent>
                      {stockCodes.map((c) => (
                        <SelectItem key={c} value={c} className="num">
                          {c}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                ) : (
                  <span className="text-tiny text-muted-foreground">暂无个股新闻</span>
                )}
              </div>
            )}
            {items.length === 0 ? (
              <EmptyState
                msg={tab === "market" ? "暂无市场新闻" : "该标的暂无新闻"}
                reason="资讯采集（P2）当日未入库，或过滤后为空"
              />
            ) : (
              <ul className="max-h-[calc(100vh-190px)] divide-y divide-border overflow-y-auto">
                {items.map((n, i) => (
                  <li key={i} className="px-3 py-2 hover:bg-muted/30">
                    <div className="flex items-baseline gap-2">
                      <span className="num shrink-0 text-tiny text-muted-foreground">{n.published_at}</span>
                      <Badge variant="neutral" className="shrink-0">
                        {n.source}
                      </Badge>
                      <a
                        href={n.url || undefined}
                        target="_blank"
                        rel="noreferrer"
                        className="min-w-0 flex-1 truncate text-[13px] font-medium hover:text-primary hover:underline"
                        title={n.title}
                      >
                        {n.title}
                      </a>
                      {n.url && <ExternalLink className="h-3 w-3 shrink-0 text-muted-foreground" />}
                    </div>
                    {n.content && (
                      <p className="mt-0.5 line-clamp-2 text-table text-muted-foreground">{n.content}</p>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </Panel>

      {/* 估值 + PE 历史 */}
      <div className="space-y-3">
        <MacroCards data={macro.data} err={macro.err} onRetry={macro.refetch} />
        <PeHistoryCard />
      </div>
    </div>
  );
}

function Bar({ pct, label }: { pct: number | null; label: string }) {
  const v = pct == null ? 0 : Math.max(0, Math.min(1, pct)) * 100;
  const tone = v >= 70 ? "bg-up" : v <= 30 ? "bg-down" : "bg-primary";
  return (
    <div className="flex items-center gap-2">
      <span className="w-8 shrink-0 text-[11px] text-muted-foreground">{label}</span>
      <div className="relative h-1.5 min-w-0 flex-1 bg-track">
        {/* 30% / 70% 分位刻度 */}
        <span className="absolute inset-y-0 left-[30%] w-px bg-border" />
        <span className="absolute inset-y-0 left-[70%] w-px bg-border" />
        <div className={cn("h-full", tone)} style={{ width: `${v}%` }} />
      </div>
      <span className="num w-12 shrink-0 text-right text-table">{pct == null ? "—" : v.toFixed(1) + "%"}</span>
    </div>
  );
}

function MacroCards({
  data,
  err,
  onRetry,
}: {
  data: MacroData | null;
  err: string | null;
  onRetry: () => void;
}) {
  if (err) return <ErrorBar msg={err} onRetry={onRetry} />;
  const indices = data?.indices ?? [];
  return (
    <Panel title="指数估值" caliber="PE/PB · 历史分位（0-100%） · 虚线为30%/70%刻度" bodyClassName="space-y-2.5">
      {indices.length === 0 ? (
        <EmptyState msg="暂无估值数据" reason="index_valuation 表为空（P2 估值采集未运行）" />
      ) : (
        indices.map((it) => (
          <div key={it.index_code} className="border-b border-border/60 pb-2 last:border-0 last:pb-0">
            <div className="flex items-baseline justify-between">
              <span className="text-[13px] font-medium">
                {it.name}
                <span className="num ml-1.5 text-tiny text-muted-foreground">{it.index_code}</span>
              </span>
              <span className="text-table text-muted-foreground">
                收盘 <span className="num text-foreground">{fmtNum(it.close, 2)}</span> ·{" "}
                <span className="num">{it.trade_date}</span>
              </span>
            </div>
            <div className="mt-1.5 flex items-baseline gap-3">
              <span className="num text-[18px] font-semibold leading-none">PE {fmtNum(it.pe, 2)}</span>
              <span className="num text-tiny text-muted-foreground">PB {fmtNum(it.pb, 2)}</span>
            </div>
            <div className="mt-1.5 space-y-1">
              <Bar pct={it.pe_pct} label="PE分位" />
              <Bar pct={it.pb_pct} label="PB分位" />
            </div>
          </div>
        ))
      )}
    </Panel>
  );
}

function PeHistoryCard() {
  const { dark } = useTheme();
  const [index, setIndex] = useState("000300");
  const [years, setYears] = useState("5");
  const hist = useApi(
    () => apiGet<MacroHistory>(`/api/macro_history?index=${index}&years=${years}`),
    [index, years],
  );

  const option = useMemo(() => {
    const c = chartPalette(dark);
    const d = hist.data;
    if (!d || d.dates.length === 0) return null;
    const peVals = d.pe.filter((v): v is number => v != null).sort((a, b) => a - b);
    if (peVals.length < 2) return null;
    const q = (p: number) => peVals[Math.min(peVals.length - 1, Math.floor(p * peVals.length))];
    const q30 = q(0.3);
    const q70 = q(0.7);
    return {
      animation: false,
      textStyle: { fontSize: 11, color: c.text },
      tooltip: {
        trigger: "axis",
        backgroundColor: c.tooltipBg,
        borderColor: c.tooltipBorder,
        textStyle: { color: dark ? "#e2e2de" : "#1c1c1a", fontSize: 11 },
      },
      grid: { left: 48, right: 14, top: 14, bottom: 22 },
      xAxis: {
        type: "category",
        data: d.dates,
        axisLine: { lineStyle: { color: c.line } },
        axisTick: { show: false },
        axisLabel: { color: c.text, fontSize: 10 },
      },
      yAxis: {
        type: "value",
        scale: true,
        splitLine: { lineStyle: { color: c.split } },
        axisLabel: { color: c.text, fontSize: 10 },
      },
      series: [
        {
          name: "PE",
          type: "line",
          data: d.pe,
          showSymbol: false,
          lineStyle: { width: 1.5, color: c.accent },
          itemStyle: { color: c.accent },
          markArea: {
            silent: true,
            itemStyle: { color: c.accentSoft },
            data: [[{ yAxis: q30 }, { yAxis: q70 }]],
          },
          markLine: {
            silent: true,
            symbol: "none",
            lineStyle: { color: c.gray, type: "dashed", width: 1 },
            label: { color: c.text, fontSize: 10, formatter: (p: { value: number }) => p.value.toFixed(2) },
            data: [
              { yAxis: q30, label: { formatter: `30%分位 ${q30.toFixed(2)}`, position: "insideEndTop" } },
              { yAxis: q70, label: { formatter: `70%分位 ${q70.toFixed(2)}`, position: "insideEndTop" } },
            ],
          },
        },
      ],
    };
  }, [hist.data, dark]);

  if (hist.err) return <ErrorBar msg={hist.err} onRetry={hist.refetch} />;

  return option && hist.data ? (
    <ChartCard
      title="PE 历史分位"
      caliber={
        <div className="flex items-center gap-1.5">
          <Select value={index} onValueChange={setIndex}>
            <SelectTrigger className="h-5 min-w-[92px] text-[11px]" aria-label="选择指数">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="000300">沪深300</SelectItem>
              <SelectItem value="000905">中证500</SelectItem>
              <SelectItem value="000001">上证指数</SelectItem>
            </SelectContent>
          </Select>
          <Select value={years} onValueChange={setYears}>
            <SelectTrigger className="h-5 min-w-[72px] text-[11px]" aria-label="选择年限">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {["1", "3", "5", "10"].map((y) => (
                <SelectItem key={y} value={y} className="num">
                  近 {y} 年
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      }
      footnote="口径：index_valuation 月度/日度 PE；背景带为窗口内 30%~70% 分位区间（虚线为界）。"
      height={240}
    >
      <EChart option={option} height={240} />
    </ChartCard>
  ) : (
    <Panel title="PE 历史分位" bodyClassName="p-0">
      <EmptyState msg="PE 历史数据不足" reason="窗口内估值样本过少，无法计算分位带" />
    </Panel>
  );
}
