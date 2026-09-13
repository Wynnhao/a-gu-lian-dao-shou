import { Moon, Pause, Play, RefreshCw, Sun } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { useLive, useRefresh } from "@/lib/refresh";
import { useTheme } from "@/lib/theme";
import { cn } from "@/lib/utils";

export type PageKey =
  | "workflow"
  | "overview"
  | "signals"
  | "groups"
  | "decisions"
  | "gate"
  | "news"
  | "reports"
  | "strategy";

export const PAGES: { key: PageKey; label: string }[] = [
  { key: "workflow", label: "决策工作流" },
  { key: "overview", label: "总览" },
  { key: "signals", label: "信号" },
  { key: "groups", label: "自选分组" },
  { key: "decisions", label: "决策" },
  { key: "gate", label: "成交与风控" },
  { key: "news", label: "新闻与宏观" },
  { key: "reports", label: "报告与日志" },
  { key: "strategy", label: "策略库" },
];

/**
 * 顶部 40px 工具条：
 * 左 = 系统名 + 模拟盘徽标；中 = 页签；右 = 数据日 + 运行状态点 + 刷新倒计时 + 手动刷新 + 主题切换。
 */
export function TopBar({
  page,
  onPage,
}: {
  page: PageKey;
  onPage: (p: PageKey) => void;
}) {
  const { overview, pending } = useLive();
  const { countdown, auto, setAuto, refreshNow, manualTick } = useRefresh();
  const { dark, toggle } = useTheme();

  const ov = overview.data;
  const issues = ov?.health_issues ?? [];
  const kill = Boolean(ov?.kill_switch_active || ov?.kill_switch);
  const status = kill
    ? { color: "bg-up", label: `熔断开启（kill_switch）`, tip: "kill_switch 已触发，禁止开新仓" }
    : issues.length > 0
      ? { color: "bg-warn", label: `${issues.length} 项数据健康告警`, tip: issues.join("；") }
      : {
          color: "bg-down",
          label: "运行正常",
          tip: "数据健康无告警，熔断未触发",
        };
  const pendingCount = pending.data?.length ?? 0;
  const busy = overview.loading || pending.loading;

  return (
    <TooltipProvider delayDuration={200}>
      <header className="flex h-10 shrink-0 items-center gap-3 border-b bg-card pl-3 pr-2">
        {/* 左：系统名 + 模拟盘徽标 */}
        <div className="flex shrink-0 items-center gap-2">
          <span className="whitespace-nowrap text-[13px] font-semibold tracking-tight">
            A股镰刀手 · AI交易员看板
          </span>
          <Badge variant="outline" className="border-primary/40 text-primary" title="PaperBroker 模拟盘（P6）">
            PAPER 模拟盘
          </Badge>
        </div>

        {/* 中：页签（下划线式，与 40px 工具条等高） */}
        <Tabs value={page} onValueChange={(v) => onPage(v as PageKey)} className="min-w-0 flex-1 self-stretch">
          <TabsList className="h-full">
            {PAGES.map((p) => (
              <TabsTrigger key={p.key} value={p.key} className="px-2.5">
                {p.label}
                {p.key === "gate" && pendingCount > 0 && (
                  <span className="num inline-flex h-[15px] min-w-[15px] items-center justify-center bg-warn/15 px-0.5 text-[10px] font-semibold leading-none text-warn">
                    {pendingCount}
                  </span>
                )}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>

        {/* 右：数据日 + 状态点 + 倒计时 + 刷新 + 主题 */}
        <div className="flex shrink-0 items-center gap-2.5">
          <span className="text-tiny text-muted-foreground">
            数据日 <span className="num text-foreground">{ov?.as_of || "—"}</span>
          </span>
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="flex cursor-default items-center gap-1.5" data-testid="run-status">
                <span className={cn("inline-block h-2 w-2 rounded-full", status.color, busy && "animate-pulse")} />
                <span className="hidden text-tiny text-muted-foreground 2xl:inline">{status.label}</span>
              </span>
            </TooltipTrigger>
            <TooltipContent side="bottom">
              <div className="font-medium">运行状态：{status.label}</div>
              <div className="mt-0.5 text-muted-foreground">{status.tip}</div>
            </TooltipContent>
          </Tooltip>

          <Tooltip>
            <TooltipTrigger asChild>
              <button
                type="button"
                onClick={() => setAuto(!auto)}
                className="flex items-center gap-1 text-tiny text-muted-foreground hover:text-foreground focus-visible:outline-none"
                data-testid="refresh-countdown"
              >
                {auto ? <Pause className="h-3 w-3" /> : <Play className="h-3 w-3" />}
                <span className="num w-7 text-right">{auto ? `${countdown}s` : "已暂停"}</span>
              </button>
            </TooltipTrigger>
            <TooltipContent side="bottom">
              30s 自动刷新轻量接口（总览/工作流/闸门）。点击{auto ? "暂停" : "恢复"}。
            </TooltipContent>
          </Tooltip>

          <Tooltip>
            <TooltipTrigger asChild>
              <Button variant="ghost" size="iconSm" onClick={refreshNow} data-testid="manual-refresh" aria-label="手动刷新">
                <RefreshCw className={cn("h-3.5 w-3.5", busy && "animate-spin")} />
              </Button>
            </TooltipTrigger>
            <TooltipContent side="bottom">手动刷新全部（key={manualTick}）</TooltipContent>
          </Tooltip>

          <Tooltip>
            <TooltipTrigger asChild>
              <Button variant="ghost" size="iconSm" onClick={toggle} aria-label="切换主题">
                {dark ? <Sun className="h-3.5 w-3.5" /> : <Moon className="h-3.5 w-3.5" />}
              </Button>
            </TooltipTrigger>
            <TooltipContent side="bottom">{dark ? "切换到浅色" : "切换到深色"}</TooltipContent>
          </Tooltip>
        </div>
      </header>
    </TooltipProvider>
  );
}
