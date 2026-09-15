import { useEffect, useState } from "react";
import { TopBar, type PageKey } from "@/components/layout/TopBar";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { InspectorProvider } from "@/components/Inspector";
import { LiveProvider, RefreshProvider } from "@/lib/refresh";
import { ThemeProvider } from "@/lib/theme";
import { OverviewPage, WorkflowPage } from "@/pages/WorkflowPage";
import { SignalsPage } from "@/pages/SignalsPage";
import { GroupsPage } from "@/pages/GroupsPage";
import { DecisionsPage } from "@/pages/DecisionsPage";
import { TradesGatePage } from "@/pages/TradesGatePage";
import { NewsMacroPage } from "@/pages/NewsMacroPage";
import { ReportsLogsPage } from "@/pages/ReportsLogsPage";
import { StrategyLibPage } from "@/pages/StrategyLibPage";

const VALID: PageKey[] = [
  "workflow",
  "overview",
  "signals",
  "groups",
  "decisions",
  "gate",
  "news",
  "reports",
  "strategy",
];

function pageFromHash(): PageKey {
  const h = window.location.hash.replace(/^#/, "");
  return (VALID as string[]).includes(h) ? (h as PageKey) : "workflow";
}

export default function App() {
  const [page, setPage] = useState<PageKey>(pageFromHash);

  useEffect(() => {
    const onHash = () => setPage(pageFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const goto = (p: PageKey) => {
    setPage(p);
    // hash 仅做前端定位，服务端不参与（无 SPA fallback 需要）
    if (window.location.hash.replace(/^#/, "") !== p) {
      window.history.replaceState(null, "", `#${p}`);
    }
  };

  return (
    <ThemeProvider>
      <InspectorProvider>
        <RefreshProvider>
          <LiveProvider>
            <div className="h-screen overflow-x-auto">
              <div className="flex h-screen min-w-[1100px] flex-col bg-background">
                <TopBar page={page} onPage={goto} />
                <main className="min-h-0 flex-1 overflow-y-auto p-3">
                  {/* 页面级兜底：单页渲染异常只降级该页，TopBar 仍可切换其他页 */}
                  <ErrorBoundary label="页面">
                    {page === "workflow" && <WorkflowPage />}
                    {page === "overview" && <OverviewPage onNavigate={goto} />}
                    {page === "signals" && <SignalsPage />}
                    {page === "groups" && <GroupsPage />}
                    {page === "decisions" && <DecisionsPage />}
                    {page === "gate" && <TradesGatePage />}
                    {page === "news" && <NewsMacroPage />}
                    {page === "reports" && <ReportsLogsPage />}
                    {page === "strategy" && <StrategyLibPage />}
                  </ErrorBoundary>
                </main>
              </div>
            </div>
          </LiveProvider>
        </RefreshProvider>
      </InspectorProvider>
    </ThemeProvider>
  );
}
