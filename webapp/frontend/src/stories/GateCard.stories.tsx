import type { Meta, StoryObj } from "@storybook/react";
import { GateCard } from "@/components/GateCard";
import type { PendingItem } from "@/lib/api";

const meta: Meta<typeof GateCard> = {
  title: "闸门/GateCard 人工闸门卡",
  component: GateCard,
  parameters: { layout: "padded" },
  decorators: [
    (Story) => (
      <div className="w-[980px]">
        <Story />
      </div>
    ),
  ],
};

export default meta;
type Story = StoryObj<typeof GateCard>;

const base: PendingItem = {
  decision_id: 42,
  path: "logs/orders/2026-09-11/pending_42.json",
  decision: {
    id: 42,
    code: "000001",
    name: "平安银行",
    action: "buy",
    target_weight: 0.25,
    confidence: 0.78,
    reasons: [
      "MA5/20/60 多头排列，20日动量 +5.67%",
      "PE分位 28%，估值中低位",
      "近3日无负面新闻",
    ],
  },
  verdict: {
    approved: true,
    violations: [],
    warnings: ["单票权重 25% 接近上限 30%"],
  },
  confirm_hint: "确认后立即进入 PaperBroker 模拟执行（T+1）",
  reject_hint: "否决请说明理由，将写入决策流水",
  created_at: "2026-09-12T18:02:12",
};

const blocked: PendingItem = {
  ...base,
  decision_id: 43,
  path: "logs/orders/2026-09-11/pending_43.json",
  decision: {
    ...base.decision,
    id: 43,
    code: "300750",
    name: "宁德时代",
    action: "sell",
    target_weight: 0,
    confidence: 0.61,
    reasons: ["MA20 下穿 MA60，趋势转空", "RSI 25.9 超卖但动量 -16.1%"],
  },
  verdict: {
    approved: false,
    violations: ["卖出数量 200 股 > 可卖 0 股（T+1 限制）"],
    warnings: ["当日跌幅 -2.23%，接近跌停观察阈值"],
    adjusted_order: { code: "300750", side: "sell", shares: 0, note: "数量调整为 0，建议明日复核" },
  },
  created_at: "2026-09-12T18:02:13",
};

export const 风控通过待确认: Story = {
  render: () => <GateCard item={base} busy={false} onConfirm={() => {}} onReject={() => {}} />,
  name: "verdict 通过 · 可确认",
};

export const 风控拦截含违规: Story = {
  render: () => <GateCard item={blocked} busy={false} onConfirm={() => {}} onReject={() => {}} />,
  name: "verdict 未通过 · violations 红字",
};
