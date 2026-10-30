"""Deterministic workflow engine with parallel stages and durable checkpoints.

A node is re-executed when a process crashed before its checkpoint. This is
at-least-once execution, not a claim of exactly-once external side effects.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional

from .conditions import evaluate_condition
from .metrics import RunMetrics, merge_metrics
from .state import WorkflowState
from .store import WorkflowStore, flow_hash

END = "END"


class FlowError(Exception):
    """Raised for malformed flow definitions or incompatible resumes."""


def _eval_when(expr: str, state: WorkflowState) -> bool:
    try:
        return bool(evaluate_condition(expr, state.data))
    except Exception as exc:
        raise FlowError(f"条件表达式求值失败 {expr!r}: {exc}")


def _write(log: Any, event: Dict[str, Any]) -> None:
    """Append and flush every event so the JSONL journal survives a crash."""
    log.write(json.dumps(event, ensure_ascii=False) + "\n")
    log.flush()


class WorkflowEngine:
    def __init__(
        self,
        flow: Dict[str, Any],
        agents: Dict[str, Any],
        max_steps: int = 30,
        max_node_visits: int = 5,
        node_retries: int = 1,
        log_dir: str = "runs",
        on_step: Optional[Callable[[Dict[str, Any]], None]] = None,
        reducers: Optional[Dict[str, Callable[[Dict[str, Any]], Any]]] = None,
        store_path: Optional[str] = None,
        metrics: Optional[RunMetrics] = None,
    ) -> None:
        self.flow = flow
        self.agents = agents
        self.max_steps = max_steps
        self.max_node_visits = max_node_visits
        self.node_retries = node_retries
        self.log_dir = log_dir
        self.on_step = on_step
        self.reducers = reducers or {}
        self.metrics = metrics or RunMetrics()
        os.makedirs(self.log_dir, exist_ok=True)
        self.store = WorkflowStore(store_path or os.path.join(self.log_dir, "workflow.db"))
        self._validate()

    @classmethod
    def from_file(cls, path: str, agents: Dict[str, Any], **kw: Any) -> "WorkflowEngine":
        with open(path, "r", encoding="utf-8") as handle:
            return cls(json.load(handle), agents, **kw)

    def _validate(self) -> None:
        nodes = self.flow.get("nodes", [])
        ids = [node.get("id") for node in nodes]
        node_ids = set(ids)
        if len(ids) != len(node_ids):
            raise FlowError("节点 id 不能重复")
        if self.flow.get("start") not in node_ids:
            raise FlowError(f"start 节点 {self.flow.get('start')!r} 不存在")
        for node in nodes:
            if node.get("type") == "parallel":
                self._validate_parallel(node)
            elif node.get("agent") not in self.agents:
                raise FlowError(f"节点 {node['id']!r} 引用了未注册的 Agent {node.get('agent')!r}")
            elif not node.get("output"):
                raise FlowError(f"普通节点 {node['id']!r} 缺少 output 键")
        for edge in self.flow.get("edges", []):
            for key in ("from", "to"):
                if edge[key] != END and edge[key] not in node_ids:
                    raise FlowError(f"边引用了不存在的节点: {edge}")

    def _validate_parallel(self, node: Dict[str, Any]) -> None:
        tasks = node.get("tasks") or []
        if len(tasks) < 2:
            raise FlowError(f"并行节点 {node['id']!r} 至少需要 2 个任务")
        outputs = [task.get("output") for task in tasks]
        if len(set(outputs)) != len(outputs):
            raise FlowError(f"并行节点 {node['id']!r} 的任务输出键必须互不相同: {outputs}")
        for task in tasks:
            if task.get("agent") not in self.agents:
                raise FlowError(f"并行任务 {task.get('id')!r} 引用了未注册的 Agent")
            if not task.get("output"):
                raise FlowError(f"并行任务 {task.get('id')!r} 缺少 output 键")
        if node.get("reduce"):
            if node["reduce"] not in self.reducers:
                raise FlowError(f"并行节点 {node['id']!r} 引用了未注册的 reducer")
            if not node.get("output"):
                raise FlowError(f"并行节点 {node['id']!r} 使用 reduce 时必须声明 output 键")

    def _resolve_inputs(self, node: Dict[str, Any], state: WorkflowState) -> Dict[str, Any]:
        inputs = {}
        for arg, key in node.get("input", {}).items():
            if key not in state.data:
                raise FlowError(f"节点 {node['id']!r} 需要的输入 {key!r} 不在状态中")
            inputs[arg] = state.data[key]
        return inputs

    def _next_node(self, node_id: str, state: WorkflowState) -> str:
        for edge in self.flow.get("edges", []):
            if edge["from"] != node_id:
                continue
            when = edge.get("when")
            if when is None or _eval_when(when, state):
                return edge["to"]
        return END

    def run(self, initial: Optional[Dict[str, Any]] = None) -> WorkflowState:
        """Start a new run and persist its initial checkpoint."""
        state = WorkflowState(initial)
        state.status = "running"
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        log_path = os.path.join(self.log_dir, f"run-{run_id}.jsonl")
        state.data["_run_log"] = log_path
        current = self.flow["start"]
        prev = "START"
        visits: Dict[str, int] = {}
        self.store.save_checkpoint(run_id, self.flow, state, current, prev, visits, log_path)
        with open(log_path, "w", encoding="utf-8") as log:
            _write(log, {
                "event": "run_start", "run_id": run_id, "flow": self.flow.get("name"),
                "flow_hash": flow_hash(self.flow), "flow_def": self.flow, "ts": time.time(),
            })
            return self._execute(log, run_id, state, current, prev, visits, log_path)

    def resume(self, run_id: str) -> WorkflowState:
        """Resume the latest checkpoint, rejecting a changed flow definition."""
        record = self.store.load_run(run_id)
        if record["flow_hash"] != flow_hash(self.flow):
            raise FlowError("流程定义已变化，拒绝直接恢复；请创建新运行或恢复原流程版本")
        state = WorkflowState.from_dict(record["state"])
        log_path = record["log_path"]
        state.data["_run_log"] = log_path
        if record["current_node"] == END and record["status"] == "success":
            return state
        state.status = "running"
        state.error = None
        with open(log_path, "a", encoding="utf-8") as log:
            _write(log, {"event": "run_resume", "run_id": run_id, "ts": time.time()})
            return self._execute(
                log, run_id, state, record["current_node"], record["prev_node"],
                dict(record["visits"]), log_path,
            )

    def inspect(self, run_id: str) -> Dict[str, Any]:
        return self.store.load_run(run_id)

    def _execute(
        self,
        log: Any,
        run_id: str,
        state: WorkflowState,
        current: str,
        prev: str,
        visits: Dict[str, int],
        log_path: str,
    ) -> WorkflowState:
        previous_metrics = dict(state.data.get("_metrics") or {})
        msg_cursor = len(state.messages)

        def checkpoint() -> None:
            state.data["_metrics"] = merge_metrics(previous_metrics, self.metrics.summary())
            self.store.save_checkpoint(
                run_id, self.flow, state, current, prev, visits, log_path
            )

        while current != END:
            if sum(visits.values()) >= self.max_steps:
                state.status = "failed"
                state.error = f"超过最大步数 {self.max_steps}，疑似死循环，已中止。"
                break
            visits[current] = visits.get(current, 0) + 1
            if visits[current] > self.max_node_visits:
                state.status = "failed"
                state.error = f"节点 {current!r} 执行超过 {self.max_node_visits} 次，已中止。"
                break

            node = next(item for item in self.flow["nodes"] if item["id"] == current)
            is_parallel = node.get("type") == "parallel"
            step: Dict[str, Any] = {
                "event": "step", "run_id": run_id, "node": current,
                "agent": "parallel" if is_parallel else node["agent"],
                "visit": visits[current], "ts": time.time(),
                "idempotency_key": f"{run_id}:{current}:{visits[current]}",
            }
            started = time.time()
            try:
                if is_parallel:
                    output = self._run_parallel_node(node, state, prev, step, visits[current])
                else:
                    output = self._run_agent_node(node, state, prev, visits[current])
                step["duration_ms"] = round((time.time() - started) * 1000)
                step["status"] = "ok"
                step["output_key"] = node.get("output") or [
                    task["output"] for task in node.get("tasks", [])
                ]
            except Exception as exc:
                step["duration_ms"] = round((time.time() - started) * 1000)
                state.status = "failed"
                state.error = f"节点 {current!r} 失败: {exc}"
                step["status"] = "error"
                step["error"] = str(exc)
                self.metrics.record_node(step)
                _write(log, step)
                for message in state.messages[msg_cursor:]:
                    _write(log, {"event": "message", **message})
                if self.on_step:
                    self.on_step(step)
                break

            self.metrics.record_node(step)
            _write(log, step)
            for message in state.messages[msg_cursor:]:
                _write(log, {"event": "message", **message})
            msg_cursor = len(state.messages)

            if is_parallel:
                artifact_items = [
                    (task["output"], state.get(task["output"]))
                    for task in node.get("tasks", [])
                ]
                if node.get("reduce"):
                    artifact_items.append((node["output"], output))
            else:
                artifact_items = [(node["output"], output)]
            for key, value in artifact_items:
                metadata = state.artifacts[key]
                _write(log, {
                    "event": "artifact", "run_id": run_id, "node": current,
                    "visit": visits[current], "key": key, "value": value,
                    "hash": metadata["hash"], "version": metadata["version"],
                    "producer": metadata["producer"], "ts": time.time(),
                })
                self.store.record_artifact(
                    run_id, current, visits[current], key, value, metadata
                )

            if self.on_step:
                self.on_step(step)
            try:
                next_node = self._next_node(current, state)
            except Exception as exc:
                state.status = "failed"
                state.error = f"节点 {current!r} 的出边求值失败: {exc}"
                _write(log, {
                    "event": "route_error", "run_id": run_id, "node": current,
                    "error": str(exc), "ts": time.time(),
                })
                break
            prev, current = current, next_node
            checkpoint()

        if state.status == "running":
            state.status = "success"
        state.data["_metrics"] = merge_metrics(previous_metrics, self.metrics.summary())
        _write(log, {
            "event": "run_end", "run_id": run_id, "ts": time.time(),
            **state.summary(), "metrics": state.data["_metrics"],
        })
        checkpoint()
        state.data["_run_log"] = log_path
        return state

    def _run_agent_node(
        self, node: Dict[str, Any], state: WorkflowState, prev: str, visit: int
    ) -> Any:
        inputs = self._resolve_inputs(node, state)
        state.post_message(prev, node["id"], "handoff", {
            key: _brief(value) for key, value in inputs.items()
        })
        output = self._call_with_retry(self.agents[node["agent"]], inputs)
        state.set(node["output"], output)
        state.track_artifact(node["output"], output, node["id"], visit)
        state.post_message(node["id"], node["id"], "artifact", {
            node["output"]: _brief(output)
        })
        return output

    def _run_parallel_node(
        self, node: Dict[str, Any], state: WorkflowState, prev: str,
        step: Dict[str, Any], visit: int,
    ) -> Any:
        tasks = node["tasks"]
        task_inputs = {task["id"]: self._resolve_inputs(task, state) for task in tasks}
        for task in tasks:
            state.post_message(
                prev, f"{node['id']}.{task['id']}", "handoff",
                {key: _brief(value) for key, value in task_inputs[task["id"]].items()},
            )

        def work(task: Dict[str, Any]) -> Dict[str, Any]:
            started = time.time()
            result = self._call_with_retry(
                self.agents[task["agent"]], task_inputs[task["id"]]
            )
            return {
                "task": task, "result": result,
                "duration_ms": round((time.time() - started) * 1000),
            }

        results: Dict[str, Any] = {}
        step["tasks"] = []
        with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
            futures = [pool.submit(work, task) for task in tasks]
            for future in as_completed(futures):
                done = future.result()
                task, output = done["task"], done["result"]
                results[task["id"]] = output
                state.set(task["output"], output)
                state.track_artifact(task["output"], output, task["id"], visit)
                state.post_message(
                    f"{node['id']}.{task['id']}", node["id"], "artifact",
                    {task["output"]: _brief(output)},
                )
                step["tasks"].append({
                    "task": task["id"], "agent": task["agent"],
                    "duration_ms": done["duration_ms"], "output_key": task["output"],
                })

        if node.get("reduce"):
            reduced = self.reducers[node["reduce"]](results)
            state.set(node["output"], reduced)
            state.track_artifact(node["output"], reduced, node["id"], visit)
            state.post_message(node["id"], node["id"], "artifact", {
                node["output"]: _brief(reduced)
            })
            return reduced
        return results

    def _call_with_retry(self, agent: Any, inputs: Dict[str, Any]) -> Any:
        last: Optional[Exception] = None
        for attempt in range(self.node_retries + 1):
            try:
                return agent.run(inputs)
            except Exception as exc:
                last = exc
                if attempt < self.node_retries:
                    time.sleep(1)
        raise last  # type: ignore[misc]

    def to_mermaid(self) -> str:
        lines = ["flowchart TD"]
        labels = {}
        for node in self.flow["nodes"]:
            if node.get("type") == "parallel":
                names = "+".join(task["agent"] for task in node["tasks"])
                labels[node["id"]] = f"{node['id']} 并行({names})"
            else:
                labels[node["id"]] = f"{node['id']}({node['agent']})"
        for node_id, label in labels.items():
            lines.append(f"    {node_id}[{label}]")
        for edge in self.flow.get("edges", []):
            destination = "END((END))" if edge["to"] == END else edge["to"]
            when = edge.get("when")
            arrow = f"-->|{when}|" if when else "-->"
            lines.append(f"    {edge['from']} {arrow} {destination}")
        return "\n".join(lines)


def _brief(value: Any, limit: int = 120) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"
