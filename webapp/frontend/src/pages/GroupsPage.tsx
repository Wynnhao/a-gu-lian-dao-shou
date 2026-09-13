import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/EmptyState";
import { ErrorBar } from "@/components/ErrorBar";
import { Panel } from "@/components/Panel";
import { KlineCard } from "@/pages/SignalsPage";
import { apiGet, type ConceptStock, type ConceptsData, type DynamicPools } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { useRefresh } from "@/lib/refresh";
import { fmtNum, fmtPct } from "@/lib/format";
import { cn } from "@/lib/utils";

/* 自选分组页：按概念分组展示自选池，点击行加载该票 K 线 */

function Chg({ v }: { v: number | null }) {
  if (v == null) return <span className="text-muted-foreground">—</span>;
  const cls = v > 0 ? "text-up" : v < 0 ? "text-down" : "text-muted-foreground";
  return <span className={cn("font-mono tabular-nums", cls)}>{fmtPct(v)}</span>;
}

function StockRow({ s, onSelect }: { s: ConceptStock; onSelect: () => void }) {
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
      <span className="text-right font-mono tabular-nums">{fmtNum(s.close)}</span>
      <span className="text-right">
        <Chg v={s.pct_chg} />
      </span>
      <span className="text-right font-mono tabular-nums text-muted-foreground">
        {s.score == null ? "—" : s.score.toFixed(2)}
      </span>
      <span className="text-right text-muted-foreground">
        {s.ma_trend === "up" ? "多头" : s.ma_trend === "down" ? "空头" : s.ma_trend === "flat" ? "走平" : "—"}
      </span>
    </button>
  );
}

export function GroupsPage() {
  const { manualTick } = useRefresh();
  const data = useApi(() => apiGet<ConceptsData>("/api/concepts"), [manualTick]);
  const pools = useApi(() => apiGet<DynamicPools>("/api/dynamic_pools"), [manualTick]);
  const [activeCode, setActiveCode] = useState<string | null>(null);
  const [activeName, setActiveName] = useState<string>("");

  const groups = data.data?.concepts ?? [];
  // 概念数据里补收盘价/涨跌幅（动态池行本身只存原因与强度）
  const stockMap = new Map<string, { close: number | null; pct_chg: number | null }>();
  for (const g of groups) for (const s of g.stocks) stockMap.set(s.code, s);

  return (
    <div className="flex flex-col gap-3">
      {data.err && <ErrorBar msg={data.err} onRetry={data.refetch} />}
      {pools.err && <ErrorBar msg={"动态池加载失败：" + pools.err} onRetry={pools.refetch} />}
      {!data.err && groups.length === 0 && !data.loading && (
        <Panel title="自选分组">
          <EmptyState msg="暂无分组数据 —— 检查 config.json 的 watchlist[].concepts 标签" />
        </Panel>
      )}
      {pools.data && (pools.data.movers.length > 0 || pools.data.hot_theme.length > 0 || pools.data.hot_stock.length > 0) && (
        <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
          <Panel title="异动池" caliber={<>自动筛入 · 仅观察不可交易 · 更新 {pools.data.updated_at ?? "—"}</>} bodyClassName="p-0">
            {pools.data.movers.map((m) => {
              const st = stockMap.get(m.code);
              return (
                <div key={m.code} className="border-b border-line/60 px-3 py-1.5 text-xs">
                  <div className="flex items-center justify-between gap-2">
                    <span>
                      <span className="font-mono text-muted-foreground">{m.code}</span>
                      {" "}<span className="font-medium">{m.name}</span>
                      {st?.close != null && (
                        <span className="ml-2 font-mono tabular-nums">{fmtNum(st.close)}</span>
                      )}
                      {st?.pct_chg != null && (
                        <span className={cn("ml-1.5 font-mono tabular-nums", st.pct_chg > 0 ? "text-up" : st.pct_chg < 0 ? "text-down" : "text-muted-foreground")}>
                          {fmtPct(st.pct_chg)}
                        </span>
                      )}
                    </span>
                    <span className="font-mono text-[11px] text-muted-foreground">强度 {m.strength.toFixed(1)}</span>
                  </div>
                  <div className="mt-0.5 text-[11px] text-muted-foreground">
                    {m.reasons.join(" · ")}
                  </div>
                </div>
              );
            })}
            {pools.data.movers.length === 0 && (
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
      <KlineCard code={activeCode} name={activeName} asOf="" />
    </div>
  );
}
