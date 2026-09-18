import { useState } from "react";
import { AlertTriangle, ShieldAlert, ShieldCheck } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { actionLabel, fmtPct, fmtTsFull } from "@/lib/format";
import type { PendingItem } from "@/lib/api";

/**
 * 人工闸门卡：pending 单详情 + 风控裁决（violations 红字）+ 确认/否决操作。
 * POST /api/confirm|reject 后由父组件弹出 runner 输出。
 */
export function GateCard({
  item,
  busy,
  onConfirm,
  onReject,
}: {
  item: PendingItem;
  busy: boolean;
  onConfirm: (decisionId: number, by: string) => void;
  onReject: (decisionId: number, reason: string, by: string) => void;
}) {
  const [by, setBy] = useState("");
  const [rejectOpen, setRejectOpen] = useState(false);
  const [reason, setReason] = useState("");
  const d = item.decision ?? {};
  const v = item.verdict ?? {};
  const violations: string[] = Array.isArray(v.violations) ? v.violations : [];
  const warnings: string[] = Array.isArray(v.warnings) ? v.warnings : [];
  const code = String(d.code ?? "—");
  const name = String(d.name ?? "");
  const reasons: string[] = Array.isArray(d.reasons) ? (d.reasons as string[]) : [];

  return (
    <div className="panel">
      <div className="flex flex-wrap items-center gap-2 border-b px-3 py-2">
        <Badge variant="warn">
          <ShieldAlert className="h-3 w-3" />
          待人工确认
        </Badge>
        <span className="text-[13px] font-semibold">
          决策 <span className="num">#{String(item.decision_id ?? d.id ?? "—")}</span>
        </span>
        <span>
          {name} <span className="num text-tiny text-muted-foreground">{code}</span>
        </span>
        <Badge variant="outline">{actionLabel(String(d.action ?? ""))}</Badge>
        <span className="text-table text-muted-foreground">
          目标权重{" "}
          {d.action === "buy" ? (
            <span className="num text-foreground">{fmtPct(Number(d.target_weight ?? 0) * 100)}</span>
          ) : (
            <span
              className="text-muted-foreground"
              title="卖出/观察/持有不设目标权重（仅建仓有仓位比例）"
            >
              —
            </span>
          )}
          {" · "}置信度 <span className="num text-foreground">{Number(d.confidence ?? 0).toFixed(2)}</span>
        </span>
        <span className="num ml-auto text-tiny text-muted-foreground" title={item.created_at}>
          {fmtTsFull(item.created_at)}
        </span>
      </div>

      <div className="grid grid-cols-1 gap-3 px-3 py-2.5 lg:grid-cols-[1fr_360px]">
        <div className="min-w-0 space-y-2">
          <div>
            <div className="mb-1 text-[11px] font-semibold text-muted-foreground">决策理由</div>
            {reasons.length ? (
              <ul className="list-disc space-y-0.5 pl-4 text-table">
                {reasons.map((r, i) => (
                  <li key={i}>{r}</li>
                ))}
              </ul>
            ) : (
              <p className="text-table text-muted-foreground">未提供理由</p>
            )}
          </div>
          {item.path && (
            <div className="num truncate text-[10px] text-muted-foreground/80" title={item.path}>
              工单：{item.path}
            </div>
          )}
        </div>

        <div className="space-y-1.5 border-border lg:border-l lg:pl-3">
          <div className="flex items-center gap-1.5 text-[11px] font-semibold text-muted-foreground">
            风控裁决（确认时重跑）
            {v.approved ? (
              <Badge variant="success">
                <ShieldCheck className="h-3 w-3" />
                通过
              </Badge>
            ) : (
              <Badge variant="destructive">
                <ShieldAlert className="h-3 w-3" />
                未通过
              </Badge>
            )}
          </div>
          {violations.length > 0 && (
            <ul className="space-y-0.5 text-table text-up">
              {violations.map((x, i) => (
                <li key={i} className="flex gap-1">
                  <AlertTriangle className="mt-[3px] h-3 w-3 shrink-0" />
                  <span>{x}</span>
                </li>
              ))}
            </ul>
          )}
          {warnings.length > 0 && (
            <ul className="space-y-0.5 text-table text-warn">
              {warnings.map((x, i) => (
                <li key={i}>· {x}</li>
              ))}
            </ul>
          )}
          {violations.length === 0 && warnings.length === 0 && (
            <p className="text-table text-muted-foreground">无违规、无警告</p>
          )}
          {v.adjusted_order != null && (
            <details className="text-table">
              <summary className="cursor-pointer text-muted-foreground">调整后委托（adjusted_order）</summary>
              <pre className="num mt-1 max-h-32 overflow-auto border bg-muted/60 p-1.5 text-[11px]">
                {JSON.stringify(v.adjusted_order, null, 2)}
              </pre>
            </details>
          )}
          {item.confirm_hint && (
            <p className="text-tiny text-muted-foreground">{item.confirm_hint}</p>
          )}
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-end gap-2 border-t px-3 py-2">
        <label className="flex items-center gap-1.5 text-tiny text-muted-foreground">
          操作人
          <Input
            value={by}
            onChange={(e) => setBy(e.target.value)}
            placeholder="姓名/ID"
            className="h-6 w-28 text-[12px]"
          />
        </label>
        <Button
          size="sm"
          disabled={busy}
          onClick={() => onConfirm(Number(item.decision_id), by.trim())}
        >
          确认执行
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={busy}
          onClick={() => {
            setReason("");
            setRejectOpen(true);
          }}
        >
          否决…
        </Button>
      </div>

      {/* 否决 Dialog：理由必填 */}
      <Dialog open={rejectOpen} onOpenChange={setRejectOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>
              否决决策 <span className="num">#{String(item.decision_id)}</span>
            </DialogTitle>
            <DialogDescription>
              否决必须填写理由，理由将写入决策流水并留痕（reject_hint：
              {item.reject_hint || "无"}）。
            </DialogDescription>
          </DialogHeader>
          <textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            rows={3}
            placeholder="否决理由（必填，如：估值分位过高，暂不建仓）"
            className="w-full resize-none border bg-transparent p-2 text-[13px] focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />
          <div className="flex items-center gap-1.5 text-tiny text-muted-foreground">
            操作人
            <Input value={by} onChange={(e) => setBy(e.target.value)} placeholder="姓名/ID" className="h-6 w-28 text-[12px]" />
          </div>
          <DialogFooter>
            <Button size="sm" variant="ghost" onClick={() => setRejectOpen(false)}>
              取消
            </Button>
            <Button
              size="sm"
              variant="destructive"
              disabled={!reason.trim() || busy}
              onClick={() => {
                onReject(Number(item.decision_id), reason.trim(), by.trim());
                setRejectOpen(false);
              }}
            >
              确认否决
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
