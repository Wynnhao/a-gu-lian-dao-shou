import { useEffect, useMemo, useState } from "react";
import { ChartCard } from "@/components/ChartCard";
import { EChart } from "@/components/EChart";
import { EmptyState } from "@/components/EmptyState";
import { ErrorBar } from "@/components/ErrorBar";
import { Panel } from "@/components/Panel";
import { SignalPanel } from "@/components/SignalPanel";
import { apiGet, type Candles, type ConceptsData, type SignalRow } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useApi } from "@/lib/hooks";
import { useRefresh } from "@/lib/refresh";
import { chartPalette, useTheme } from "@/lib/theme";

/* 信号页：左侧紧凑行卡列表，点击加载 K 线 + MA（红涨绿跌） */

export function SignalsPage() {
  const { manualTick } = useRefresh();
  const signals = useApi(() => apiGet<SignalRow[]>("/api/signals"), [manualTick]);
  const concepts = useApi(() => apiGet<ConceptsData>("/api/concepts"), []);
  const [conceptFilter, setConceptFilter] = useState<string>("全部");
  const [activeCode, setActiveCode] = useState<string | null>(null);

  const allRows = signals.data ?? [];
  const conceptOf = useMemo(() => {
    const m = new Map<string, string[]>();
    for (const g of concepts.data?.concepts ?? []) {
      for (const s of g.stocks) {
        m.set(s.code, [...(m.get(s.code) ?? []), g.name]);
      }
    }
    return m;
  }, [concepts.data]);
  const rows = conceptFilter === "全部"
    ? allRows
    : allRows.filter((r) => (conceptOf.get(r.code) ?? []).includes(conceptFilter));
  useEffect(() => {
    if (!activeCode && rows.length > 0) setActiveCode(rows[0].code);
  }, [rows, activeCode]);

  const active = rows.find((r) => r.code === activeCode) ?? null;

  return (
    <div className="grid grid-cols-1 gap-3 xl:grid-cols-[460px_minmax(0,1fr)]">
      <Panel
        title="信号列表"
        caliber={<>score 降序 · {rows.length}/{allRows.length} 票 · 点击行加载K线</>}
        bodyClassName="p-0"
        className="self-start"
      >
        {signals.err ? (
          <div className="p-3">
            <ErrorBar msg={signals.err} onRetry={signals.refetch} />
          </div>
        ) : (
          <>
          <div className="flex flex-wrap gap-1 border-b border-line px-3 py-2">
            {["全部", ...(concepts.data?.concepts ?? []).map((g) => g.name)].map((name) => (
              <button key={name}
                onClick={() => setConceptFilter(name)}
                className={cn(
                  "rounded px-2 py-0.5 text-[11px] border",
                  conceptFilter === name
                    ? "border-accent bg-accent/10 text-accent font-medium"
                    : "border-line text-muted-foreground hover:text-foreground"
                )}>
                {name}
              </button>
            ))}
          </div>
          <SignalPanel
            rows={rows}
            activeCode={activeCode}
            onSelect={(c) => setActiveCode(c)}
            className="max-h-[calc(100vh-180px)] overflow-y-auto"
          />
          </>
        )}
      </Panel>

      <KlineCard code={activeCode} name={active?.name ?? ""} asOf={active?.as_of ?? ""} />
    </div>
  );
}

export function KlineCard({ code, name, asOf }: { code: string | null; name: string; asOf: string }) {
  const { dark } = useTheme();
  const { manualTick } = useRefresh();
  const candles = useApi(
    () => (code ? apiGet<Candles>(`/api/candles?code=${encodeURIComponent(code)}&days=120`) : Promise.resolve(null)),
    [code, manualTick],
    { enabled: Boolean(code) },
  );

  const option = useMemo(() => {
    const c = chartPalette(dark);
    const d = candles.data;
    if (!d || d.dates.length === 0) return null;
    const volColor = d.kline.open.map((o, i) =>
      d.kline.close[i] >= o ? c.up : c.down,
    );
    return {
      animation: false,
      textStyle: { fontSize: 11, color: c.text },
      axisPointer: { link: [{ xAxisIndex: "all" }], lineStyle: { color: c.line } },
      tooltip: {
        trigger: "axis",
        backgroundColor: c.tooltipBg,
        borderColor: c.tooltipBorder,
        textStyle: { color: dark ? "#e2e2de" : "#1c1c1a", fontSize: 11 },
        axisPointer: { type: "cross", label: { backgroundColor: c.accent } },
        formatter: (params: unknown) => {
          const arr = params as { axisIndex: number; seriesName?: string; value: unknown; name: string }[];
          const k = arr.find((p) => p.seriesName === "日K");
          if (!k) return "";
          const v = k.value as (number | string)[];
          const [o, cl, lo, hi, vol] = [v[1], v[2], v[3], v[4], v[5]];
          const up = Number(cl) >= Number(o);
          const colorCls = up ? c.up : c.down;
          return [
            `<b>${k.name}</b>`,
            `开 <span style="color:${colorCls}">${o}</span>　收 <span style="color:${colorCls}">${cl}</span>`,
            `低 ${lo}　高 ${hi}`,
            vol != null ? `量 ${Number(vol).toLocaleString("zh-CN")}` : "",
          ]
            .filter(Boolean)
            .join("<br/>");
        },
      },
      grid: [
        { left: 60, right: 14, top: 10, height: "58%" },
        { left: 60, right: 14, top: "74%", height: "18%" },
      ],
      xAxis: [
        {
          type: "category",
          gridIndex: 0,
          data: d.dates,
          axisLine: { lineStyle: { color: c.line } },
          axisTick: { show: false },
          axisLabel: { show: false },
        },
        {
          type: "category",
          gridIndex: 1,
          data: d.dates,
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
          axisLabel: { color: c.text, fontSize: 10 },
        },
        {
          gridIndex: 1,
          splitLine: { show: false },
          axisLabel: {
            color: c.textDim,
            fontSize: 10,
            formatter: (v: number) => (v >= 10000 ? (v / 10000).toFixed(0) + "万" : String(v)),
          },
        },
      ],
      series: [
        {
          name: "日K",
          type: "candlestick",
          xAxisIndex: 0,
          yAxisIndex: 0,
          data: d.kline.open.map((o, i) => [o, d.kline.close[i], d.kline.low[i], d.kline.high[i], d.kline.volume[i]]),
          itemStyle: {
            color: c.up,
            color0: c.down,
            borderColor: c.up,
            borderColor0: c.down,
          },
        },
        { name: "MA5", type: "line", xAxisIndex: 0, yAxisIndex: 0, data: d.ma5, showSymbol: false, lineStyle: { width: 1, color: c.accent } },
        { name: "MA20", type: "line", xAxisIndex: 0, yAxisIndex: 0, data: d.ma20, showSymbol: false, lineStyle: { width: 1, color: c.gray } },
        { name: "MA60", type: "line", xAxisIndex: 0, yAxisIndex: 0, data: d.ma60, showSymbol: false, lineStyle: { width: 1, color: c.warn, type: "dashed" } },
        {
          name: "成交量",
          type: "bar",
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: d.kline.volume,
          itemStyle: { color: volColor },
          barWidth: "60%",
        },
      ],
    };
  }, [candles.data, dark]);

  return (
    <div className="space-y-3">
      {candles.err && <ErrorBar msg={candles.err} onRetry={candles.refetch} />}
      {option && candles.data ? (
        <ChartCard
          title={
            <>
              {candles.data.name || name}
              <span className="num ml-1.5 text-tiny text-muted-foreground">{candles.data.code || code}</span>
            </>
          }
          caliber={<>日K · 120 交易日 · MA5/20/60（头部预热裁剪） · as_of <span className="num">{asOf || "—"}</span></>}
          footnote="红=收≥开，绿=收<开（A股口径）；副图为成交量。"
          height={430}
        >
          <EChart option={option} height={430} />
        </ChartCard>
      ) : (
        <Panel title="K线" caliber={code ? `加载中…` : "未选择标的"} bodyClassName="p-0">
          <EmptyState
            msg={code ? "K线数据加载中…" : "未选择标的"}
            reason={code ? `GET /api/candles?code=${code}` : "在左侧信号列表点击任一标的"}
          />
        </Panel>
      )}
    </div>
  );
}
