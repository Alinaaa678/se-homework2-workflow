# Verified Workflow

软件工程 Homework 2：一个可运行、可恢复、可评估的多 Agent 工作流。

系统将自然语言需求转换为 Python 代码，并经过规格追踪、并行审查、独立测试和确定性完成判定。模型负责生成代码、审查和候选测试，**程序根据当前代码版本与测试退出码决定是否完成**。

## 功能

- 顺序执行、条件分支、有界修复循环、双路并行审查。
- 共享状态、Agent 消息、完整工件版本与内容哈希。
- 独立验收测试，旧审查和旧验收不能授权新代码完成。
- SQLite 检查点与跨进程恢复，JSONL 日志与 HTML 回放。
- 模型调用、重试、token 和节点耗时统计。
- 直接生成、单 Agent、顺序 Workflow、并行 Workflow 四组对照评估。

## 工程结构

```text
Homework2-Workflow/
├── main.py                 # 命令行入口
├── config.example.json     # 配置模板，不含真实密钥
├── flows/                  # 顺序与并行流程定义
├── workflow/               # 编排器、Agent、状态、验收与回放
├── evals/                  # 外部隐藏测试与对照评估
├── samples/                # 默认示例需求
├── tests/
│   ├── fixtures/           # 测试使用的参考实现
│   └── test_*.py           # 42 项离线测试
├── Design.md               # 详细技术设计
└── README.md               # 使用入口
```

`runs/`、`output/` 和 `evals/results/` 是本地运行后生成的目录，不提交 Git。

## 立即运行

环境要求：Python 3.9+，仅使用标准库。

```bash
# 离线演示：不需要 API Key
python main.py --demo

# 运行全部测试：不需要 API Key
python -m unittest discover -s tests -v
```

## 使用真实模型

真实模型运行和对照评估需要 API Key。

```bash
cp config.example.json config.json
```

然后编辑本地 `config.json`：

```json
{
  "base_url": "https://api.deepseek.com/v1",
  "api_key": "YOUR_API_KEY",
  "model": "deepseek-chat"
}
```

把 `YOUR_API_KEY` 换成自己的真实密钥，再运行：

```bash
python main.py
```

`config.json` 已被 `.gitignore` 排除。**不要提交真实 API Key，也不要把密钥写入 README、源码或 `config.example.json`。**

## 常用命令

| 目的 | 命令 |
|---|---|
| 真实模型运行 | `python main.py` |
| 指定需求 | `python main.py --requirement my_requirement.md` |
| 使用顺序流程 | `python main.py --flow flows/code_review.json` |
| 查看 Mermaid 流程图 | `python main.py --viz` |
| 从检查点恢复 | `python main.py --resume <run-id>` |
| 查看状态和证据 | `python main.py --inspect <run-id>` |
| 生成历史回放，不重新执行 | `python main.py --replay runs/run-<run-id>.jsonl` |

只有最终状态为 `completed_verified` 时，真实运行才写入 `output/final_code.py`；离线演示写入 `output/demo_code.py`。

## 对照评估

```bash
# 会调用真实模型，可能产生 API 费用
python -m evals.run_eval --arm all --repeat 3
```

评估器使用 Workflow 看不见的隐藏测试，统计独立通过率、误报成功率、耗时和 token。附带的 3 个任务用于验证评估框架，不构成有统计意义的模型基准。

## 技术边界

- 恢复采用 at-least-once 语义，正在执行的节点在崩溃后可能重试。
- 模型生成的测试可能遗漏需求，仍需外部隐藏测试或人工验收。
- 子进程有超时和敏感环境变量清理，但不是安全沙箱。

架构、证据门禁、恢复语义和评估方法详见 [Design.md](Design.md)。
