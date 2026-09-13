import { AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui/button";

/** 行内错误条：fetch 失败时的轻量提示 + 重试 */
export function ErrorBar({ msg, onRetry }: { msg: string; onRetry?: () => void }) {
  return (
    <div className="flex items-center gap-2 border border-up/30 bg-up/5 px-2.5 py-1.5 text-[12px] text-up">
      <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
      <span className="min-w-0 flex-1 truncate" title={msg}>
        加载失败：{msg}
      </span>
      {onRetry && (
        <Button variant="outline" size="sm" className="h-5 px-1.5" onClick={onRetry}>
          重试
        </Button>
      )}
    </div>
  );
}
