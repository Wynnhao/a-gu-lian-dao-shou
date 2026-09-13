import { Fragment } from "react";
import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { STAGE_STATUS_LABEL, fmtTs, unknown } from "@/lib/format";
import type { WorkflowStage } from "@/lib/api";
import { cn } from "@/lib/utils";

export const STAGE_DOT: Record<string, string> = {
  ok: "bg-down",
  warn: "bg-warn",
  fail: "bg-up",
  idle: "bg-idle/50",
};

export function StageDot({ status, className }: { status: string; className?: string }) {
  return (
    <span
      className={cn(
        "inline-block h-2 w-2 shrink-0 rounded-full",
        STAGE_DOT[status] ?? "bg-idle/50",
        className,
      )}
    />
  );
}

/**
 * 十段流水线横向 stepper：状态点 + 名称 + detail 小字 + 时间戳，
 * hover Tooltip 展示 desc 全文。
 */
export function PipelineFlow({ stages, className }: { stages: WorkflowStage[]; className?: string }) {
  return (
    <TooltipProvider delayDuration={150}>
      <div className={cn("flex items-stretch overflow-x-auto", className)}>
        {stages.map((s, i) => (
          <Fragment key={s.id}>
            {i > 0 && <div className="mt-auto mb-[52px] h-px w-3 shrink-0 bg-border" />}
            <Tooltip>
              <TooltipTrigger asChild>
                <div className="min-w-[104px] flex-1 cursor-default px-1.5 py-1.5 hover:bg-muted/50">
                  <div className="flex items-center gap-1.5">
                    <StageDot status={s.status} />
                    <span className="whitespace-nowrap text-[12px] font-medium leading-none">
                      {i + 1}. {s.name}
                    </span>
                  </div>
                  <div className="mt-1.5 truncate text-tiny leading-none text-muted-foreground" title={s.detail}>
                    {s.detail}
                  </div>
                  <div className="num mt-1 text-[10px] leading-none text-muted-foreground/80">
                    {fmtTs(s.ts)}
                  </div>
                </div>
              </TooltipTrigger>
              <TooltipContent side="bottom">
                <div className="flex items-center gap-1.5 font-medium">
                  <StageDot status={s.status} />
                  {s.name}
                  <Badge variant="neutral" className="ml-1">
                    {unknown(STAGE_STATUS_LABEL, s.status)}
                  </Badge>
                </div>
                <div className="mt-1 text-muted-foreground">{s.desc}</div>
                <div className="mt-0.5">{s.detail}</div>
                <div className="num mt-0.5 text-muted-foreground">时间戳：{fmtTs(s.ts)}</div>
              </TooltipContent>
            </Tooltip>
          </Fragment>
        ))}
      </div>
    </TooltipProvider>
  );
}
