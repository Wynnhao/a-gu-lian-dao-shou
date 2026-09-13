import { useEffect, useRef } from "react";
import * as echarts from "echarts/core";
import {
  BarChart,
  CandlestickChart,
  LineChart,
} from "echarts/charts";
import {
  GridComponent,
  LegendComponent,
  MarkAreaComponent,
  MarkLineComponent,
  TooltipComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { cn } from "@/lib/utils";

echarts.use([
  LineChart,
  CandlestickChart,
  BarChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  MarkAreaComponent,
  MarkLineComponent,
  CanvasRenderer,
]);

/** 宽松 option 类型：option 由调用方按 echarts 语法构造 */
export type ECOption = Record<string, unknown>;

/**
 * ECharts 轻封装：容器固定高度（防抖动），ResizeObserver 自适应宽度，
 * option 变化时 notMerge 全量替换。
 */
export function EChart({
  option,
  height,
  className,
}: {
  option: ECOption;
  height: number;
  className?: string;
}) {
  const boxRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);

  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const chart = echarts.init(el);
    chartRef.current = chart;
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(el);
    return () => {
      ro.disconnect();
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    chartRef.current?.setOption(option as echarts.EChartsCoreOption, true);
  }, [option]);

  return <div ref={boxRef} className={cn("w-full", className)} style={{ height }} />;
}
