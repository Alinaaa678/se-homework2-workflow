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
        解析节点输入（从黑板取键）-> 记录 handoff 消息摘要
        调用 Agent（失败重试 node_retries 次）
        输出写回黑板 -> 记录 artifact 消息摘要和完整 artifact 事件
        顺序扫描出边，第一条 when 成立的边决定下一节点
        无匹配边 -> 隐式 END；条件求值异常 -> failed 并记录 route_error
    步骤、消息、完整工件和结束状态追加写入 runs/run-<id>.jsonl
```

- **条件分支**：`when` 表达式在受限环境（无 builtins，只暴露 `state`
  和少数安全函数）中求值，例如 `state['review']['passed']`。
- **循环**：边允许指回前面的节点（`fixer → reviews`），由守卫防止
  死循环——真实运行中曾触发 5 次上限后优雅中止并报错，而不是挂死。
- **并行（fork/join）**：`"type": "parallel"` 的节点把多个 Agent 任务
  放进线程池**并发执行**。设计上把 fork 和 join 融合在一个节点内：
  所有任务共享同一份输入快照，各自写入**互不相同的**黑板键（校验期
  强制检查，从源头消除写冲突），join 就是"等所有 future 完成"；
  随后由具名 reducer（如 `merge_reviews`）把多路输出归并成节点的
  单一输出键供下游消费。除耗时日志外，同步屏障测试还验证两路会同时
  进入 `run()`，避免把"有两个任务"误当成"已经并发"。
- **错误处理**：节点（含并行任务）和出边条件异常均被引擎捕获；节点
  重试后仍失败则 `status=failed`。错误、完整日志和结束状态会保留，
  引擎不向调用方抛出执行或路由异常。

## 3. Agent 设计

| Agent | 输入 | 输出 | 要点 |
|---|---|---|---|
| CoderAgent | requirement | code | 只输出 fenced 代码块，解析器容错提取 |
| CorrectnessReviewAgent | requirement + code | review JSON | **先真实执行代码**（限时子进程，10s 超时），再逐项对照原始需求、代码和运行证据评正确性/健壮性 |
| StyleReviewAgent | code | review JSON | 不执行代码，只评风格：命名/函数长度/重复代码/文档字符串/接口一致性 |
| FixAgent | requirement + code + review | code | 按 issue 列表逐一修复，同时以原始需求约束功能不退化 |

两个审查 Agent 在 `reviews` 并行节点中**并发执行**，输出 schema 相同，
由 `merge_reviews` reducer 汇合：passed 取全体与、issues 合并并标注来源
（`[correctness]` / `[style]`）、保留正确性审查收集的运行证据。

Agent 输出的解析（`extract_code` / `extract_json`）都做了防御式处理；
`normalize_review` 进一步校验 `passed/issues/summary`、问题级别和非空描述。
最终 verdict 不直接信任模型自报结果：**存在 high/medium 问题必然不通过**，
模型自报不通过但没有列出问题时也保守判定不通过。reducer 会再次校验，
防止矛盾回复绕过修复循环。解析或 schema 校验失败按节点失败走重试/中止路径。

## 3.1 可视化回放（viz.py）

每次运行后自动生成 `runs/run-*.html`：自包含页面（内联 SVG + 原生 JS，
无 CDN、无服务器，断网可开），包含五部分——分层布局的流程图（节点标注
执行次数、回边虚线、条件标注）、步骤时间线（含并行任务逐路耗时）、
Agent 消息摘要、**完整工件版本记录**、最终产物查看器。消息摘要保持时间线
可读，`artifact` 事件则按节点、visit 和状态键保存每轮完整代码与审查结果。
流程定义嵌入 `run_start`，因此 `python main.py --replay <日志>` 可重新生成
回放页，并由 `artifacts_from_events` 恢复新版日志中的最终产物。

## 4. 从作业 1 复用

- `workflow/llm.py`：ChatClient 原样复用（指数退避、Retry-After、
  429/5xx 判定），仅去掉 tool-calling 载荷——Workflow 节点的 Agent
  是单次结构化调用，不需要工具循环。
- `workflow/tools.py`：复用 `run_python` 的子进程执行思路；修复了中文
  Windows 下 GBK 解码崩溃的问题（强制子进程 `PYTHONIOENCODING=utf-8` +
  父进程 `errors="replace"`），并在执行模型生成代码前清除 API Key、
  Token、密码等敏感环境变量。它提供超时和临时目录隔离，不是安全沙箱。
- 作业 1 是「一个 Agent 用工具多轮迭代」，作业 2 是「多个专职 Agent
  由流程编排协作」——复杂度从 Agent 内部上移到了编排层。

## 5. 可扩展性

- **加 Agent**：在 `agents.py` 写一个类（实现 `run(inputs) -> output`），
  注册进 `default_registry`，再在 flow JSON 里加一个节点即可。
- **改流程**：只改 JSON。例如默认流程里审查就是并行 stage，要再加一路
  「安全审查」，只需在 `reviews` 节点的 `tasks` 里加一项，不动引擎。
- **换模型**：`config.json` 改 `base_url` / `model`，任何 OpenAI 兼容
  服务（DeepSeek / 通义 / Kimi / 本地 vLLM）都可接入。

## 6. 已知边界

- 条件表达式用受限 `eval` 实现，适合课程项目；生产环境应换成
  真正的表达式语言（如 CEL/JSONLogic）。
- review 已有明确的运行时 schema 和 verdict 校验；其他黑板键的类型仍靠
  节点约定。若扩展为通用工作流平台，可在节点声明中增加 JSON Schema。
- 子进程执行不是安全沙箱，不能阻止恶意代码访问文件系统或网络；本项目的
  用途是课程内证据采集，生产环境应使用容器或更严格的隔离机制。
- 并行粒度是"stage 内多任务"（fork/join 融合），尚不支持跨阶段的
  自由分叉汇合（如 A→(B∥C)→D 且 B、C 各自是多步子流程）；
  需要时可把 stage 泛化为子图执行。
