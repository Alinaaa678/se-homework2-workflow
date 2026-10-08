# 设计文档 — Simple Workflow MVP

## 1. 核心概念

| 概念 | 实现 | 说明 |
|---|---|---|
| Workflow | `flows/*.json` | 声明式定义：节点列表 + 带条件的有向边 |
| Node | flow JSON 中的 `nodes[]` | 声明用哪个 Agent、读哪些状态键、写哪个键 |
| Agent | `workflow/agents.py` | 提示词驱动的 LLM 调用单元，产出结构化工件 |
| State（黑板） | `WorkflowState.data` | 所有节点共享的键值空间，数据血缘显式可查 |
| Message | `WorkflowState.messages` | 每次节点交接都记录信封（from/to/kind/payload/ts） |
| Edge | flow JSON 中的 `edges[]` | `from → to`，可带 `when` 条件；`to: "END"` 结束流程 |

### 为什么是「黑板 + 消息」双通道？

- 黑板解决**状态共享**：节点只声明自己读/写的键（如 reviewer 读 `code`
  写 `review`），引擎负责注入与落盘，节点之间零耦合——新增节点不需要
  改任何已有节点。
- 消息解决**通信留痕**：每次交接都生成信封，评审时能完整回放
  「谁把什么交给了谁」，消息数本身也是运行指标。

## 2. 引擎执行模型

```
run(initial):
    current = flow.start
    loop:
        守卫检查（总步数 / 单节点访问上限，超限 -> failed 并中止）
        解析节点输入（从黑板取键）-> 记录 handoff 消息
        调用 Agent（失败重试 node_retries 次）
        输出写回黑板 -> 记录 artifact 消息
        顺序扫描出边，第一条 when 成立的边决定下一节点
        无匹配边 -> 隐式 END
    所有步骤追加写入 runs/run-<id>.jsonl
```

- **条件分支**：`when` 表达式在受限环境（无 builtins，只暴露 `state`
  和少数安全函数）中求值，例如 `state['review']['passed']`。
- **循环**：边允许指回前面的节点（`fixer → reviewer`），由守卫防止
  死循环——真实运行中曾触发 reviewer 5 次上限后优雅中止并报错，
  而不是挂死。
- **错误处理**：节点异常被引擎捕获，重试后仍失败则 `status=failed`、
  记录错误并写出完整日志，引擎本身不向调用方抛异常。

## 3. Agent 设计

| Agent | 输入 | 输出 | 要点 |
|---|---|---|---|
| CoderAgent | requirement | code | 只输出 fenced 代码块，解析器容错提取 |
| ReviewAgent | code | review JSON | **先真实执行代码**（沙箱子进程，10s 超时），把运行结果连同代码一起交给 LLM 评审；输出 schema 归一化 |
| FixAgent | code + review | code | 按 issue 列表逐一修复，保持功能不变 |

Agent 输出的解析（`extract_code` / `extract_json`）都做了防御式处理：
LLM 回复夹杂解释文字、markdown 围栏时仍能提取，解析失败按节点失败
走重试/中止路径。

## 4. 从作业 1 复用

- `workflow/llm.py`：ChatClient 原样复用（指数退避、Retry-After、
  429/5xx 判定），仅去掉 tool-calling 载荷——Workflow 节点的 Agent
  是单次结构化调用，不需要工具循环。
- `workflow/tools.py`：复用 `run_python` 的隔离执行思路；修复了中文
  Windows 下 GBK 解码崩溃的问题（强制子进程 `PYTHONIOENCODING=utf-8` +
  父进程 `errors="replace"`）。
- 作业 1 是「一个 Agent 用工具多轮迭代」，作业 2 是「多个专职 Agent
  由流程编排协作」——复杂度从 Agent 内部上移到了编排层。

## 5. 可扩展性

- **加 Agent**：在 `agents.py` 写一个类（实现 `run(inputs) -> output`），
  注册进 `default_registry`，再在 flow JSON 里加一个节点即可。
- **改流程**：只改 JSON。例如把修复循环改成并行「性能审查 + 安全审查」，
  只需加节点和边，不动引擎。
- **换模型**：`config.json` 改 `base_url` / `model`，任何 OpenAI 兼容
  服务（DeepSeek / 通义 / Kimi / 本地 vLLM）都可接入。

## 6. 已知边界

- 条件表达式用受限 `eval` 实现，适合课程项目；生产环境应换成
  真正的表达式语言（如 CEL/JSONLogic）。
- 黑板键的类型靠约定（review 必须是 dict），未做 schema 校验；
  可在节点声明中加 `schema` 字段用 jsonschema 校验。
- 目前是顺序执行引擎；并行分支可在引擎中按「出边集合 + join 节点」
  扩展，状态写冲突需约定合并策略。
