import { useState } from "react";
import { TradeSideBadge, TradeStatusBadge } from "@/components/DecisionTable";
import { EmptyState } from "@/components/EmptyState";
import { ErrorBar } from "@/components/ErrorBar";
import { GateCard } from "@/components/GateCard";
import { Panel } from "@/components/Panel";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { apiGet, apiPost, type ConfirmResp, type PendingItem, type RiskEvent, type Trade } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { useLive, useRefresh } from "@/lib/refresh";
import { fmtInt, fmtMoney, fmtTs, fmtTsFull } from "@/lib/format";

/* 成交与风控页：人工闸门置顶 + 成交表 + 风控事件表 */

export function TradesGatePage() {
  const { pending } = useLive();
  const { manualTick, refreshNow } = useRefresh();
  const trades = useApi(() => apiGet<Trade[]>("/api/trades?limit=50"), [manualTick]);
  const riskEvents = useApi(() => apiGet<RiskEvent[]>("/api/risk_events?limit=50"), [manualTick]);

  const [busy, setBusy] = useState(false);
  const [postErr, setPostErr] = useState<string | null>(null);
  const [result, setResult] = useState<{ title: string; resp: ConfirmResp } | null>(null);

  async function submit(fn: () => Promise<ConfirmResp>, title: string) {
    setBusy(true);
    setPostErr(null);
    try {
      const resp = await fn();
      setResult({ title, resp });
      // POST 落地后立即重拉轻量接口（闸门/工作流/总览）与成交/风控表
      refreshNow();
    } catch (e) {
      setPostErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const onConfirm = (decisionId: number, by: string) =>
    submit(
      () => apiPost<ConfirmResp>("/api/confirm", { decision_id: decisionId, by: by || "human" }),
      `确认执行 · 决策 #${decisionId}`,
    );
  const onReject = (decisionId: number, reason: string, by: string) =>
    submit(
      () =>
        apiPost<ConfirmResp>("/api/reject", {
          decision_id: decisionId,
          reason,
          by: by || "human",
        }),
      `否决 · 决策 #${decisionId}`,
    );

  const pend = pending.data ?? [];

  return (
    <div className="space-y-3">
      {/* 人工闸门（置顶） */}
      <section className="space-y-2">
        <div className="flex items-baseline gap-2">
          <h2 className="text-[13px] font-semibold">人工闸门</h2>
          <span className="caliber">
            pending 工单确认后进入执行 · confirm 时重跑 15 条硬规则 · 唯一写操作
          </span>
        </div>
        {postErr && <ErrorBar msg={postErr} onRetry={() => setPostErr(null)} />}
        {pend.length === 0 ? (
          <Panel bodyClassName="p-0">
            <EmptyState msg="无待确认单——闸门空闲" reason="当日决策均已裁决，或流水线未产出 pending 工单" />
          </Panel>
        ) : (
          pend.map((p) => (
            <GateCard key={p.path} item={p} busy={busy} onConfirm={onConfirm} onReject={onReject} />
          ))
        )}
      </section>

      <div className="grid grid-cols-1 gap-3 2xl:grid-cols-2">
        {/* 成交表 */}
        <Panel title="成交流水" caliber="最近 50 笔 · PaperBroker（T+1，含佣金/印花税）" bodyClassName="p-0">
          {trades.err ? (
            <div className="p-3">
              <ErrorBar msg={trades.err} onRetry={trades.refetch} />
            </div>
          ) : (trades.data ?? []).length === 0 ? (
            <EmptyState msg="无成交记录" reason="尚无已确认执行的决策（P9 模拟执行未发生）" />
          ) : (
            <TradeTable rows={trades.data ?? []} />
          )}
        </Panel>

        {/* 风控事件表 */}
        <Panel title="风控事件" caliber="15 条硬规则裁决留痕 · 最近 50 条" bodyClassName="p-0">
          {riskEvents.err ? (
            <div className="p-3">
              <ErrorBar msg={riskEvents.err} onRetry={riskEvents.refetch} />
            </div>
          ) : (riskEvents.data ?? []).length === 0 ? (
            <EmptyState msg="无风控事件" reason="当日无决策送审，或全部通过硬规则（P7 规则裁决未拦截）" />
          ) : (
            <RiskEventTable rows={riskEvents.data ?? []} />
          )}
        </Panel>
      </div>

      {/* runner 输出 Dialog */}
      <Dialog open={result !== null} onOpenChange={(o) => !o && setResult(null)}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>
              Runner 输出 · {result?.title}
              {result && (
                <span className="ml-2 align-middle">
                  {result.resp.ok ? (
                    <span className="text-[12px] font-medium text-down">执行成功（exit 0）</span>
                  ) : (
                    <span className="text-[12px] font-medium text-up">执行失败（exit 非 0）</span>
                  )}
                </span>
              )}
            </DialogTitle>
            <DialogDescription>execution/runner.py CLI 原文，已同步刷新闸门与工作流。</DialogDescription>
          </DialogHeader>
          <pre className="num max-h-[420px] overflow-auto border bg-muted/60 p-3 text-[11px] leading-[1.6] whitespace-pre-wrap">
            {result?.resp.output ?? ""}
          </pre>
          <DialogFooter>
            <Button size="sm" onClick={() => setResult(null)}>
              关闭
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function TradeTable({ rows }: { rows: Trade[] }) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="text-right">ID</TableHead>
          <TableHead>日期</TableHead>
          <TableHead>标的</TableHead>
          <TableHead>方向</TableHead>
          <TableHead className="text-right">价格</TableHead>
          <TableHead className="text-right">数量</TableHead>
          <TableHead className="text-right">金额</TableHead>
          <TableHead>状态</TableHead>
          <TableHead className="text-right">决策</TableHead>
          <TableHead>确认人</TableHead>
          <TableHead className="text-right">时间</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((t) => (
          <TableRow key={t.id}>
            <TableCell className="num text-right">{t.id}</TableCell>
            <TableCell className="num">{t.trade_date}</TableCell>
            <TableCell className="whitespace-nowrap">
              {t.name} <span className="num text-tiny text-muted-foreground">{t.code}</span>
            </TableCell>
            <TableCell>
              <TradeSideBadge side={t.side} />
            </TableCell>
            <TableCell className="num text-right">{fmtMoney(t.price)}</TableCell>
            <TableCell className="num text-right">{fmtInt(t.shares)}</TableCell>
            <TableCell className="num text-right">{fmtMoney(t.amount)}</TableCell>
            <TableCell>
              <TradeStatusBadge status={t.status} />
            </TableCell>
            <TableCell className="num text-right">{t.decision_id ?? "—"}</TableCell>
            <TableCell className="text-tiny text-muted-foreground">{t.confirmed_by ?? "—"}</TableCell>
            <TableCell className="num whitespace-nowrap text-right text-tiny text-muted-foreground" title={fmtTsFull(t.created_at)}>
              {fmtTs(t.created_at)}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

function RiskEventTable({ rows }: { rows: RiskEvent[] }) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="text-right">ID</TableHead>
          <TableHead>时间</TableHead>
          <TableHead>规则</TableHead>
          <TableHead>明细</TableHead>
          <TableHead className="text-right">决策</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((r) => (
          <TableRow key={r.id}>
            <TableCell className="num text-right">{r.id}</TableCell>
            <TableCell className="num whitespace-nowrap text-tiny">{fmtTs(r.ts)}</TableCell>
            <TableCell className="num font-medium">{r.rule}</TableCell>
            <TableCell className="min-w-[200px] text-muted-foreground">{r.detail}</TableCell>
            <TableCell className="num text-right">{r.decision_id ?? "—"}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
