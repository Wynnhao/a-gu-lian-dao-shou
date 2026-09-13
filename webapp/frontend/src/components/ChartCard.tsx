import type { ReactNode } from "react";
import { Panel } from "@/components/Panel";

/**
 * 图表面板：标题 + 口径注 + 固定高度图表容器（防抖动）+ 可选脚注。
 */
export function ChartCard({
  title,
  caliber,
  footnote,
  height,
  children,
  actions,
  className,
}: {
  title: ReactNode;
  caliber?: ReactNode;
  footnote?: ReactNode;
  height: number;
  children: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <Panel
      title={title}
      caliber={caliber}
      actions={actions}
      className={className}
      bodyClassName="p-0"
    >
      <div style={{ height }} className="px-1 pt-1">
        {children}
      </div>
      {footnote && (
        <div className="border-t px-3 py-1.5 text-[10px] text-muted-foreground/80">{footnote}</div>
      )}
    </Panel>
  );
}
