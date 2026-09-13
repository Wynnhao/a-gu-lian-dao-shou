import { useMemo, useState } from "react";
import { DecisionTable } from "@/components/DecisionTable";
import { EmptyState } from "@/components/EmptyState";
import { ErrorBar } from "@/components/ErrorBar";
import { MarkdownView } from "@/components/MarkdownView";
import { Panel } from "@/components/Panel";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { apiGet, type Decision, type SessionContent, type SessionInfo } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { useRefresh } from "@/lib/refresh";

/* 决策页：决策流水表 + 顶部「决策输入包」（bundle.md）选择器 */

export function DecisionsPage() {
  const { manualTick } = useRefresh();
  const decisions = useApi(
    () => apiGet<Decision[]>("/api/decisions?limit=50"),
    [manualTick],
  );
  const sessions = useApi(() => apiGet<SessionInfo[]>("/api/sessions"), []);

  const bundleDates = useMemo(
    () => (sessions.data ?? []).filter((s) => s.has_bundle).map((s) => s.date),
    [sessions.data],
  );
  // 手选优先；未手选时默认最新一个含 bundle 的会话日
  const [picked, setPicked] = useState<string | null>(null);
  const bundleDate = picked ?? bundleDates[0] ?? null;

  return (
    <div className="space-y-3">
      {/* 决策输入包 */}
      <Panel
        title="决策输入包"
        caliber="LLM 决策依据 · bundle.md（信号+新闻+宏观+账户）"
        actions={
          bundleDates.length > 0 ? (
            <Select value={bundleDate ?? undefined} onValueChange={setPicked}>
              <SelectTrigger className="h-6 min-w-[140px]" aria-label="选择输入包日期">
                <SelectValue placeholder="选择日期" />
              </SelectTrigger>
              <SelectContent>
                {bundleDates.map((d) => (
                  <SelectItem key={d} value={d} className="num">
                    {d}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : undefined
        }
        bodyClassName="p-0"
      >
        <BundleBody date={bundleDate} />
      </Panel>

      {/* 决策流水 */}
      <Panel
        title="决策流水"
        caliber="最近 50 条 · 状态徽章：executed绿 / approved蓝 / proposed黄 / rejected红 / report_only灰"
        bodyClassName="p-0"
      >
        {decisions.err ? (
          <div className="p-3">
            <ErrorBar msg={decisions.err} onRetry={decisions.refetch} />
          </div>
        ) : (
          <DecisionTable rows={decisions.data ?? []} />
        )}
      </Panel>
    </div>
  );
}

function BundleBody({ date }: { date: string | null }) {
  const session = useApi(
    () =>
      date
        ? apiGet<SessionContent>(
            `/api/session?date=${encodeURIComponent(date)}&kind=bundle_md`,
          )
        : Promise.resolve(null),
    [date],
    { enabled: Boolean(date) },
  );

  if (!date) {
    return (
      <EmptyState
        msg="暂无决策输入包"
        reason="logs/session/ 下没有含 bundle.md 的会话目录（P5 输入包组装未运行）"
      />
    );
  }
  if (session.err) {
    return (
      <div className="p-3">
        <ErrorBar msg={session.err} onRetry={session.refetch} />
      </div>
    );
  }
  if (session.data?.content) {
    return (
      <div className="max-h-[380px] overflow-y-auto px-3 py-2.5">
        <MarkdownView markdown={session.data.content} />
      </div>
    );
  }
  return <EmptyState msg="输入包加载中…" reason={date} />;
}
