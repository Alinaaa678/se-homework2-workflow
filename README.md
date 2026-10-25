# Simple Workflow MVP — 软件工程 Homework 2

一个**多 Agent 协作工作流**：`编码 Agent → 并行双路审查（正确性 + 风格）→ （任一不通过则）修复 → 再审查`，
端到端地把一句自然语言需求变成一份通过审查、可直接运行的 Python 代码。

## 流程图

```mermaid
flowchart TD
    coder[coder 编码Agent]
    reviews[reviews 并行审查: 正确性 + 风格]
    fixer[fixer 修复Agent]
    coder --> reviews
    reviews -->|passed| END((END))
    reviews -->|not passed| fixer
    fixer --> reviews
```

（`python main.py --viz` 打印上图源码；`python main.py` 运行后还会在 `runs/` 生成
**自包含 HTML 回放页**——流程图、步骤时间线、消息摘要、完整工件版本和最终产物，浏览器打开即可，无需联网）

## 对照作业要求

| 要求 | 实现 |
|---|---|
| 至少 2 个 Agent 协作 | 4 个：Coder / CorrectnessReviewer / StyleReviewer / Fixer |
| Agent 间消息传递或状态共享 | 两者都有：共享黑板 `WorkflowState.data` + 每次交接记录为消息信封（并写入 JSONL 日志） |
| 顺序执行流程控制 | 声明式 JSON 流程定义（`flows/`），引擎按边推进 |
| 条件分支、循环（推荐） | 边可带 `when` 条件表达式；`fixer → reviews` 回边构成修复-复审循环 |
| **并行执行**（推荐） | 并行 stage：正确性审查与风格审查**并发执行**（fork/join 融合为一个节点），`merge_reviews` reducer 汇合双路结论；同步屏障测试证明两路会同时进入执行 |
| **可视化**（推荐，可选） | `runs/run-*.html` 离线回放页：SVG 流程图 + 步骤时间线 + 消息摘要 + **完整工件版本记录** + 最终产物；`--replay` 可从历史日志恢复产物 |
| 执行日志与状态追踪（推荐） | 每步写入 `runs/run-*.jsonl`；消息保存交接摘要，`artifact` 事件按节点和轮次保存**完整代码与审查结果** |
| 错误处理与恢复（推荐） | 节点失败自动重试；LLM 调用指数退避；并行失败、路由异常和循环超限均记录并优雅中止 |
| 复用项目 1（推荐） | `workflow/llm.py` 复用作业 1 的 ChatClient；`workflow/tools.py` 复用「运行代码验证」思路，正确性审查 Agent 会**真实执行**被审代码 |
| 需求验收与审查可信度（额外） | 正确性审查和修复均读取原始需求；校验 review schema，`high/medium` 问题强制阻断；31 项测试中含 5 项 Todo 原始需求验收测试 |

## 快速开始

```bash
# 1. 配置（与作业 1 相同）
cp config.example.json config.json   # 填入 base_url / api_key / model

# 2. 端到端运行（默认并行流程 + samples/requirement.md）
python main.py

# 3. 换需求 / 换流程
python main.py --requirement my_requirement.md
python main.py --flow flows/code_review.json   # 顺序版流程（单审查员）

# 4. 流程图与回放
python main.py --viz                            # Mermaid 源码
python main.py --replay runs/run-XXXX.jsonl     # 历史日志 → HTML 回放页

# 5. 离线演示 / 自测（无需 API Key）
python main.py --demo                         # 演示产物写入 output/demo_code.py，不覆盖真实产物
python -m unittest discover -s tests -v        # 31 项测试，含 5 项最终 Todo 产物验收
```

零第三方依赖，Python 3.9+ 即可运行。

## 一次真实运行记录（并行版）

```
流程: code-review-workflow-parallel
  [step] coder (visit 1) -> code  3595ms
  [step] reviews (visit 1) -> review  2386ms
      └─ 并行 correctness(reviewer_correctness) 862ms
      └─ 并行 style(reviewer_style) 2382ms      ← 总耗时≈较慢一路，确为并发
状态: success
审查结论: ✅ 通过（正确性确认全部需求实现；风格给出 6 条 low 级建议）
最终代码已写入: output/final_code.py（31 项测试通过，含 5 项需求验收）
对应证据: runs/run-20261025-143429-7f64bb.jsonl / .html
```

## 项目结构

```
├── main.py               # CLI 入口（run / --flow / --viz / --replay / --demo）
├── workflow/
│   ├── engine.py         # 声明式流程 + 条件边 + 并行 stage + 循环守卫 + 完整工件日志
│   ├── state.py          # 共享状态：黑板 + Agent 消息日志
│   ├── agents.py         # 4 个 Agent + review schema 校验 + merge_reviews reducer
│   ├── llm.py            # OpenAI 兼容客户端（复用自作业 1，含重试）
│   ├── tools.py          # 限时子进程（清除敏感环境变量；非安全沙箱）
│   └── viz.py            # 离线 HTML 回放：流程、时间线、消息、工件版本、最终产物
├── flows/
│   ├── code_review_parallel.json  # 默认流程：并行双路审查
│   └── code_review.json           # 顺序版流程（单审查员，用于对照）
├── samples/requirement.md         # 示例需求
├── tests/
│   ├── test_workflow.py           # 解析器、reducer、顺序流、并行流、回放
│   ├── test_engine_robustness.py  # 真实并发、异常、schema、需求传递、工件恢复
│   └── test_todo_acceptance.py    # 当前最终 Todo 产物的 5 项需求验收
└── runs/                 # 每次运行的 JSONL 日志 + HTML 回放页
```

设计取舍与扩展方法见 [Design.md](Design.md)。
