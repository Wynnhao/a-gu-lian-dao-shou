import type { Meta, StoryObj } from "@storybook/react";
import { DecisionTable } from "@/components/DecisionTable";
import type { DecisionRow } from "@/components/DecisionTable";

const meta: Meta<typeof DecisionTable> = {
  title: "流水/DecisionTable 决策表",
  component: DecisionTable,
  parameters: { layout: "padded" },
};

export default meta;
type Story = StoryObj<typeof DecisionTable>;

const rows: DecisionRow[] = [
  {
    id: 41,
    run_date: "2026-09-11",
    code: "601318",
    name: "中国平安",
    action: "hold",
    target_weight: 0.3,
    confidence: 0.82,
    status: "executed",
    reasons: ["多头排列未破坏", "股息率支撑", "换手率正常"],
    risk_notes: [],
    created_at: "2026-09-12T18:02:08",
    risk_events: [
      { ts: "2026-09-12T18:02:11", rule: "R01_max_weight", detail: "目标权重 30% ≤ 上限 30%，通过" },
      { ts: "2026-09-12T18:02:11", rule: "R07_t_plus_1", detail: "卖出校验通过" },
    ],
    trade: { id: 7, side: "sell", price: 54.83, shares: 100, amount: 5477.75, status: "filled", confirmed_by: "zhang" },
    pending: false,
  },
  {
    id: 42,
    run_date: "2026-09-11",
    code: "000001",
    name: "平安银行",
    action: "buy",
    target_weight: 0.25,
    confidence: 0.78,
    status: "approved",
    reasons: ["MA 多头", "20日动量 +5.67%", "PE分位 28%"],
    risk_notes: ["单票权重接近上限"],
    created_at: "2026-09-12T18:02:09",
    trade: null,
    pending: true,
  },
  {
    id: 43,
    run_date: "2026-09-11",
    code: "300750",
    name: "宁德时代",
    action: "sell",
    target_weight: 0,
    confidence: 0.61,
    status: "rejected",
    reasons: ["趋势转空", "动量 -16.1%"],
    risk_notes: ["触发 T+1 可卖数量校验"],
    created_at: "2026-09-12T18:02:10",
  },
  {
    id: 44,
    run_date: "2026-09-11",
    code: "600519",
    name: "贵州茅台",
    action: "watch",
    target_weight: 0.1,
    confidence: 0.45,
    status: "report_only",
    reasons: ["MA 走平，方向不明"],
    risk_notes: [],
    created_at: "2026-09-12T18:02:10",
  },
];

export const 四种状态含追踪链: Story = {
  name: "executed/approved/rejected/report_only + 展开信息",
  render: () => (
    <div className="w-[1100px] border bg-card">
      <DecisionTable rows={rows} showRunDate={false} />
    </div>
  ),
  parameters: {
    docs: {
      description: {
        story: "点击行展开：决策理由 / 风险提示 / 风控事件时间线 / 成交结果 / 待人工确认标记。",
      },
    },
  },
};

export const 空状态: Story = {
  name: "无决策记录（空态含原因）",
  render: () => (
    <div className="w-[800px] border bg-card">
      <DecisionTable rows={[]} />
    </div>
  ),
};
