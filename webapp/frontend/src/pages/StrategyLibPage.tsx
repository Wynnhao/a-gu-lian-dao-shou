import { EmptyState } from "@/components/EmptyState";
import { ErrorBar } from "@/components/ErrorBar";
import { MarkdownView } from "@/components/MarkdownView";
import { Panel } from "@/components/Panel";
import { apiGet, type DocResp } from "@/lib/api";
import { useApi } from "@/lib/hooks";

/* 策略库页：渲染 /api/doc?name=strategy_lib；missing → 「策略研究进行中」占位 */

export function StrategyLibPage() {
  const doc = useApi(() => apiGet<DocResp>("/api/doc?name=strategy_lib"), []);

  return (
    <Panel
      title="策略库"
      caliber="docs/策略库.md · 因子/规则/参数档案"
      bodyClassName="p-0"
    >
      {doc.err ? (
        <div className="p-3">
          <ErrorBar msg={doc.err} onRetry={doc.refetch} />
        </div>
      ) : doc.data?.missing ? (
        <EmptyState
          msg="策略研究进行中"
          reason="docs/策略库.md 尚未建立——因子定义、参数与回测档案整理完成后归档于此"
        />
      ) : doc.data?.markdown ? (
        <div className="max-h-[calc(100vh-140px)] overflow-y-auto px-4 py-3">
          <MarkdownView markdown={doc.data.markdown} />
        </div>
      ) : (
        <EmptyState msg="加载中…" />
      )}
    </Panel>
  );
}
