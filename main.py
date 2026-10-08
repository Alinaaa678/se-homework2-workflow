"""CLI entry: run the code-review workflow end to end.

Usage:
    python main.py                          # run with samples/requirement.md
    python main.py --requirement path.md    # run with a custom requirement
    python main.py --viz                    # print the flow as Mermaid
    python main.py --demo                   # offline run with a FakeLLM
"""

from __future__ import annotations

import argparse
import json
import sys

from workflow.agents import default_registry
from workflow.engine import WorkflowEngine
from workflow.llm import load_client


def make_step_printer():
    def print_step(step: dict) -> None:
        if step["status"] == "ok":
            print(
                f"  [step] {step['node']} (visit {step['visit']}) "
                f"-> {step['output_key']}  {step['duration_ms']}ms"
            )
        else:
            print(f"  [step] {step['node']} 失败: {step.get('error')}")
    return print_step


def run_workflow(requirement_path: str, demo: bool = False) -> int:
    with open(requirement_path, "r", encoding="utf-8") as f:
        requirement = f.read()

    if demo:
        from tests.test_workflow import FakeClient
        agents = default_registry(FakeClient())
        print("（演示模式：使用 FakeLLM，不调用真实 API）")
    else:
        agents = default_registry(load_client())

    engine = WorkflowEngine.from_file(
        "flows/code_review.json", agents, on_step=make_step_printer()
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
            print(f"  - [{issue.get('severity')}] {issue.get('desc')}")
    print(f"消息数: {len(state.messages)}  日志: {state.get('_run_log')}")

    if state.status == "success" and state.get("code"):
        out_path = "output/final_code.py"
        import os
        os.makedirs("output", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(state.get("code") + "\n")
        print(f"最终代码已写入: {out_path}")
    return 0 if state.status == "success" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Simple Workflow MVP (Homework 2)")
    parser.add_argument("--requirement", default="samples/requirement.md",
                        help="需求文件路径（默认 samples/requirement.md）")
    parser.add_argument("--viz", action="store_true", help="打印流程的 Mermaid 图")
    parser.add_argument("--demo", action="store_true", help="离线演示（FakeLLM）")
    args = parser.parse_args()

    if args.viz:
        # viz only needs the flow definition; agents are never invoked
        dummy = {"coder": None, "reviewer": None, "fixer": None}
        engine = WorkflowEngine.from_file("flows/code_review.json", agents=dummy)
        print(engine.to_mermaid())
        return 0

    return run_workflow(args.requirement, demo=args.demo)


if __name__ == "__main__":
    sys.exit(main())
