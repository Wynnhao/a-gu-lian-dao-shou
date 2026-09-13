import type { Meta, StoryObj } from "@storybook/react";
import { StatTile } from "@/components/StatTile";

const meta: Meta<typeof StatTile> = {
  title: "盯盘/StatTile 核心数字块",
  component: StatTile,
  parameters: { layout: "padded" },
};

export default meta;
type Story = StoryObj<typeof StatTile>;

export const 盯市组合四件套: Story = {
  render: () => (
    <div className="grid w-[900px] grid-cols-4 gap-3">
      <StatTile label="总资产" value="1,000,000.00" unit="元" sub="期初 1,000,000" note="盯市 · 最新收盘" />
      <StatTile label="现金" value="438,902.10" unit="元" sub="可用资金（PaperBroker）" note="未含在途委托" />
      <StatTile label="持仓市值" value="561,097.90" unit="元" sub="2 只持仓" note="行情日 2026-09-11" />
      <StatTile
        label="累计收益"
        value="+0.00%"
        sub={
          <>
            超额 <span className="text-up">+0.00%</span> vs 沪深300
          </>
        }
        subTone="up"
        note="相对基准同起点"
      />
    </div>
  ),
};

export const 盈亏语义色: Story = {
  render: () => (
    <div className="grid w-[680px] grid-cols-3 gap-3">
      <StatTile label="当日盈亏" value="+1,284.50" unit="元" sub="浮盈贡献 +0.13%" subTone="up" note="红涨" />
      <StatTile label="当日盈亏" value="-3,120.00" unit="元" sub="浮亏贡献 -0.31%" subTone="down" note="绿跌" />
      <StatTile label="当前回撤" value="-1.87%" sub="接近熔断观察线 -2.00%" subTone="warn" note="相对净值高点" />
    </div>
  ),
};
