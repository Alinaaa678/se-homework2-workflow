"""The workflow engine: declarative DAG + conditional edges + loop guard.

A flow is defined in JSON (see flows/code_review.json):

- ``nodes``: ``id``, ``agent`` (registry name), ``input`` (mapping of
  argument name -> blackboard key), ``output`` (blackboard key to write).
- ``edges``: ``from`` / ``to`` (``"END"`` finishes the run) and an
  optional ``when`` expression evaluated against the blackboard.
  The first edge whose condition holds is followed, so order encodes
  priority — this gives conditional branching *and* loops (an edge may
  point back to an earlier node).

Safety / robustness:

- ``max_steps`` and a per-node visit cap prevent infinite loops.
- A failing node is retried ``node_retries`` times; after that the run
  ends with ``status=failed`` and the error is recorded — the engine
  never throws past ``run()``.
- Every step is appended to a JSONL run log (timestamp, node, duration,
  status, error) for state tracking and post-mortem analysis.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from .state import WorkflowState

END = "END"


class FlowError(Exception):
    """Raised for malformed flow definitions."""


def _eval_when(expr: str, state: WorkflowState) -> bool:
    """Evaluate an edge condition against the blackboard.

    Runs with no builtins; only ``state`` (the blackboard dict) and a
    few safe helpers are in scope.
    """
    env = {
        "state": state.data,
        "len": len,
        "any": any,
        "all": all,
        "bool": bool,
        "str": str,
        "int": int,
    }
    try:
        return bool(eval(expr, {"__builtins__": {}}, env))  # noqa: S307
    except Exception as e:
        raise FlowError(f"条件表达式求值失败 {expr!r}: {e}")


class WorkflowEngine:
    """Executes a declarative flow over a shared WorkflowState."""

    def __init__(
        self,
        flow: Dict[str, Any],
        agents: Dict[str, Any],
        max_steps: int = 20,
        max_node_visits: int = 5,
        node_retries: int = 1,
        log_dir: str = "runs",
        on_step: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.flow = flow
        self.agents = agents
        self.max_steps = max_steps
        self.max_node_visits = max_node_visits
        self.node_retries = node_retries
        self.log_dir = log_dir
        self.on_step = on_step  # UI callback, keeps the engine print-free
        self._validate()

    # ------------------------------------------------------------------ #
    @classmethod
    def from_file(cls, path: str, agents: Dict[str, Any], **kw: Any) -> "WorkflowEngine":
        with open(path, "r", encoding="utf-8") as f:
            return cls(json.load(f), agents, **kw)

    def _validate(self) -> None:
        node_ids = {n["id"] for n in self.flow.get("nodes", [])}
        if self.flow.get("start") not in node_ids:
            raise FlowError(f"start 节点 {self.flow.get('start')!r} 不存在")
        for n in self.flow.get("nodes", []):
            if n["agent"] not in self.agents:
                raise FlowError(f"节点 {n['id']!r} 引用了未注册的 Agent {n['agent']!r}")
        for e in self.flow.get("edges", []):
            for key in ("from", "to"):
                if e[key] != END and e[key] not in node_ids:
                    raise FlowError(f"边引用了不存在的节点: {e}")

    # ------------------------------------------------------------------ #
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
        # No outgoing edge matched -> implicit END.
        return END

    # ------------------------------------------------------------------ #
    def run(self, initial: Optional[Dict[str, Any]] = None) -> WorkflowState:
        """Execute the flow; returns the final state (never raises)."""
        state = WorkflowState(initial)
        state.status = "running"
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        os.makedirs(self.log_dir, exist_ok=True)
        log_path = os.path.join(self.log_dir, f"run-{run_id}.jsonl")

        visits: Dict[str, int] = {}
        current = self.flow["start"]
        prev = "START"

        with open(log_path, "w", encoding="utf-8") as log:
            log.write(json.dumps({
                "event": "run_start", "run_id": run_id,
                "flow": self.flow.get("name"), "ts": time.time(),
            }, ensure_ascii=False) + "\n")

            while current != END:
                if len(visits) and sum(visits.values()) >= self.max_steps:
                    state.status = "failed"
                    state.error = f"超过最大步数 {self.max_steps}，疑似死循环，已中止。"
                    break
                visits[current] = visits.get(current, 0) + 1
                if visits[current] > self.max_node_visits:
                    state.status = "failed"
                    state.error = (
                        f"节点 {current!r} 执行超过 {self.max_node_visits} 次，已中止。"
                    )
                    break

                node = next(n for n in self.flow["nodes"] if n["id"] == current)
                agent = self.agents[node["agent"]]
                step: Dict[str, Any] = {
                    "event": "step", "run_id": run_id, "node": current,
                    "agent": node["agent"], "visit": visits[current],
                    "ts": time.time(),
                }

                try:
                    inputs = self._resolve_inputs(node, state)
                    state.post_message(prev, current, "handoff",
                                       {k: _brief(v) for k, v in inputs.items()})
                    start = time.time()
                    output = self._call_with_retry(agent, inputs)
                    step["duration_ms"] = round((time.time() - start) * 1000)
                    state.set(node["output"], output)
                    state.post_message(current, current, "artifact",
                                       {node["output"]: _brief(output)})
                    step["status"] = "ok"
                    step["output_key"] = node["output"]
                except Exception as e:  # node failure -> graceful stop
                    state.status = "failed"
                    state.error = f"节点 {current!r} 失败: {e}"
                    step["status"] = "error"
                    step["error"] = str(e)
                    log.write(json.dumps(step, ensure_ascii=False) + "\n")
                    if self.on_step:
                        self.on_step(step)
                    break

                log.write(json.dumps(step, ensure_ascii=False) + "\n")
                if self.on_step:
                    self.on_step(step)
                prev, current = current, self._next_node(current, state)

            if state.status == "running":
                state.status = "success"
            log.write(json.dumps({
                "event": "run_end", "run_id": run_id, "ts": time.time(),
                **state.summary(),
            }, ensure_ascii=False) + "\n")

        state.data["_run_log"] = log_path
        return state

    def _call_with_retry(self, agent: Any, inputs: Dict[str, Any]) -> Any:
        last: Optional[Exception] = None
        for attempt in range(self.node_retries + 1):
            try:
                return agent.run(inputs)
            except Exception as e:
                last = e
                if attempt < self.node_retries:
                    time.sleep(1)
        raise last  # type: ignore[misc]

    # ------------------------------------------------------------------ #
    def to_mermaid(self) -> str:
        """Render the flow definition as a Mermaid diagram."""
        lines = ["flowchart TD"]
        labels = {n["id"]: f"{n['id']}({n['agent']})" for n in self.flow["nodes"]}
        for nid, label in labels.items():
            lines.append(f"    {nid}[{label}]")
        for e in self.flow["edges"]:
            dst = "END((END))" if e["to"] == END else e["to"]
            when = e.get("when")
            arrow = f"-->|{when}|" if when else "-->"
            lines.append(f"    {e['from']} {arrow} {dst}")
        return "\n".join(lines)


def _brief(value: Any, limit: int = 120) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"
