import { ChevronDown, ChevronRight } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  TRADE_STATUS_LABEL,
  actionLabel,
  fmtPct,
  fmtTs,
  fmtTsFull,
  unknown,
} from "@/lib/format";
import type { Decision, Trade } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Fragment, useState, type ReactNode } from "react";

/** 决策状态徽章：executed绿 / approved蓝(藏青) / proposed黄 / rejected红 / report_only灰 */
export function DecisionStatusBadge({ status }: { status: string }) {
  const map: Record<string, { variant: "success" | "navy" | "warn" | "destructive" | "neutral"; label: string }> = {
    executed: { variant: "success", label: "已执行" },
    approved: { variant: "navy", label: "已批准" },
    proposed: { variant: "warn", label: "待裁决" },
    rejected: { variant: "destructive", label: "已否决" },
    report_only: { variant: "neutral", label: "仅报告" },
  };
  const it = map[status] ?? { variant: "neutral" as const, label: status };
  return (
    <Badge variant={it.variant} title={status}>
      {it.label}
    </Badge>
  );
}

export function TradeSideBadge({ side }: { side: string }) {
  // A股语义：买=红，卖=绿
  if (side === "buy") return <Badge variant="destructive">买入</Badge>;
  if (side === "sell") return <Badge variant="success">卖出</Badge>;
  return <Badge variant="neutral">{side}</Badge>;
}

export function TradeStatusBadge({ status }: { status: string | null | undefined }) {
  if (!status) return <span className="text-muted-foreground">—</span>;
  const variant =
    status === "filled" ? "success" : status === "rejected" ? "destructive" : "neutral";
  return (
    <Badge variant={variant} title={status}>
      {unknown(TRADE_STATUS_LABEL, status)}
    </Badge>
  );
}

/** 成交结果小块（决策追踪链展开区复用） */
export function TradeMiniCard({ trade }: { trade: Trade | Pick<Trade, "id" | "side" | "price" | "shares" | "amount" | "status" | "confirmed_by"> | null }) {
  if (!trade) return <span className="text-tiny text-muted-foreground">无成交记录</span>;
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-table">
      <span className="flex items-center gap-1.5">
        成交 <span className="num">#{trade.id}</span> <TradeSideBadge side={trade.side} />
      </span>
      <span>
        价格 <span className="num">{trade.price}</span> × <span className="num">{trade.shares}</span> 股 ={" "}
        <span className="num">{Number(trade.amount ?? 0).toLocaleString("zh-CN", { minimumFractionDigits: 2 })}</span> 元
      </span>
      <TradeStatusBadge status={trade.status} />
      {trade.confirmed_by && <span className="text-tiny text-muted-foreground">确认人：{trade.confirmed_by}</span>}
    </div>
  );
}

export interface DecisionRow extends Decision {
  /** 工作流追踪链附加信息（决策页不含） */
  risk_events?: { ts: string; rule: string; detail: string }[];
  trade?: Pick<Trade, "id" | "side" | "price" | "shares" | "amount" | "status" | "confirmed_by"> | null;
  pending?: boolean;
}

/**
 * 决策表：行展开显示 reasons / risk_notes / 风控事件时间线 / 成交结果 / pending 标记。
 * 决策页与工作流追踪链共用。
 */
export function DecisionTable({
  rows,
  showRunDate = true,
  extraColumns,
  onViewBundle,
}: {
  rows: DecisionRow[];
  showRunDate?: boolean;
  extraColumns?: { head: ReactNode; cell: (r: DecisionRow) => ReactNode }[];
  /** 传入时展开区显示"查看当时输入包"跳转（决策→证据归因） */
  onViewBundle?: (date: string) => void;
}) {
  const [open, setOpen] = useState<Set<number>>(new Set());
  const toggle = (id: number) =>
    setOpen((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  const colCount = 8 + (showRunDate ? 1 : 0) + (extraColumns?.length ?? 0);

  if (rows.length === 0) {
    return (
      <div className="px-3 py-8 text-center">
        <p className="text-[13px] text-muted-foreground">暂无决策记录</p>
        <p className="text-[11px] text-muted-foreground/70">P5 决策环节尚未产出（无 LLM 决策或当日未跑流水线）</p>
      </div>
    );
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="w-6" />
          <TableHead className="w-14 text-right">ID</TableHead>
          {showRunDate && <TableHead>运行日</TableHead>}
          <TableHead>标的</TableHead>
          <TableHead>动作</TableHead>
          <TableHead className="text-right">目标权重</TableHead>
          <TableHead className="text-right">置信度</TableHead>
          <TableHead>状态</TableHead>
          {extraColumns?.map((c, i) => (
            <TableHead key={i}>{c.head}</TableHead>
          ))}
          <TableHead className="text-right">生成时间</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((r) => {
          const expanded = open.has(r.id);
          return (
            <Fragment key={r.id}>
              <TableRow className={cn("cursor-pointer", expanded && "bg-muted/40")} onClick={() => toggle(r.id)}>
                <TableCell className="w-6 px-1 text-muted-foreground">
                  {expanded ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
                </TableCell>
                <TableCell className="num text-right">{r.id}</TableCell>
                {showRunDate && <TableCell className="num">{r.run_date}</TableCell>}
                <TableCell className="whitespace-nowrap">
                  <span className="font-medium">{r.name}</span>{" "}
                  <span className="num text-tiny text-muted-foreground">{r.code}</span>
                </TableCell>
                <TableCell>{actionLabel(r.action)}</TableCell>
                <TableCell className="num text-right">{fmtPct(r.target_weight * 100)}</TableCell>
                <TableCell className="num text-right">{r.confidence.toFixed(2)}</TableCell>
                <TableCell>
                  <div className="flex items-center gap-1.5">
                    <DecisionStatusBadge status={r.status} />
                    {r.pending && <Badge variant="warn">待人工确认</Badge>}
                  </div>
                </TableCell>
                {extraColumns?.map((c, i) => (
                  <TableCell key={i}>{c.cell(r)}</TableCell>
                ))}
                <TableCell className="num whitespace-nowrap text-right text-tiny text-muted-foreground" title={fmtTsFull(r.created_at)}>
                  {fmtTs(r.created_at)}
                </TableCell>
              </TableRow>
              {expanded && (
                <TableRow className="hover:bg-transparent">
                  <TableCell colSpan={colCount} className="bg-muted/20 px-3 py-2.5">
                    <div className="grid grid-cols-1 gap-x-8 gap-y-2 lg:grid-cols-2">
                      <div>
                        <div className="mb-1 text-[11px] font-semibold text-muted-foreground">决策理由（reasons）</div>
                        {r.reasons?.length ? (
                          <ul className="list-disc space-y-0.5 pl-4 text-table">
                            {r.reasons.map((x, i) => (
                              <li key={i}>{x}</li>
                            ))}
                          </ul>
                        ) : (
                          <p className="text-table text-muted-foreground">未提供理由</p>
                        )}
                      </div>
                      <div>
                        <div className="mb-1 text-[11px] font-semibold text-muted-foreground">风险提示（risk_notes）</div>
                        {r.risk_notes?.length ? (
                          <ul className="list-disc space-y-0.5 pl-4 text-table text-warn">
                            {r.risk_notes.map((x, i) => (
                              <li key={i}>{x}</li>
                            ))}
                          </ul>
                        ) : (
                          <p className="text-table text-muted-foreground">无</p>
                        )}
                      </div>
                      {r.risk_events && r.risk_events.length > 0 && (
                        <div>
                          <div className="mb-1 text-[11px] font-semibold text-muted-foreground">风控事件时间线（risk_events）</div>
                          <ol className="space-y-1.5 border-l border-border pl-3 text-table">
                            {r.risk_events.map((e, i) => (
                              <li key={i} className="relative">
                                <span className="absolute -left-[17px] top-[6px] h-1.5 w-1.5 rounded-full bg-warn" />
                                <span className="num text-tiny text-muted-foreground">{fmtTs(e.ts)}</span>{" "}
                                <span className="num font-medium">{e.rule}</span>
                                <span className="text-muted-foreground"> — {e.detail}</span>
                              </li>
                            ))}
                          </ol>
                        </div>
                      )}
                      {r.trade !== undefined && (
                        <div>
                          <div className="mb-1 text-[11px] font-semibold text-muted-foreground">执行结果（trade）</div>
                          <TradeMiniCard trade={r.trade} />
                        </div>
                      )}
                      {onViewBundle && r.run_date && (
                        <div className="lg:col-span-2">
                          <button
                            type="button"
                            onClick={() => onViewBundle(r.run_date)}
                            className="text-[11px] text-primary underline-offset-2 hover:underline"
                          >
                            查看 {r.run_date} 决策时的完整输入包（bundle）→
                          </button>
                        </div>
                      )}
                    </div>
                  </TableCell>
                </TableRow>
              )}
            </Fragment>
          );
        })}
      </TableBody>
    </Table>
  );
}
