"""CLI entry: run the code-review workflow end to end.

Usage:
    python main.py                            # run parallel flow with samples/requirement.md
    python main.py --requirement path.md      # run with a custom requirement
    python main.py --flow flows/code_review.json   # use the sequential flow instead
    python main.py --viz                      # print the flow as Mermaid
    python main.py --replay runs/run-XX.jsonl # render an offline HTML replay page
    python main.py --demo                     # offline run with a FakeLLM
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from workflow.agents import REDUCERS, default_registry
from workflow.engine import WorkflowEngine
from workflow.llm import load_client
from workflow.viz import render_replay, replay_path_for

DEFAULT_FLOW = "flows/code_review_parallel.json"
# viz only needs the flow definition; agents are never invoked
DUMMY_AGENTS = {
    "coder": None, "reviewer": None, "reviewer_correctness": None,
    "reviewer_style": None, "fixer": None,
}


def make_step_printer():
    def print_step(step: dict) -> None:
        if step["status"] == "ok":
            print(
                f"  [step] {step['node']} (visit {step['visit']}) "
                f"-> {step['output_key']}  {step['duration_ms']}ms"
            )
            for t in step.get("tasks", []):
                print(f"      └─ 并行 {t['task']}({t['agent']}) {t['duration_ms']}ms")
        else:
            print(f"  [step] {step['node']} 失败: {step.get('error')}")
    return print_step


def run_workflow(requirement_path: str, flow_path: str, demo: bool = False) -> int:
    with open(requirement_path, "r", encoding="utf-8") as f:
        requirement = f.read()

    if demo:
        from tests.test_workflow import FakeClient
        agents = default_registry(FakeClient())
        print("（演示模式：使用 FakeLLM，不调用真实 API）")
    else:
        agents = default_registry(load_client())

    engine = WorkflowEngine.from_file(
        flow_path, agents, on_step=make_step_printer(), reducers=REDUCERS
    )
    print(f"流程: {engine.flow['name']} — {engine.flow.get('description', '')}")
    print(f"需求: {requirement_path}\n")

    state = engine.run({"requirement": requirement})

    print("\n===== 运行结果 =====")
    print(f"状态: {state.status}")
    if state.error:
        print(f"错误: {state.error}")
    review = state.get("review")
    if review:
        print(f"审查结论: {'✅ 通过' if review['passed'] else '❌ 未通过'}")
        print(f"总结: {review.get('summary', '')}")
        for issue in review.get("issues", []):
            src = f"[{issue['source']}] " if issue.get("source") else ""
            print(f"  - [{issue.get('severity')}] {src}{issue.get('desc')}")
    print(f"消息数: {len(state.messages)}  日志: {state.get('_run_log')}")

    # Offline HTML replay next to the run log.
    log_path = state.get("_run_log")
    if log_path and os.path.exists(log_path):
        with open(log_path, encoding="utf-8") as f:
            events = [json.loads(line) for line in f]
        replay = replay_path_for(log_path)
        render_replay(
            engine.flow, events,
            {"code": state.get("code"), "review": state.get("review")},
            replay,
        )
        print(f"可视化回放: {replay}（浏览器打开即可）")

    if state.status == "success" and state.get("code"):
        out_path = "output/final_code.py"
        os.makedirs("output", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(state.get("code") + "\n")
        print(f"最终代码已写入: {out_path}")
    return 0 if state.status == "success" else 1


def replay_run(log_path: str) -> int:
    with open(log_path, encoding="utf-8") as f:
        events = [json.loads(line) for line in f]
    start = next((e for e in events if e.get("event") == "run_start"), {})
    flow = start.get("flow_def")
    if not flow:
        print("该日志不含流程定义，无法回放（请用本次升级后生成的日志）", file=sys.stderr)
        return 1
    out = replay_path_for(log_path)
    render_replay(flow, events, {}, out)
    print(f"可视化回放: {out}（浏览器打开即可）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Simple Workflow MVP (Homework 2)")
    parser.add_argument("--requirement", default="samples/requirement.md",
                        help="需求文件路径（默认 samples/requirement.md）")
    parser.add_argument("--flow", default=DEFAULT_FLOW,
                        help=f"流程定义（默认 {DEFAULT_FLOW}）")
    parser.add_argument("--viz", action="store_true", help="打印流程的 Mermaid 图")
    parser.add_argument("--replay", metavar="LOG", help="把运行日志渲染成 HTML 回放页")
    parser.add_argument("--demo", action="store_true", help="离线演示（FakeLLM）")
    args = parser.parse_args()

    if args.replay:
        return replay_run(args.replay)

    if args.viz:
        engine = WorkflowEngine.from_file(
            args.flow, agents=DUMMY_AGENTS, reducers=REDUCERS
        )
        print(engine.to_mermaid())
        return 0

    return run_workflow(args.requirement, args.flow, demo=args.demo)


if __name__ == "__main__":
    sys.exit(main())
