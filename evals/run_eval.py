"""Run direct / single-agent / sequential / parallel comparisons.

Example (uses the configured real LLM and may incur API cost):
    python -m evals.run_eval --arm all --repeat 3
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.verify import verify_code
from workflow.agents import (
    AcceptanceTestAgent, CoderAgent, FixAgent, REDUCERS, default_registry,
)
from workflow.engine import WorkflowEngine
from workflow.llm import load_client
from workflow.metrics import RunMetrics
from workflow.spec import build_spec

ARMS = ("direct", "single", "sequential", "parallel")


def discover_tasks(task_root: Path, selected: str | None = None) -> List[Path]:
    tasks = [
        path for path in sorted(task_root.iterdir())
        if path.is_dir()
        and (path / "requirement.md").exists()
        and (path / "hidden_tests.py").exists()
        and (selected is None or path.name == selected)
    ]
    if not tasks:
        raise ValueError("没有找到同时包含 requirement.md 和 hidden_tests.py 的任务")
    return tasks


def run_candidate(
    arm: str, requirement: str, client: Any, log_dir: Path
) -> Dict[str, Any]:
    started = time.time()
    code = ""
    reported_success = None
    status = "completed"
    state_data: Dict[str, Any] = {}

    if arm == "direct":
        code = CoderAgent(client).run({"requirement": requirement})
    elif arm == "single":
        spec = build_spec(requirement)
        coder = CoderAgent(client)
        acceptance_agent = AcceptanceTestAgent(client)
        fixer = FixAgent(client)
        code = coder.run({"requirement": requirement})
        acceptance: Dict[str, Any] = {}
        for _ in range(3):
            acceptance = acceptance_agent.run({
                "code": code, "requirement": requirement, "spec": spec,
            })
            if acceptance["passed"]:
                reported_success = True
                break
            reported_success = False
            code = fixer.run({
                "code": code, "requirement": requirement, "review": {"issues": []},
                "acceptance": acceptance,
            })
        status = "success" if reported_success else "failed"
        state_data = {"acceptance": acceptance}
    else:
        flow_name = "code_review.json" if arm == "sequential" else "code_review_parallel.json"
        engine = WorkflowEngine.from_file(
            str(ROOT / "flows" / flow_name),
            default_registry(client),
            reducers=REDUCERS,
            log_dir=str(log_dir),
        )
        state = engine.run({"requirement": requirement})
        code = state.get("code") or ""
        status = state.status
        verdict = state.get("verdict") or {}
        reported_success = bool(state.status == "success" and verdict.get("passed"))
        state_data = {
            "verdict": verdict,
            "metrics": state.get("_metrics"),
            "run_log": state.get("_run_log"),
        }

    return {
        "code": code,
        "status": status,
        "reported_success": reported_success,
        "duration_ms": round((time.time() - started) * 1000),
        "details": state_data,
    }


def metric_delta(before: Dict[str, int], after: Dict[str, int]) -> Dict[str, int]:
    return {key: after.get(key, 0) - before.get(key, 0) for key in after}


def summarize(arm: str, records: List[Dict[str, Any]], metrics: Dict[str, int]) -> Dict[str, Any]:
    verified = sum(record["verification"]["verified"] for record in records)
    claimed = [record for record in records if record["reported_success"] is not None]
    false_success = sum(
        record["reported_success"] and not record["verification"]["verified"]
        for record in claimed
    )
    return {
        "arm": arm,
        "runs": len(records),
        "verified_successes": verified,
        "verified_success_rate": verified / len(records) if records else 0,
        "false_successes": false_success,
        "false_success_rate": false_success / len(claimed) if claimed else None,
        "average_duration_ms": round(
            sum(record["duration_ms"] for record in records) / len(records)
        ) if records else 0,
        "llm_metrics": metrics,
        "note": "重复运行用于观察波动，不会增加独立任务数量。",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="External workflow evaluation")
    parser.add_argument("--arm", choices=(*ARMS, "all"), default="all")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--tasks", default="evals/tasks")
    parser.add_argument("--task", help="只运行指定任务目录名")
    parser.add_argument("--results", default="evals/results")
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat 必须至少为 1")

    task_root = Path(args.tasks)
    result_root = Path(args.results)
    result_root.mkdir(parents=True, exist_ok=True)
    tasks = discover_tasks(task_root, args.task)
    arms: Iterable[str] = ARMS if args.arm == "all" else (args.arm,)
    summaries = []
    timestamp = time.strftime("%Y%m%d-%H%M%S")

    for arm in arms:
        metrics = RunMetrics()
        client = load_client(on_event=metrics.handle_event)
        records = []
        for task in tasks:
            requirement = (task / "requirement.md").read_text(encoding="utf-8")
            for repetition in range(1, args.repeat + 1):
                candidate = run_candidate(
                    arm, requirement, client, result_root / "runs" / arm
                )
                verification = verify_code(
                    candidate.pop("code"), str(task / "hidden_tests.py")
                )
                record = {
                    "arm": arm,
                    "task_id": task.name,
                    "repetition": repetition,
                    **candidate,
                    "verification": verification,
                }
                records.append(record)
                print(
                    f"{arm:10s} {task.name:20s} #{repetition} "
                    f"verified={verification['verified']} "
                    f"reported={candidate['reported_success']}"
                )
        before = {key: 0 for key in metrics.summary()}
        summary = summarize(arm, records, metric_delta(before, metrics.summary()))
        summaries.append(summary)
        payload = {"summary": summary, "records": records}
        out = result_root / f"{arm}-{timestamp}.json"
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已写入: {out}")

    comparison = result_root / f"comparison-{timestamp}.json"
    comparison.write_text(
        json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    print(f"对照汇总已写入: {comparison}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
