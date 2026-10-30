# 设计文档 — Verified Workflow

## 1. 设计目标

本系统把控制流、上下文、状态和完成证据放在模型之外。模型是节点的一种实现，不能单独决定整个任务已经完成。

```text
Flow JSON → Engine → Node Executor → LLM / deterministic function
                 ├─ State + messages + artifact versions
                 ├─ SQLite checkpoints + JSONL journal
                 ├─ Acceptance gate + content hashes
                 └─ Metrics + HTML replay + external evaluation
```

## 2. 节点与流程

| 节点 | 类型 | 职责 |
|---|---|---|
| `spec` | 确定性 | 保留原始需求，生成需求 ID、验收范围和规格哈希 |
| `coder` | LLM | 根据需求生成候选代码 |
| `reviewer_correctness` | LLM＋执行 | 运行候选代码，对照需求给出结构化审查 |
| `reviewer_style` | LLM | 只检查风格、可读性和接口一致性 |
| `acceptance` | LLM＋确定性执行 | 生成候选 unittest，实际执行并以退出码得出结果 |
| `verdict` | 确定性 | 汇总当前版本的所有证据，给出最终完成状态 |
| `fixer` | LLM | 根据审查问题与失败测试生成完整修复版 |

顺序版与并行版共用节点注册表。并行版将两个审查任务放入线程池，主线程汇合后调用 `merge_reviews`；每个任务写不同的黑板键，避免写冲突。

修复回边受 `max_steps` 和 `max_node_visits` 限制。达到预算后运行失败并保留证据，而不是无限修复。

## 3. 上下文、状态与消息

### 3.1 黑板

`WorkflowState.data` 保存 `requirement`、`spec`、`code`、`review`、`acceptance` 和 `verdict`。节点通过 flow JSON 声明输入键和输出键，引擎负责注入与写回。

### 3.2 消息

每次节点交接生成 `from / to / kind / payload / ts` 信封。消息只保留摘要，完整值写入 `artifact` 事件，兼顾时间线可读性与事后审计。

### 3.3 工件版本

工件值保持原类型以兼容节点接口，元数据单独保存在 `WorkflowState.artifacts`：

```json
{
  "version": 2,
  "hash": "sha256:...",
  "producer": "fixer",
  "visit": 1,
  "ts": 0
}
```

字符串按 UTF-8 哈希，结构化对象按规范化 JSON 哈希。审查和验收都记录 `target_code_hash`，验收还记录 `spec_hash`。

## 4. 规格与独立验收

`spec.py` 对编号或项目符号需求生成稳定的 `R001…Rxxx`。它是确定性格式化节点，不声称理解任意自然语言；原始文本始终保留。

`AcceptanceTestAgent` 使用需求和候选代码接口生成 unittest，随后 `run_acceptance`：

1. 在新的临时目录写入 `candidate.py` 与 `test_candidate.py`；
2. 清除敏感环境变量；
3. 在新子进程执行 `python -m unittest -v test_candidate`；
4. 只根据实际退出码设置测试是否通过；
5. 保存测试代码、输出、退出码、需求 ID、代码哈希和规格哈希。

模型生成测试不代表测试充分，所以系统另有 `evals/` 外部隐藏测试，防止工作流自评代替独立评价。

## 5. 最终完成门禁

`VerdictGate` 要求以下五项同时为真：

```text
review_passed        审查没有阻断问题
acceptance_passed    静态检查与独立测试均通过
review_current       审查对应当前代码哈希
acceptance_current   验收对应当前代码哈希
spec_current         验收对应当前规格哈希
```

全部满足时返回 `completed_verified`，否则返回 `needs_attention` 和未满足条件。模型自报 `passed=true` 不能绕过这些检查。

这解决两类问题：

- 程序跑完但没有满足需求；
- 代码变化后仍使用旧审查或旧验收结果。

## 6. 执行、日志与恢复

### 6.1 事件日志

每个事件追加并立即 flush 到 `runs/run-<id>.jsonl`：

```text
run_start / run_resume
step / message / artifact
route_error / run_end
```

step 包含节点、visit、耗时、状态和 `run_id:node:visit` 幂等标识；artifact 包含完整值、版本、哈希和生产者。

### 6.2 SQLite 检查点

`runs/workflow.db` 包含：

- `runs`：流程定义及哈希、当前节点、前序节点、访问次数、完整状态、错误和日志路径；
- `artifacts`：逐节点、逐轮次的完整工件历史。

初始运行和每个成功节点路由后保存检查点。`resume(run_id)`：

1. 读取最后检查点；
2. 对比当前 flow 哈希，不一致则拒绝恢复；
3. 恢复状态、消息、工件版本和访问次数；
4. 从尚未完成的节点继续；
5. 已完成的运行直接返回，不重复执行。

语义是 **at-least-once**：已保存检查点的节点不会重跑；崩溃时正在执行的节点可能重试。日志提供幂等标识，但任意外部系统仍需自身实现去重，不能据此宣称 exactly-once。

### 6.3 Replay 与 Resume

- Replay 只读取 JSONL 并生成 HTML。
- Resume 读取 SQLite 并真正继续执行。
- Rerun 创建新的 `run_id`，不覆盖旧运行。

## 7. 条件路由

`conditions.py` 使用 AST 白名单解析表达式，只允许：

- 字典／列表取值、常量、布尔运算与比较；
- 基本算术；
- `len / any / all / bool / str / int` 白名单函数。

属性访问、导入、推导式和任意函数调用均被拒绝。路由异常转为运行失败并记录 `route_error`。

## 8. 遥测与回放

`ChatClient` 发出 `llm_attempt / llm_retry / llm_response` 事件；`RunMetrics` 线程安全地汇总：

- 请求尝试、成功调用和重试次数；
- prompt、completion 和 total token；
- 节点调用次数与累计耗时。

指标进入检查点和 `run_end`。HTML 回放展示流程图、步骤、消息、工件版本与哈希、验收输出、证据检查和运行指标。

## 9. 外部评估

`evals/run_eval.py` 提供四组：

```text
direct      直接生成
single      单 Agent + 验收 + 修复
sequential  顺序 Workflow
parallel    并行双审 Workflow
```

每个任务目录同时包含 `requirement.md` 和 `hidden_tests.py`。工作流只能看到需求，评估器最后运行隐藏测试并记录：

- `verified_success_rate`；
- `false_success_rate`；
- 耗时、模型调用和 token；
- 每次运行的原始证据。

判断原则是：先用隐藏测试判断“行不行”，再与最简单基线比较质量、成本和延迟，判断复杂度是否值得。重复运行用于观察波动，不增加独立任务数。

## 10. 可扩展性

- **新增节点**：实现 `run(inputs) -> output`，加入 registry，再修改 flow JSON。
- **新增并行归并**：注册 reducer，并保证任务输出键不同。
- **新增评估任务**：增加一个需求与隐藏测试目录，无需改评估器。
- **替换模型**：修改 OpenAI 兼容服务配置；token 不存在时按 0 记录，不影响执行。

## 11. 已知边界

1. 模型生成的测试可能不完整，外部隐藏测试与人工验收仍不可替代。
2. 子进程不是安全沙箱；生产环境需要容器或更严格的权限隔离。
3. SQLite 是本地持久化方案，不是分布式工作流服务。
4. 并行粒度是 stage 内 fork/join，尚不支持任意子图分叉汇合。
5. 系统记录副作用幂等标识，但不保证外部副作用自动去重。
