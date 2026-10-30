"""Tests for evidence gates, durable resume, safe routing and telemetry."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from workflow.agents import VerdictGate
from workflow.artifacts import content_hash
from workflow.conditions import evaluate_condition
from workflow.engine import FlowError, WorkflowEngine
from workflow.llm import ChatClient
from workflow.metrics import RunMetrics
from workflow.spec import build_spec
from workflow.tools import run_acceptance


class TestEvidenceGate(unittest.TestCase):
    def test_acceptance_exit_code_not_model_claim_controls_result(self):
        passing = run_acceptance(
            "def add(a, b):\n    return a + b\n",
            "import unittest\nfrom candidate import add\n"
            "class T(unittest.TestCase):\n"
            "    def test_add(self): self.assertEqual(add(1, 2), 3)\n",
        )
        failing = run_acceptance(
            "def add(a, b):\n    return a - b\n",
            "import unittest\nfrom candidate import add\n"
            "class T(unittest.TestCase):\n"
            "    def test_add(self): self.assertEqual(add(1, 2), 3)\n",
        )
        self.assertTrue(passing["passed"])
        self.assertFalse(failing["passed"])
        self.assertNotEqual(failing["exit_code"], 0)

    def test_stale_review_cannot_authorize_current_code(self):
        code = "def add(a, b):\n    return a + b\n"
        spec = build_spec("1. 实现加法")
        verdict = VerdictGate().run({
            "code": code,
            "review": {"passed": True, "target_code_hash": content_hash("old")},
            "acceptance": {
                "passed": True,
                "target_code_hash": content_hash(code),
                "spec_hash": spec["spec_hash"],
            },
            "spec": spec,
        })
        self.assertFalse(verdict["passed"])
        self.assertIn("review_current", verdict["reasons"])

    def test_spec_has_stable_ids_and_content_hash(self):
        first = build_spec("目标\n1. 添加任务\n2. 完成任务")
        second = build_spec("目标\n1. 添加任务\n2. 完成任务")
        self.assertEqual(first, second)
        self.assertEqual([item["id"] for item in first["requirements"]], ["R001", "R002"])
        self.assertEqual(first["spec_hash"], content_hash(first["source"]))


class CountingAgent:
    def __init__(self, value="ok"):
        self.value = value
        self.calls = 0

    def run(self, inputs):
        self.calls += 1
        return self.value


class CrashOnceAgent(CountingAgent):
    def run(self, inputs):
        self.calls += 1
        if self.calls == 1:
            raise SystemExit("simulated process crash")
        return self.value


class TestDurableResume(unittest.TestCase):
    @staticmethod
    def flow():
        return {
            "name": "resume-test", "start": "a",
            "nodes": [
                {"id": "a", "agent": "a", "input": {}, "output": "a"},
                {"id": "b", "agent": "b", "input": {}, "output": "b"},
            ],
            "edges": [{"from": "a", "to": "b"}],
        }

    def test_crash_resume_does_not_repeat_checkpointed_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = CountingAgent("A")
            second = CrashOnceAgent("B")
            run_ids = []
            engine = WorkflowEngine(
                self.flow(), {"a": first, "b": second}, log_dir=tmp,
                on_step=lambda step: run_ids.append(step["run_id"]),
            )
            with self.assertRaises(SystemExit):
                engine.run()
            run_id = run_ids[0]
            self.assertEqual(engine.inspect(run_id)["current_node"], "b")

            # A fresh engine simulates a new process and fresh in-memory metrics.
            resumed_engine = WorkflowEngine(
                self.flow(), {"a": first, "b": second}, log_dir=tmp
            )
            state = resumed_engine.resume(run_id)
            self.assertEqual(state.status, "success")
            self.assertEqual(first.calls, 1)
            self.assertEqual(second.calls, 2)
            self.assertEqual((state.get("a"), state.get("b")), ("A", "B"))

    def test_changed_flow_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = WorkflowEngine(
                self.flow(), {"a": CountingAgent(), "b": CrashOnceAgent()},
                log_dir=tmp, on_step=lambda step: setattr(self, "run_id", step["run_id"]),
            )
            with self.assertRaises(SystemExit):
                engine.run()
            changed = self.flow()
            changed["description"] = "incompatible version"
            with self.assertRaises(FlowError):
                WorkflowEngine(
                    changed, {"a": CountingAgent(), "b": CountingAgent()}, log_dir=tmp
                ).resume(self.run_id)

    def test_completed_resume_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = CountingAgent("done")
            flow = {
                "name": "one", "start": "a",
                "nodes": [{"id": "a", "agent": "a", "input": {}, "output": "a"}],
                "edges": [],
            }
            engine = WorkflowEngine(flow, {"a": agent}, log_dir=tmp)
            state = engine.run()
            run_id = os.path.basename(state.get("_run_log"))[4:-6]
            resumed = WorkflowEngine(flow, {"a": agent}, log_dir=tmp).resume(run_id)
            self.assertEqual(resumed.status, "success")
            self.assertEqual(agent.calls, 1)


class TestRoutingAndMetrics(unittest.TestCase):
    def test_condition_whitelist(self):
        self.assertTrue(evaluate_condition("state['x'] == 1 and not state['bad']", {
            "x": 1, "bad": False,
        }))
        with self.assertRaises(ValueError):
            evaluate_condition("state['x'].__class__", {"x": 1})

    def test_llm_usage_is_collected_without_exposing_credentials(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                }).encode("utf-8")

        metrics = RunMetrics()
        client = ChatClient(
            base_url="http://fake", api_key="secret", model="fake",
            on_event=metrics.handle_event,
        )
        with patch("urllib.request.urlopen", return_value=Response()):
            client.chat([{"role": "user", "content": "hi"}])
        summary = metrics.summary()
        self.assertEqual(summary["llm_attempts"], 1)
        self.assertEqual(summary["llm_calls"], 1)
        self.assertEqual(summary["total_tokens"], 15)


if __name__ == "__main__":
    unittest.main(verbosity=2)
