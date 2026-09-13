import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useApi } from "./hooks";
import { apiGet, type Overview, type PendingItem, type Workflow } from "./api";

/* ---------------------------------------------------------------
   全局刷新：30s 自动轮询轻量接口（overview / workflow / pending），
   倒计时在工具条展示；手动刷新立即重拉并归零倒计时。
   manualTick 供重量级页面（信号/决策/新闻等）响应「手动刷新」。
---------------------------------------------------------------- */

const REFRESH_SECONDS = 30;

interface RefreshCtx {
  /** 30s 自动轮询 tick（含手动刷新触发） */
  autoTick: number;
  /** 仅手动刷新触发 */
  manualTick: number;
  countdown: number;
  auto: boolean;
  setAuto: (v: boolean) => void;
  refreshNow: () => void;
}

const RCtx = createContext<RefreshCtx>({
  autoTick: 0,
  manualTick: 0,
  countdown: REFRESH_SECONDS,
  auto: true,
  setAuto: () => {},
  refreshNow: () => {},
});

export function RefreshProvider({ children }: { children: ReactNode }) {
  const [autoTick, setAutoTick] = useState(0);
  const [manualTick, setManualTick] = useState(0);
  const [countdown, setCountdown] = useState(REFRESH_SECONDS);
  const [auto, setAuto] = useState(true);
  const autoRef = useRef(auto);
  autoRef.current = auto;

  useEffect(() => {
    const id = setInterval(() => {
      if (!autoRef.current) return;
      setCountdown((c) => {
        if (c <= 1) {
          setAutoTick((t) => t + 1);
          return REFRESH_SECONDS;
        }
        return c - 1;
      });
    }, 1000);
    return () => clearInterval(id);
  }, []);

  const refreshNow = useCallback(() => {
    setCountdown(REFRESH_SECONDS);
    setAutoTick((t) => t + 1);
    setManualTick((t) => t + 1);
  }, []);

  const value = useMemo<RefreshCtx>(
    () => ({ autoTick, manualTick, countdown, auto, setAuto, refreshNow }),
    [autoTick, manualTick, countdown, auto, refreshNow],
  );
  return <RCtx.Provider value={value}>{children}</RCtx.Provider>;
}

export function useRefresh() {
  return useContext(RCtx);
}

/* ---------------------------------------------------------------
   实时数据：overview / workflow / pending 三个轻量接口全局共享，
   TopBar（数据日/状态点/待确认角标）与相关页面共用，避免重复请求。
---------------------------------------------------------------- */

interface LiveCtx {
  overview: ReturnType<typeof useApi<Overview>>;
  workflow: ReturnType<typeof useApi<Workflow>>;
  pending: ReturnType<typeof useApi<PendingItem[]>>;
  /** 闸门操作后重拉全部轻量接口 */
  refetchAll: () => void;
}

const LCtx = createContext<LiveCtx | null>(null);

export function LiveProvider({ children }: { children: ReactNode }) {
  const { autoTick } = useRefresh();
  const overview = useApi<Overview>(() => apiGet<Overview>("/api/overview"), [], { tick: autoTick });
  const workflow = useApi<Workflow>(() => apiGet<Workflow>("/api/workflow"), [], { tick: autoTick });
  const pending = useApi<PendingItem[]>(() => apiGet<PendingItem[]>("/api/pending"), [], {
    tick: autoTick,
  });

  const refetchAll = useCallback(() => {
    overview.refetch();
    workflow.refetch();
    pending.refetch();
  }, [overview, workflow, pending]);

  const value = useMemo<LiveCtx>(
    () => ({ overview, workflow, pending, refetchAll }),
    [overview, workflow, pending, refetchAll],
  );
  return <LCtx.Provider value={value}>{children}</LCtx.Provider>;
}

export function useLive(): LiveCtx {
  const v = useContext(LCtx);
  if (!v) throw new Error("useLive 必须在 LiveProvider 内使用");
  return v;
}
