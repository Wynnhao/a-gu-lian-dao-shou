import type { Meta, StoryObj } from "@storybook/react";
import { SignalPanel } from "@/components/SignalPanel";
import type { SignalRow } from "@/lib/api";

const meta: Meta<typeof SignalPanel> = {
  title: "盯盘/SignalPanel 信号面板",
  component: SignalPanel,
  parameters: { layout: "padded" },
};

export default meta;
type Story = StoryObj<typeof SignalPanel>;

const rows: SignalRow[] = [
  {
    code: "601318",
    name: "中国平安",
    as_of: "2026-09-11",
    signals: {
      ma_trend: "up", ma5: 55.73, ma20: 55.15, ma60: 52.81,
      rsi_14: 48.99, mom_20d: 0.0595, turnover_pct: 0.3,
      close: 54.83, pct_chg: -1.24, above_ma60: true,
    },
    score: 0.8794,
  },
  {
    code: "000001",
    name: "平安银行",
    as_of: "2026-09-11",
    signals: {
      ma_trend: "up", ma5: 11.75, ma20: 11.62, ma60: 11.11,
      rsi_14: 57.13, mom_20d: 0.0567, turnover_pct: 0.39,
      close: 11.74, pct_chg: -0.93, above_ma60: true,
    },
    score: 0.8756,
  },
  {
    code: "600519",
    name: "贵州茅台",
    as_of: "2026-09-11",
    signals: {
      ma_trend: "flat", ma5: 1295.3, ma20: 1298.32, ma60: 1278.01,
      rsi_14: 41.76, mom_20d: -0.0498, turnover_pct: 0.24,
      close: 1275.16, pct_chg: -0.78, above_ma60: false,
    },
    score: 0.5586,
  },
  {
    code: "300750",
    name: "宁德时代",
    as_of: "2026-09-11",
    signals: {
      ma_trend: "down", ma5: 345.2, ma20: 362.4, ma60: 388.5,
      rsi_14: 25.9, mom_20d: -0.161, turnover_pct: 0.68,
      close: 330.51, pct_chg: -2.23, above_ma60: false,
    },
    score: 0.0,
  },
];

export const 四票Score降序: Story = {
  name: "score 降序 · 选中第一行",
  render: () => (
    <div className="w-[480px] border bg-card">
      <SignalPanel rows={rows} activeCode="601318" onSelect={() => {}} />
    </div>
  ),
};

export const 空状态: Story = {
  name: "无信号（空态含原因）",
  render: () => (
    <div className="w-[480px] border bg-card">
      <SignalPanel rows={[]} />
    </div>
  ),
};
