import { Component, type ErrorInfo, type ReactNode } from "react";

/**
 * 渲染期异常兜底（Phase 5 前端加固）。
 *
 * 此前无任何 ErrorBoundary：API 的 reasons/risk_notes 等字段经 parse_json_field
 * 动态解析后直接进渲染，畸形 payload（非数组/意外形状）触发渲染期异常即全站白屏。
 *
 * 用法双层：main.tsx 顶层包住 <App />（最后防线），App.tsx 页面级包住路由出口
 * （单页崩溃只降级该页，TopBar 仍可切换）。
 */
type Props = { children: ReactNode; label?: string };
type State = { error: Error | null };

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // 渲染期异常留痕到控制台，便于从浏览器排查是哪个 payload 引起
    console.error("[ErrorBoundary]%s 渲染异常:", this.props.label ?? "",
      error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="flex h-full min-h-[240px] flex-col items-center justify-center gap-3 rounded-lg border border-destructive/30 bg-destructive/5 p-6 text-center">
        <div className="text-sm font-semibold text-destructive">
          {this.props.label ?? "页面"}渲染出错
        </div>
        <pre className="max-w-[640px] overflow-auto whitespace-pre-wrap break-all text-left text-xs text-muted-foreground">
          {error.message}
        </pre>
        <button
          className="rounded-md border bg-background px-3 py-1.5 text-sm hover:bg-muted"
          onClick={() => window.location.reload()}
        >
          刷新页面
        </button>
      </div>
    );
  }
}
