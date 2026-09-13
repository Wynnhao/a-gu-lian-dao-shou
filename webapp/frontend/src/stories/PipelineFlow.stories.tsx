import type { Meta, StoryObj } from "@storybook/react";
import { PipelineFlow } from "@/components/PipelineFlow";
import type { WorkflowStage } from "@/lib/api";

const meta: Meta<typeof PipelineFlow> = {
  title: "盯盘/PipelineFlow 工作流流水线",
  component: PipelineFlow,
  parameters: { layout: "padded" },
};

export default meta;
type Story = StoryObj<typeof PipelineFlow>;

const stages: WorkflowStage[] = [
  { id: "fetch", name: "行情采集", desc: "东财/腾讯双源日K增量入库", status: "ok", detail: "5/5 票数据至 2026-09-11", ts: "2026-09-12T18:01:51" },
  { id: "news", name: "资讯/估值", desc: "个股+市场新闻；三指数PE/PB分位", status: "ok", detail: "新闻 74 条；估值 2026-09-11（滞后 0 天）", ts: "2026-09-12T18:00:29" },
  { id: "health", name: "数据体检", desc: "黑名单过滤 + 数据健康检查", status: "ok", detail: "健康 OK；黑名单拦截 1/5", ts: null },
  { id: "signals", name: "信号计算", desc: "MA/RSI/动量/换手分位 → score", status: "ok", detail: "4 票 as_of=2026-09-11", ts: null },
  { id: "bundle", name: "输入包组装", desc: "信号+新闻+宏观+账户 → bundle.md", status: "ok", detail: "bundle 生成于 09-12 18:00", ts: "2026-09-12T18:00:29" },
  { id: "llm", name: "LLM决策", desc: "读bundle输出结构化建议（校验失败整包放弃）", status: "warn", detail: "2 条决策 approved=1、rejected=1", ts: "2026-09-12T18:02:10" },
  { id: "risk", name: "规则裁决", desc: "15条硬规则：仓位/价格/T+1/涨跌停/kill", status: "ok", detail: "风控事件 3 条；approved=1 rejected=1 report_only=0", ts: "2026-09-12T18:02:11" },
  { id: "gate", name: "人工闸门", desc: "pending待确认单，confirm时重跑风控", status: "warn", detail: "1 单待人工确认", ts: "2026-09-12T18:02:12" },
  { id: "exec", name: "模拟执行", desc: "PaperBroker成交(T+1)+回读校验", status: "idle", detail: "当日无成交", ts: null },
  { id: "review", name: "盘后复盘", desc: "盯市+日报+归因", status: "fail", detail: "日报未生成", ts: null },
];

export const 十段全状态: Story = {
  args: { stages },
  name: "十段 · ok/warn/fail/idle 混合",
  render: () => (
    <div className="w-[1200px] border bg-card">
      <PipelineFlow stages={stages} className="px-1.5 py-1" />
    </div>
  ),
};

export const 全部空闲: Story = {
  name: "十段 · 全部 idle（未开跑）",
  render: () => (
    <div className="w-[1200px] border bg-card">
      <PipelineFlow stages={stages.map((s) => ({ ...s, status: "idle", detail: "未运行", ts: null }))} className="px-1.5 py-1" />
    </div>
  ),
};
