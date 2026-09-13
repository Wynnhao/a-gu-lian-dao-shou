import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

/**
 * 核心数字块：标签 + 大号等宽数字 + 副行（同比/超额等，带语义色）+ 口径小注。
 */
export function StatTile({
  label,
  value,
  unit,
  sub,
  subTone = "neutral",
  note,
  className,
}: {
  label: string;
  value: ReactNode;
  unit?: string;
  sub?: ReactNode;
  subTone?: "up" | "down" | "neutral" | "warn";
  note?: ReactNode;
  className?: string;
}) {
  const subCls =
    subTone === "up"
      ? "text-up"
      : subTone === "down"
        ? "text-down"
        : subTone === "warn"
          ? "text-warn"
          : "text-muted-foreground";
  return (
    <div className={cn("panel px-3 py-2.5", className)}>
      <div className="text-[12px] text-muted-foreground">{label}</div>
      <div className="mt-1 flex items-baseline gap-1">
        <span className="num text-[20px] font-semibold leading-none tracking-tight">{value}</span>
        {unit && <span className="text-[11px] text-muted-foreground">{unit}</span>}
      </div>
      <div className="mt-1.5 flex items-baseline justify-between gap-2">
        <span className={cn("truncate text-[11px] leading-none", subCls)}>{sub ?? " "}</span>
        {note != null && (
          <span className="shrink-0 text-[10px] leading-none text-muted-foreground/80">{note}</span>
        )}
      </div>
    </div>
  );
}
