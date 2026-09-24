# 贡献指南

感谢你愿意让这个项目变得更好。下面是参与贡献的流程与约定。

## 行为准则

参与本项目即表示同意 [CODE_OF_CONDUCT.md](./CODE_OF_CONDUCT.md)。请在所有沟通中保持专业与尊重。

## 提 Issue

- **Bug**：用 [Bug 报告模板](.github/ISSUE_TEMPLATE/bug_report.md)，附运行命令、报错堆栈、相关日志片段（注意脱敏）。
- **功能请求**：用 [Feature Request 模板](.github/ISSUE_TEMPLATE/feature_request.md)。
- **安全漏洞**：**不要**在公开 Issue 里披露，请按 [SECURITY.md](./SECURITY.md) 走私密渠道。

## 提 PR

1. Fork 仓库 → 从 `main` 拉新分支（建议命名 `fix/xxx` 或 `feat/xxx`）。
2. 本地开发：
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   # 跑全部测试（必须全绿）
   .venv/bin/python3 tests/run_all.py
   ```
3. 提交：
   - 一个 commit 只做一件事。
   - commit message 第一行 ≤ 72 字；正文解释"为什么"而不是"做了什么"。
   - 中文 / 英文均可，但风格保持一致。
4. 推送到你的 fork → 在 GitHub 上开 PR：
   - 标题简洁（动词开头，如 `fix: 修复 X 场景下 Y 异常`）。
   - 正文链接相关 Issue，说明改动与影响面。
   - 勾选 PR 模板里的"已跑 `tests/run_all.py`"复选框。
5. 等待 CI 与维护者 review；按 review 意见迭代。

## 开发约定

- **不要跳过测试**。新增功能必须配测试，bug 修复必须先写能复现的失败测试。
- **不要直接改 `config.json`** 提交个人化改动（如 watchlist）。本地自定义用 `config.local.json` 覆盖。
- **不要把 `.venv/`、`data/*.db`、`logs/`、`__pycache__/` 提交**——`.gitignore` 已覆盖。
- **不要在代码里硬编码个人信息**（姓名、邮箱、API key 等）。

## 项目结构速览

```
A股镰刀手/
├── data/          # 数据层：行情/资讯/宏观的入库与体检
├── signals/       # 信号层：因子/信号计算/动态池/回测
├── ai/            # 决策层：输入包组装 + LLM 决策 JSON 校验
├── risk/          # 风控层：19 条硬规则裁决 + 市场环境总闸
├── execution/     # 执行层：模拟成交 + 人工闸门编排
├── pipeline/      # 流水线：盘前/午评/下午/尾盘/盘后/catchup
├── review/        # 复盘层：日报/周报/信号有效性评估
├── webapp/        # Web 控制台：stdlib 后端 + React/Vite 前端
├── tests/         # 测试套件（聚合入口 tests/run_all.py）
├── docs/          # 文档（核心 5 篇 + docs/internal/archives/）
└── config.json    # 全局配置
```

## 关键约束

请阅读 [CONSTRAINTS.md](./CONSTRAINTS.md) 了解项目硬约束：
- L1：默认 paper 模拟盘，不接实盘资金。
- L2：永不自动成交，所有订单必经人工闸门。
- L3：LLM 决策必须保留可追溯快照（`decision.input_snapshot`）。
- L4：数据体检发现严重不一致时全链降级，不容许带病运行。

## 许可证

贡献的代码默认以 MIT 协议授权，与项目主许可证一致。详见 [LICENSE](./LICENSE)。
