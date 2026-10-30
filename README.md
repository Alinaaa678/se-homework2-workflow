# Verified Workflow — 软件工程 Homework 2

一个可运行、可恢复、可评估的多 Agent 工作流。系统将一句自然语言需求转换为 Python 代码，并经过规格追踪、双路审查、独立测试执行和确定性完成判定。

默认流程：

```text
Spec → Coder → 并行双路 Review → Acceptance Test → Verdict
                                      ↑                  │
                                      └──── Fixer ←──────┘
                                      （不通过且有剩余预算）
```

模型负责生成代码、审查意见和候选测试；**最终是否完成由程序根据当前代码版本、审查结果和测试退出码判定**。

## 课程要求对照

| 课程要求 | 实现情况 |
|---|---|
| 至少 2 个 Agent 协作 | Coder、CorrectnessReviewer、StyleReviewer、AcceptanceTest、Fixer 五个模型节点 |
| Agent 间消息传递或状态共享 | 共享黑板、消息信封、完整工件日志 |
| 顺序执行 | 声明式 JSON 节点与边 |
| 条件分支、循环 | Verdict 条件边与 Fixer 回边；总步数、单节点访问上限 |
| 并行执行 | 正确性与风格审查真实 fork/join，reducer 汇合 |
| 可视化 | Mermaid 流程图、自包含 HTML 运行回放；不是拖拽编辑器 |
| 执行日志与状态追踪 | JSONL 只追加日志、SQLite 检查点、工件版本和哈希 |
| 错误处理与恢复 | 节点重试、LLM 退避、超时、异常落盘、`--resume` 跨进程恢复 |
| 复用项目 1 | 复用 OpenAI 兼容客户端、工具执行和超时处理经验 |
| 可扩展性 | 注册式 Agent、声明式流程、独立 reducer、外部评估任务目录 |
| 文档 | README、Design、流程图和离线演示 |

## 本次增强

- **规格追踪**：把编号需求转换为 `R001…Rxxx`，保存规格哈希。
- **独立验收**：测试由模型候选生成，在全新子进程中执行，结果只取退出码。
- **确定性完成门禁**：审查通过、验收通过、代码哈希一致、规格哈希一致才可完成。
- **版本化证据**：每个工件记录生产者、版本和内容哈希，旧审查不能授权新代码。
- **持久恢复**：SQLite 保存运行状态、当前节点、访问次数和工件；支持失败后续跑。
- **可观测性**：记录节点耗时、模型调用、重试、输入／输出／总 token。
- **外部评估**：四种方案对照，使用 Workflow 看不见的隐藏测试计算最终通过率与误报成功率。
- **安全路由**：条件表达式使用 AST 白名单，不再直接调用 Python `eval`。

## 快速开始

环境要求：Python 3.9+，仅使用标准库。

```bash
# 1. 配置任意 OpenAI 兼容服务
cp config.example.json config.json   # 填入 base_url / api_key / model

# 2. 离线端到端演示，不需要 API Key
python main.py --demo

# 3. 使用真实模型运行
python main.py

# 4. 换需求或顺序流程
python main.py --requirement my_requirement.md
python main.py --flow flows/code_review.json

# 5. 输出 Mermaid 流程定义
python main.py --viz
```

只有 `verdict.status == completed_verified` 时，真实运行才写入 `output/final_code.py`；离线演示写入 `output/demo_code.py`。

### 恢复、检查与回放

```bash
# 从最后一个成功检查点继续；flow 哈希变化时拒绝恢复
python main.py --resume <run-id>

# 查看状态、当前节点、访问次数、最终证据和运行指标
python main.py --inspect <run-id>

# 仅生成历史回放，不重新执行节点
python main.py --replay runs/run-<run-id>.jsonl
```

`runs/workflow.db` 保存检查点，`runs/run-*.jsonl` 保存事件，`runs/run-*.html` 用于离线查看流程、消息、工件版本、验收证据和指标。

### 测试

```bash
python -m unittest discover -s tests -v
```

当前共 **42 项离线测试**，覆盖：

- 顺序、条件分支、循环和真实并发；
- 解析器、review schema、reducer 和需求传递；
- 独立测试成功／失败、旧证据拒绝和规格稳定性；
- SQLite 崩溃恢复、已完成节点不重跑、流程变更拒绝恢复；
- 安全条件表达式、LLM token 采集；
- 外部隐藏测试与误报成功统计；
- Todo 最终产物的需求验收。

## 对照评估

```bash
# 使用真实模型，可能产生 API 费用
python -m evals.run_eval --arm all --repeat 3

# 只评估一个方案或一个任务
python -m evals.run_eval --arm parallel --task todo --repeat 1
```

四个方案：

| 方案 | 用途 |
|---|---|
| `direct` | 直接生成，提供最低成本基线 |
| `single` | 单 Agent 生成、验收、修复闭环 |
| `sequential` | 顺序审查工作流 |
| `parallel` | 当前并行双审工作流 |

评估器运行 `evals/tasks/*/hidden_tests.py`，不采用 Workflow 自报分数，输出：

- 独立验收通过率；
- 自报成功但隐藏测试失败的误报成功率；
- 平均耗时、模型调用与 token；
- 每个任务、每次重复的原始证据。

仓库附带的 3 个任务用于验证评估框架，**不构成有统计意义的模型基准**。正式比较应增加任务数量，并保持模型、预算和任务集合一致。

## 项目结构

```text
├── main.py                     # run / resume / inspect / replay / viz / demo
├── flows/                      # 顺序与并行的已验证流程
├── workflow/
│   ├── engine.py               # 确定性执行、并行、路由、检查点
│   ├── agents.py               # LLM 节点与 VerdictGate
│   ├── state.py                # 黑板、消息、工件版本
│   ├── store.py                # SQLite runs / artifacts
│   ├── artifacts.py            # 内容哈希
│   ├── spec.py                 # 需求 ID 与规格版本
│   ├── conditions.py           # AST 白名单条件表达式
│   ├── metrics.py              # LLM 与节点指标
│   ├── tools.py                # 执行、静态检查、验收测试
│   ├── llm.py                  # 重试与遥测事件
│   └── viz.py                  # 离线 HTML 回放
├── evals/
│   ├── tasks/                  # 需求与隐藏测试
│   ├── verify.py               # 独立验收
│   └── run_eval.py             # 四方案对照
├── tests/                      # 42 项离线测试
├── samples/                    # 示例需求
├── output/                     # 经验证的代码产物
└── runs/                       # 日志、SQLite、HTML 回放
```

## 已知边界

- 恢复采用 **at-least-once**：已保存检查点的节点不重跑，崩溃时正在执行的节点可能重试；不宣称所有外部副作用 exactly-once。
- 模型生成的验收测试可能遗漏需求，因此仍需外部隐藏测试或人工评审判断真实质量。
- 子进程有超时、临时目录和敏感环境变量清理，但不是安全沙箱，不能阻止恶意代码访问文件系统或网络。
- SQLite 适用于本地课程项目，不提供分布式调度；并行粒度仍是 stage 内 fork/join。

详细设计见 [Design.md](Design.md)。
