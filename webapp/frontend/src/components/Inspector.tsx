import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { cn } from "@/lib/utils";

/* 数据/来源检查器：点击看板任意数值，右侧抽屉展示数值、来源、口径与 as-of 时刻。
   思路借鉴 GMT 终端——口径问题（昨收/现价、复权、数据源降级、池子滞后）靠
   "每个数字都能自证身份"来消灭，而不是靠事后翻日志。 */

export interface InspectInfo {
  /** 标的/指标名，如 "300750 宁德时代 · 收盘" */
  title: string;
  /** 展示值（已格式化） */
  value?: ReactNode;
  unit?: string;
  /** 数据时刻：bar 日期 / 刷新时刻 / 抓取时刻 */
  asOf?: string;
  /** 来源：表名 · 数据源 / 接口 */
  source?: string;
  /** 口径说明：复权方式、池子口径、计算方式等 */
  caliber?: string;
  /** 附加说明行 */
  extra?: string[];
}

const Ctx = createContext<{ show: (i: InspectInfo) => void }>({ show: () => {} });

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[64px_minmax(0,1fr)] gap-2 border-b border-line/60 py-1.5 text-xs">
      <span className="text-muted-foreground">{label}</span>
      <span className="min-w-0 break-words font-mono tabular-nums">{children}</span>
    </div>
  );
}

export function InspectorProvider({ children }: { children: ReactNode }) {
  const [info, setInfo] = useState<InspectInfo | null>(null);
  const show = useCallback((i: InspectInfo) => setInfo(i), []);

  useEffect(() => {
    if (!info) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setInfo(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [info]);

  return (
    <Ctx.Provider value={{ show }}>
      {children}
      {info && (
        <aside
          className="fixed inset-y-0 right-0 z-50 flex w-[300px] max-w-[85vw] flex-col
            border-l border-line bg-background shadow-lg"
          role="complementary"
          aria-label="数据与来源检查器"
        >
          <div className="flex items-center justify-between border-b border-line px-3 py-2">
            <span className="text-[13px] font-semibold">▣ 数据 / 来源检查器</span>
            <button
              type="button"
              onClick={() => setInfo(null)}
              className="rounded px-1.5 text-muted-foreground hover:bg-accent hover:text-foreground"
              title="关闭 (Esc)"
            >
              ✕
            </button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-3">
            <div className="mb-2 text-xs font-medium">{info.title}</div>
            {info.value != null && (
              <Row label="数值">
                {info.value}
                {info.unit ? <span className="ml-1 text-muted-foreground">{info.unit}</span> : null}
              </Row>
            )}
            {info.asOf != null && <Row label="as-of">{info.asOf}</Row>}
            {info.source != null && <Row label="来源">{info.source}</Row>}
            {info.caliber != null && (
              <div className="mt-2 whitespace-pre-wrap text-[11px] leading-relaxed text-muted-foreground">
                口径：{info.caliber}
              </div>
            )}
            {info.extra && info.extra.length > 0 && (
              <ul className="mt-2 space-y-1 text-[11px] text-muted-foreground">
                {info.extra.map((x, i) => (
                  <li key={i}>· {x}</li>
                ))}
              </ul>
            )}
          </div>
        </aside>
      )}
    </Ctx.Provider>
  );
}

/** 取 show 方法（在 Provider 内使用） */
export function useInspect() {
  return useContext(Ctx).show;
}

/** 可检查数值：点状下划线提示可点，点击弹出检查器（阻止冒泡，不干扰行点击） */
export function Inspect({
  info,
  children,
  className,
}: {
  info: InspectInfo;
  children: ReactNode;
  className?: string;
}) {
  const show = useInspect();
  return (
    <span
      role="button"
      tabIndex={0}
      title="点击查看来源与口径"
      onClick={(e) => {
        e.stopPropagation();
        show(info);
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          e.stopPropagation();
          show(info);
        }
      }}
      className={cn(
        "cursor-pointer border-b border-dotted border-muted-foreground/40",
        "hover:border-muted-foreground hover:bg-accent/30 focus-visible:outline-none",
        className,
      )}
    >
      {children}
    </span>
  );
}
