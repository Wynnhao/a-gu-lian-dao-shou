import { useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/EmptyState";
import { ErrorBar } from "@/components/ErrorBar";
import { Panel } from "@/components/Panel";
import { Inspect } from "@/components/Inspector";
import { EChart } from "@/components/EChart";
import { KlineCard } from "@/pages/SignalsPage";
import { apiGet, type ConceptGroup, type ConceptStock, type ConceptsData, type DynamicPools } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { useRefresh } from "@/lib/refresh";
import { chartPalette, useTheme } from "@/lib/theme";
import { fmtNum, fmtPct } from "@/lib/format";
import { cn } from "@/lib/utils";

/* 自选分组页：按概念分组展示自选池，点击行加载该票 K 线。
   视图：列表（分组表格）/ 热力图（面积=成交额、颜色=涨跌幅、黑名单置灰）。
   数值均接检查器：点击看来源/口径/as-of。 */

function Chg({ v }: { v: number | null }) {
  if (v == null) return <span className="text-muted-foreground">—</span>;
  const cls = v > 0 ? "text-up" : v < 0 ? "text-down" : "text-muted-foreground";
  return <span className={cn("font-mono tabular-nums", cls)}>{fmtPct(v)}</span>;
}

function StockRow({ s, onSelect }: { s: ConceptStock; onSelect: () => void }) {
  const basis = `daily_bar${s.source ? " · " + s.source : ""}`;
  return (
    <button
      onClick={onSelect}
      className="grid w-full grid-cols-[64px_minmax(0,1fr)_72px_72px_64px_56px] items-center gap-1
        border-b border-line/60 px-3 py-[5px] text-left text-xs hover:bg-accent/5"
      title={s.blacklisted ? `黑名单：${s.blacklist_reason}` : undefined}
    >
      <span className="font-mono text-muted-foreground">{s.code}</span>
      <span className="truncate font-medium">
        {s.name}
        {s.blacklisted && (
          <Badge variant="destructive" className="ml-1.5 px-1 py-0 text-[10px]">
            禁交易
          </Badge>
        )}
      </span>
      <span className="text-right font-mono tabular-nums">
        <Inspect
          info={{
            title: `${s.code} ${s.name} · 收盘`,
            value: s.close == null ? "—" : fmtNum(s.close),
            asOf: s.bar_date ?? "—",
            source: basis,
            caliber: "日线收盘价（不复权）；涨跌幅为官方口径 pct_chg",
          }}
        >
          {s.close == null ? "—" : fmtNum(s.close)}
        </Inspect>
      </span>
      <span className="text-right">
        <Chg v={s.pct_chg} />
      </span>
      <span className="text-right font-mono tabular-nums text-muted-foreground">
        <Inspect
          info={{
            title: `${s.code} ${s.name} · score`,
            value: s.score == null ? "—" : s.score.toFixed(2),
            asOf: s.bar_date ?? "—",
            source: "signal 表",
            caliber: "当日截面 rank 归一分（profile 见 config.signals.profile，默认 reversal_lowvol）",
          }}
        >
          {s.score == null ? "—" : s.score.toFixed(2)}
        </Inspect>
      </span>
      <span className="text-right text-muted-foreground">
        {s.ma_trend === "up" ? "多头" : s.ma_trend === "down" ? "空头" : s.ma_trend === "flat" ? "走平" : "—"}
      </span>
    </button>
  );
}

/** 强度色阶：涨跌从近底色渐变到红/绿（A 股口径红涨绿跌），±6% 封顶 */
function heatColor(pct: number | null, dark: boolean): string {
  const c = chartPalette(dark);
  if (pct == null) return dark ? "#2e2e2b" : "#e9e9e6";
  const base = dark ? 40 : 244;
  const t = Math.min(Math.abs(pct), 6) / 6; // 0..1 强度
  const mix = dark ? 0.18 + 0.82 * t : 0.06 + 0.94 * t;
  const hex = pct >= 0 ? c.up : c.down;
  const rgb = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
  const ch = rgb.map((v) => Math.round(base + (v - base) * mix));
  return `rgb(${ch[0]},${ch[1]},${ch[2]})`;
}

/** 自选池热力图：分组 treemap，面积=成交额（缺额等权），颜色=涨跌幅，点击色块看K线 */
function WatchHeatmap({
  groups,
  dark,
  onSelect,
}: {
  groups: ConceptGroup[];
  dark: boolean;
  onSelect: (code: string, name: string) => void;
}) {
  const option = useMemo(() => {
    const c = chartPalette(dark);
    const bg = dark ? "#1c1c1a" : "#ffffff";
    const data = groups.map((g) => ({
      name: g.name,
      children: g.stocks.map((s) => ({
        name: s.code,
        code: s.code,
        stockName: s.name,
        pct: s.pct_chg,
        blacklisted: s.blacklisted,
        barDate: s.bar_date,
        stock: { code: s.code, name: s.name },
        value: s.amount && s.amount > 0 ? s.amount : 1,
        itemStyle: { color: s.blacklisted ? (dark ? "#33332f" : "#d8d8d3") : heatColor(s.pct_chg, dark) },
        label: { color: s.blacklisted || s.pct_chg == null ? (dark ? "#9a9a94" : "#5c5c57") : "#ffffff" },
      })),
    }));
    return {
      backgroundColor: "transparent",
      tooltip: {
        formatter: (p: { data?: Record<string, unknown> }) => {
          const d = p.data ?? {};
          if (!d.code) return String(p.data?.["name"] ?? "");
          return [
            `${d.code} ${d.stockName}`,
            `涨跌 ${d.pct == null ? "—" : fmtPct(d.pct as number)}`,
            `成交额 ${d.value === 1 ? "—" : fmtNum(d.value as number)}`,
            `行情日 ${d.barDate ?? "—"}`,
            d.blacklisted ? "黑名单 · 禁交易" : "点击色块看 K 线",
          ].join("\n");
        },
        backgroundColor: c.tooltipBg,
        borderColor: c.tooltipBorder,
        textStyle: { color: dark ? "#e2e2de" : "#1c1c1a", fontSize: 11 },
      },
      series: [
        {
          type: "treemap",
          roam: false,
          nodeClick: false,
          breadcrumb: { show: false },
          left: 0,
          right: 0,
          top: 0,
          bottom: 0,
          itemStyle: { borderColor: bg, borderWidth: 2, gapWidth: 2 },
          upperLabel: { show: true, height: 20, color: c.text, fontSize: 11, fontWeight: 600 },
          label: {
            show: true,
            formatter: (p: { data?: Record<string, unknown>; name: string }) => {
              const d = p.data ?? {};
              if (!d.code) return ""; // 组节点走 upperLabel
              return `${d.code} ${d.stockName}\n${d.pct == null ? "—" : fmtPct(d.pct as number)}`;
            },
            fontSize: 10,
            lineHeight: 14,
          },
          levels: [
            {
              itemStyle: { borderColor: bg, borderWidth: 0, gapWidth: 5 },
              upperLabel: { show: true, height: 20, color: c.text, fontSize: 11, fontWeight: 600 },
            },
            {
              itemStyle: { borderColor: bg, borderWidth: 2, gapWidth: 2 },
            },
          ],
          data,
        },
      ],
    };
  }, [groups, dark]);

  return (
    <EChart
      option={option}
      height={460}
      onEvents={{
        click: (params) => {
          const d = (params as { data?: { stock?: { code: string; name: string } } }).data;
          if (d?.stock) onSelect(d.stock.code, d.stock.name);
        },
      }}
    />
  );
}

export function GroupsPage() {
  const { manualTick } = useRefresh();
  const { dark } = useTheme();
  const data = useApi(() => apiGet<ConceptsData>("/api/concepts"), [manualTick]);
  const pools = useApi(() => apiGet<DynamicPools>("/api/dynamic_pools"), [manualTick]);
  const [activeCode, setActiveCode] = useState<string | null>(null);
  const [activeName, setActiveName] = useState<string>("");
  const [view, setView] = useState<"list" | "heat">("list");

  const groups = data.data?.concepts ?? [];
  // 概念数据里补名称/收盘价/涨跌幅（动态池行本身只存原因与强度）
  const stockMap = new Map<string, { name: string; close: number | null; pct_chg: number | null }>();
  for (const g of groups) for (const s of g.stocks) stockMap.set(s.code, s);

  const movers = pools.data?.movers ?? [];
  const moversMode = movers[0]?.mode ?? "";
  const poolCaliber =
    moversMode === "all"
      ? "全市场快照口径（量比/涨幅/成交额，强度上限约 3.5）"
      : "自选/日线兜底口径（五规则，强度上限约 7.2）——两口径强度不可比";

  return (
    <div className="flex flex-col gap-3">
      {data.err && <ErrorBar msg={data.err} onRetry={data.refetch} />}
      {pools.err && <ErrorBar msg={"动态池加载失败：" + pools.err} onRetry={pools.refetch} />}
      {!data.err && groups.length === 0 && !data.loading && (
        <Panel title="自选分组">
          <EmptyState msg="暂无分组数据 —— 检查 config.json 的 watchlist[].concepts 标签" />
        </Panel>
      )}
      {pools.data && (movers.length > 0 || pools.data.hot_theme.length > 0 || pools.data.hot_stock.length > 0) && (
        <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
          <Panel
            title="异动池"
            caliber={
              <>
                {moversMode && (
                  <span className="mr-1 rounded border border-line px-1 py-px text-[10px]">
                    {moversMode === "all" ? "全市场口径" : "自选池口径"}
                  </span>
                )}
                自动筛入 · 仅观察不可交易 · 更新 {pools.data.updated_at ?? "—"}
              </>
            }
            bodyClassName="p-0"
          >
            {/* 单行紧凑行 + 双列排布：涨幅原因与涨跌幅列重复故省略，完整原因悬停可见 */}
            <div className="grid grid-cols-1 md:grid-cols-2 md:gap-x-4">
              {movers.map((m) => {
                const st = stockMap.get(m.code);
                const name = st?.name && st.name !== m.code ? st.name : m.name;
                const extra = m.reasons.filter((r) => !r.startsWith("涨幅"));
                return (
                  <div
                    key={m.code}
                    className="flex min-w-0 items-baseline gap-1.5 border-b border-line/60 px-3 py-[5px] text-xs"
                    title={`${m.code} ${name}｜${m.reasons.join(" · ")}`}
                  >
                    <span className="w-[50px] shrink-0 font-mono text-muted-foreground">{m.code}</span>
                    <span className="min-w-0 flex-1 truncate font-medium">{name}</span>
                    {st?.close != null && (
                      <span className="shrink-0 font-mono tabular-nums">{fmtNum(st.close)}</span>
                    )}
                    <span className="w-[52px] shrink-0 text-right">
                      <Chg v={st?.pct_chg ?? null} />
                    </span>
                    <span className="min-w-0 flex-[1.3] truncate text-[11px] text-muted-foreground">
                      {extra.length > 0 ? extra.join(" · ") : "—"}
                    </span>
                    <span className="w-[30px] shrink-0 text-right font-mono text-[11px] text-muted-foreground">
                      <Inspect
                        info={{
                          title: `${m.code} ${name} · 强度`,
                          value: m.strength.toFixed(1),
                          asOf: m.added_date,
                          source: "dynamic_pool 表",
                          caliber: poolCaliber,
                        }}
                      >
                        {m.strength.toFixed(1)}
                      </Inspect>
                    </span>
                  </div>
                );
              })}
            </div>
            {movers.length === 0 && (
              <div className="px-3 py-3 text-xs text-muted-foreground">当前无异动票（阈值见 config.pools.movers）</div>
            )}
          </Panel>
          <Panel title="热门池" caliber={<>题材热度 + 个股新闻突增 · 仅观察不可交易</>} bodyClassName="p-0">
            {pools.data.hot_theme.map((t) => (
              <div key={t.code} className="border-b border-line/60 px-3 py-1.5 text-xs">
                <div className="flex items-center justify-between">
                  <span className="font-medium">{t.name}</span>
                  <span className="font-mono text-[11px] text-muted-foreground">热度 {t.strength.toFixed(0)}</span>
                </div>
                <div className="mt-0.5 text-[11px] text-muted-foreground">{(t.reasons[0] ?? "")}{t.reasons[1] ? ` · ${t.reasons[1]}` : ""}</div>
              </div>
            ))}
            {pools.data.hot_stock.map((h) => (
              <div key={h.code} className="border-b border-line/60 px-3 py-1.5 text-xs">
                <div className="flex items-center justify-between">
                  <span>
                    <span className="font-mono text-muted-foreground">{h.code}</span>
                    {" "}<span className="font-medium">{h.name}</span>
                    <span className="ml-1.5 text-[10px] text-muted-foreground">新闻突增</span>
                  </span>
                  <span className="font-mono text-[11px] text-muted-foreground">×{h.strength.toFixed(1)}</span>
                </div>
                <div className="mt-0.5 text-[11px] text-muted-foreground">{(h.reasons[0] ?? "")}</div>
              </div>
            ))}
            {pools.data.hot_theme.length === 0 && pools.data.hot_stock.length === 0 && (
              <div className="px-3 py-3 text-xs text-muted-foreground">当前无热门题材/个股</div>
            )}
            {pools.data.boards.length > 0 && (
              <div className="px-3 py-2 text-[11px] text-muted-foreground">
                板块榜：{pools.data.boards.slice(0, 5).map((b) => `${b.board} ${fmtPct(b.pct_chg)}`).join(" · ")}
              </div>
            )}
          </Panel>
        </div>
      )}
      {/* 视图切换 */}
      <div className="flex items-center gap-2">
        <span className="text-[11px] text-muted-foreground">视图</span>
        {(["list", "heat"] as const).map((v) => (
          <button
            key={v}
            type="button"
            aria-pressed={view === v}
            onClick={() => setView(v)}
            className={cn(
              "rounded border px-2 py-0.5 text-[11px] hover:bg-accent/10",
              view === v ? "border-primary text-primary" : "border-line text-muted-foreground",
            )}
          >
            {v === "list" ? "≣ 列表" : "▦ 热力图"}
          </button>
        ))}
        {view === "heat" && (
          <span className="text-[11px] text-muted-foreground">
            面积=成交额 · 颜色=涨跌幅（红涨绿跌，±6% 封顶）· 黑名单置灰 · 点击色块看 K 线
          </span>
        )}
      </div>
      {view === "heat" ? (
        <Panel title="自选池热力图" caliber={<> {data.data?.total ?? "—"} 只按题材组聚合 · 行情日 {groups[0]?.stocks[0]?.bar_date ?? "—"}</>}>
          <WatchHeatmap
            groups={groups}
            dark={dark}
            onSelect={(code, name) => {
              setActiveCode(code);
              setActiveName(name);
            }}
          />
        </Panel>
      ) : (
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2 xl:grid-cols-3">
          {groups.map((g) => (
            <Panel
              key={g.name}
              title={g.name}
              caliber={<>{g.stocks.length} 只 · 点击行看K线</>}
              bodyClassName="p-0"
            >
              <div className="grid grid-cols-[64px_minmax(0,1fr)_72px_72px_64px_56px] gap-1
                border-b border-line px-3 py-1.5 text-[11px] text-muted-foreground">
                <span>代码</span>
                <span>名称</span>
                <span className="text-right">收盘</span>
                <span className="text-right">涨跌幅</span>
                <span className="text-right">score</span>
                <span className="text-right">趋势</span>
              </div>
              {g.stocks.map((s) => (
                <StockRow key={g.name + s.code} s={s}
                  onSelect={() => { setActiveCode(s.code); setActiveName(s.name); }} />
              ))}
            </Panel>
          ))}
        </div>
      )}
      <KlineCard code={activeCode} name={activeName} asOf="" />
    </div>
  );
}
