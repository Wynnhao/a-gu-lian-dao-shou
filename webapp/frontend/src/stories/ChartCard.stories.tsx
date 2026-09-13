import type { Meta, StoryObj } from "@storybook/react";
import { ChartCard } from "@/components/ChartCard";
import { EChart, type ECOption } from "@/components/EChart";
import { useMemo, useState } from "react";

const meta = {
  title: "图表/ChartCard 图表面板",
  component: ChartCard,
  parameters: { layout: "padded" },
} satisfies Meta<typeof ChartCard>;

export default meta;
type Story = StoryObj<typeof ChartCard>;

function DemoOption(dark: boolean): ECOption {
  const up = dark ? "#e0524e" : "#c2312e";
  const down = dark ? "#4caf7d" : "#1a7f4b";
  const accent = dark ? "#8fb0d9" : "#1f3a5f";
  const split = dark ? "#26292e" : "#ececea";
  const text = dark ? "#9a9a94" : "#5c5c57";
  const dates = ["09-05", "09-08", "09-09", "09-10", "09-11", "09-12"];
  return {
    animation: false,
    textStyle: { fontSize: 11, color: text },
    grid: { left: 56, right: 12, top: 10, bottom: 22 },
    xAxis: {
      type: "category",
      data: dates,
      axisLine: { lineStyle: { color: split } },
      axisLabel: { color: text, fontSize: 10 },
    },
    yAxis: {
      type: "value",
      scale: true,
      splitLine: { lineStyle: { color: split } },
      axisLabel: { color: text, fontSize: 10 },
    },
    series: [
      {
        name: "组合",
        type: "line",
        data: [100, 100.4, 99.8, 101.2, 100.9, 102.3],
        showSymbol: false,
        lineStyle: { width: 1.5, color: accent },
        itemStyle: { color: accent },
        markPoint: undefined,
        areaStyle: undefined,
        color: up,
        color0: down,
      },
    ],
  };
}

export const 权益曲线示意: Story = {
  name: "面板 + 固定高度图表（浅色）",
  render: () => {
    const [dark] = useState(false);
    const option = useMemo(() => DemoOption(dark), [dark]);
    return (
      <div className="w-[760px]">
        <ChartCard
          title="权益曲线"
          caliber={<>组合 vs 沪深300 · 归一至期初 · <span className="num">6</span> 个交易日</>}
          footnote="口径：portfolio_state 逐日盯市净值；基准为 index_daily 沪深300 收盘，同起点归一。"
          height={280}
        >
          <EChart option={option} height={280} />
        </ChartCard>
      </div>
    );
  },
};

export const 深色主题: Story = {
  name: "深色 tokens 下的图表",
  render: () => {
    const [dark, setDark] = useState(true);
    const option = useMemo(() => DemoOption(dark), [dark]);
    return (
      <div className={dark ? "dark w-[760px]" : "w-[760px]"}>
        <div className={dark ? "bg-[#141619] p-2" : "p-2"}>
          <button
            type="button"
            onClick={() => setDark(!dark)}
            className="mb-2 border px-2 py-0.5 text-[12px]"
          >
            切换主题（当前：{dark ? "深色" : "浅色"}）
          </button>
          <ChartCard
            title="权益曲线"
            caliber={<>组合 vs 沪深300 · 归一至期初</>}
            height={280}
          >
            <EChart option={option} height={280} />
          </ChartCard>
        </div>
      </div>
    );
  },
};
