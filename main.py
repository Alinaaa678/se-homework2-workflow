"""CLI for running, resuming, inspecting and replaying verified workflows."""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, Tuple

from workflow.agents import REDUCERS, default_registry
from workflow.engine import FlowError, WorkflowEngine
from workflow.llm import load_client
from workflow.metrics import RunMetrics
from workflow.state import WorkflowState
from workflow.viz import artifacts_from_events, render_replay, replay_path_for

DEFAULT_FLOW = "flows/code_review_parallel.json"
DUMMY_AGENTS = {
    "spec": None, "coder": None, "reviewer": None,
    "reviewer_correctness": None, "reviewer_style": None,
    "acceptance": None, "verdict": None, "fixer": None,
}


def make_step_printer():
    def print_step(step: Dict[str, Any]) -> None:
        if step["status"] == "ok":
            print(
                f"  [step] {step['node']} (visit {step['visit']}) "
                f"-> {step['output_key']}  {step['duration_ms']}ms"
            )
            for task in step.get("tasks", []):
                print(f"      └─ 并行 {task['task']}({task['agent']}) {task['duration_ms']}ms")
        else:
            print(f"  [step] {step['node']} 失败: {step.get('error')}")
    return print_step


def make_engine(flow_path: str, demo: bool = False) -> WorkflowEngine:
    metrics = RunMetrics()
    if demo:
        from tests.test_workflow import FakeClient
        client = FakeClient()
        print("（演示模式：使用 FakeLLM，不调用真实 API）")
    else:
        client = load_client(on_event=metrics.handle_event)
    return WorkflowEngine.from_file(
        flow_path,
        default_registry(client),
        on_step=make_step_printer(),
        reducers=REDUCERS,
        metrics=metrics,
    )


def present_result(state: WorkflowState, engine: WorkflowEngine, demo: bool) -> None:
    print("\n===== 运行结果 =====")
    print(f"状态: {state.status}")
    if state.error:
        print(f"错误: {state.error}")
    verdict = state.get("verdict") or {}
    if verdict:
        print(f"证据判定: {verdict.get('status')}；通过={verdict.get('passed')}")
        if verdict.get("reasons"):
            print(f"未满足条件: {', '.join(verdict['reasons'])}")
    review = state.get("review") or {}
    if review:
        print(f"审查总结: {review.get('summary', '')}")
        for issue in review.get("issues", []):
            source = f"[{issue['source']}] " if issue.get("source") else ""
            print(f"  - [{issue.get('severity')}] {source}{issue.get('desc')}")
    acceptance = state.get("acceptance") or {}
    if acceptance:
        result = acceptance.get("run_result") or {}
        print(f"独立验收: 通过={acceptance.get('passed')}；退出码={result.get('exit_code')}")
    metrics = state.get("_metrics") or {}
    print(
        f"模型调用: {metrics.get('llm_calls', 0)}；Token: "
        f"{metrics.get('total_tokens', 0)}；节点耗时: "
        f"{metrics.get('node_duration_ms', 0)}ms"
    )
    log_path = state.get("_run_log")
    if log_path:
        print(f"运行 ID: {os.path.basename(log_path)[4:-6]}")
        print(f"日志: {log_path}")

    if log_path and os.path.exists(log_path):
        with open(log_path, encoding="utf-8") as handle:
            events = [json.loads(line) for line in handle]
        replay = replay_path_for(log_path)
        render_replay(engine.flow, events, artifacts_from_events(events), replay)
        print(f"可视化回放: {replay}")

    if state.status == "success" and verdict.get("passed") and state.get("code"):
        out_path = "output/demo_code.py" if demo else "output/final_code.py"
        os.makedirs("output", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write(state.get("code") + "\n")
        print(f"已验证代码已写入: {out_path}")


def run_workflow(requirement_path: str, flow_path: str, demo: bool) -> int:
    with open(requirement_path, "r", encoding="utf-8") as handle:
        requirement = handle.read()
    engine = make_engine(flow_path, demo)
    print(f"流程: {engine.flow['name']} — {engine.flow.get('description', '')}")
    state = engine.run({"requirement": requirement})
    present_result(state, engine, demo)
    return 0 if state.status == "success" else 1


def resume_workflow(run_id: str, flow_path: str, demo: bool) -> int:
    engine = make_engine(flow_path, demo)
    state = engine.resume(run_id)
    present_result(state, engine, demo)
    return 0 if state.status == "success" else 1


def replay_run(log_path: str) -> int:
    with open(log_path, encoding="utf-8") as handle:
        events = [json.loads(line) for line in handle]
    start = next((event for event in events if event.get("event") == "run_start"), {})
    flow = start.get("flow_def")
    if not flow:
        print("该日志不含流程定义，无法回放", file=sys.stderr)
        return 1
    out = replay_path_for(log_path)
    render_replay(flow, events, artifacts_from_events(events), out)
    print(f"可视化回放: {out}")
    return 0


def inspect_run(run_id: str, flow_path: str) -> int:
    engine = WorkflowEngine.from_file(
        flow_path, agents=DUMMY_AGENTS, reducers=REDUCERS
    )
    record = engine.inspect(run_id)
    state = record["state"]
    summary = {
        "run_id": run_id,
        "flow": record["flow_name"],
        "flow_hash": record["flow_hash"],
        "status": record["status"],
        "current_node": record["current_node"],
        "visits": record["visits"],
        "error": record["error"],
        "verdict": state.get("data", {}).get("verdict"),
        "metrics": state.get("data", {}).get("_metrics"),
        "log_path": record["log_path"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Verified multi-agent workflow")
    parser.add_argument("--requirement", default="samples/requirement.md")
    parser.add_argument("--flow", default=DEFAULT_FLOW)
    parser.add_argument("--viz", action="store_true", help="输出 Mermaid 流程图")
    parser.add_argument("--replay", metavar="LOG", help="从历史 JSONL 生成 HTML，不执行任务")
    parser.add_argument("--resume", metavar="RUN_ID", help="从 SQLite 检查点继续执行")
    parser.add_argument("--inspect", metavar="RUN_ID", help="查看检查点、证据和指标")
    parser.add_argument("--demo", action="store_true", help="使用 FakeLLM 离线演示")
    args = parser.parse_args()

    try:
        if args.replay:
            return replay_run(args.replay)
        if args.inspect:
            return inspect_run(args.inspect, args.flow)
        if args.viz:
            engine = WorkflowEngine.from_file(
                args.flow, agents=DUMMY_AGENTS, reducers=REDUCERS
            )
            print(engine.to_mermaid())
            return 0
        if args.resume:
            return resume_workflow(args.resume, args.flow, args.demo)
        return run_workflow(args.requirement, args.flow, args.demo)
    except (FlowError, KeyError, FileNotFoundError) as exc:
        print(f"操作失败: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
