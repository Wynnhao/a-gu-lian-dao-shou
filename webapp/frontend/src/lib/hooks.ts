import { useCallback, useEffect, useRef, useState } from "react";

export interface Loadable<T> {
  data: T | null;
  err: string | null;
  loading: boolean;
  refetch: () => void;
}

/**
 * 轻量数据 hook：
 * - fetcher 每次渲染都是新引用也没关系（内部用 ref 固定取最新）；
 * - deps / opts.tick 变化时重新拉取（tick 用于全局 30s 轮询与手动刷新）；
 * - opts.enabled=false 时不请求（如依赖参数未就绪）。
 */
export function useApi<T>(
  fetcher: () => Promise<T>,
  deps: unknown[] = [],
  opts: { tick?: unknown; enabled?: boolean } = {},
): Loadable<T> {
  const fnRef = useRef(fetcher);
  fnRef.current = fetcher;
  const [data, setData] = useState<T | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // 首次之外的 refetch 调用序号，用于强制 rerun
  const [seq, setSeq] = useState(0);

  const refetch = useCallback(() => setSeq((s) => s + 1), []);

  useEffect(() => {
    if (opts.enabled === false) return;
    let alive = true;
    setLoading(true);
    fnRef
      .current()
      .then((d) => {
        if (!alive) return;
        setData(d);
        setErr(null);
      })
      .catch((e: unknown) => {
        if (!alive) return;
        setErr(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seq, opts.enabled, opts.tick, ...deps]);

  return { data, err, loading, refetch };
}
