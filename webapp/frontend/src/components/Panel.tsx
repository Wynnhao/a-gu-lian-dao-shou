import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

/** 面板：1px 发丝线 + 白底。头部分：标题（13px 加粗）+ 右侧口径注 */
export function Panel({
  title,
  caliber,
  actions,
  children,
  className,
  bodyClassName,
}: {
  title?: ReactNode;
  caliber?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section className={cn("panel", className)}>
      {(title || caliber || actions) && (
        <header className="panel-head">
          <div className="flex min-w-0 items-center gap-2">
            <h2 className="panel-title truncate">{title}</h2>
            {actions}
          </div>
          {caliber != null && <div className="caliber truncate pl-3">{caliber}</div>}
        </header>
      )}
      <div className={cn("p-3", bodyClassName)}>{children}</div>
    </section>
  );
}

/** 面板头内联版（自带 header 的自由布局用） */
export function PanelHead({ title, caliber }: { title: ReactNode; caliber?: ReactNode }) {
  return (
    <div className="flex items-baseline gap-2">
      <span className="text-[13px] font-semibold">{title}</span>
      {caliber != null && <span className="caliber">{caliber}</span>}
    </div>
  );
}
