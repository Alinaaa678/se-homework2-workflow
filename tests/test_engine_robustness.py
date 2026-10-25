"""Regression tests for review contracts, concurrency, routing, and replay."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_workflow import FakeClient
from workflow.agents import REDUCERS, default_registry, merge_reviews, normalize_review
from workflow.engine import WorkflowEngine
from workflow.tools import _scrub_sensitive_env
from workflow.viz import artifacts_from_events, render_replay

PAR_FLOW = os.path.join(os.path.dirname(__file__), "..", "flows", "code_review_parallel.json")


class TestReviewContract(unittest.TestCase):
    def test_medium_issue_overrides_contradictory_model_pass(self):
        review = normalize_review({
            "passed": True,
            "issues": [{"severity": "medium", "desc": "命名不清晰"}],
            "summary": "需要修复",
        })
        self.assertFalse(review["passed"])

    def test_low_issue_does_not_block(self):
        review = normalize_review({
            "passed": True,
            "issues": [{"severity": "low", "desc": "可补充注释"}],
            "summary": "整体良好",
        })
        self.assertTrue(review["passed"])

    def test_invalid_review_schema_is_rejected(self):
        invalid = [
            {"passed": "true", "issues": [], "summary": "bad type"},
            {"passed": True, "summary": "missing issues"},
            {"passed": True, "issues": [], "summary": None},
            {"passed": True, "issues": [{"severity": "fatal", "desc": "x"}], "summary": "x"},
            {"passed": True, "issues": [{"severity": "low", "desc": " "}], "summary": "x"},
        ]
        for review in invalid:
            with self.subTest(review=review), self.assertRaises(ValueError):
                normalize_review(review)

    def test_reducer_is_a_second_line_of_defence(self):
        merged = merge_reviews({
            "correctness": {"passed": True, "issues": [], "summary": "ok"},
            "style": {
                "passed": True,
                "issues": [{"severity": "medium", "desc": "Magic Number"}],
                "summary": "needs work",
            },
        })
        self.assertFalse(merged["passed"])
        self.assertEqual(merged["issues"][0]["source"], "style")


class RecordingClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.requests = []

    def chat(self, messages, temperature: float = 0.2):
        self.requests.append(messages)
        return super().chat(messages, temperature)


class TestRequirementPropagation(unittest.TestCase):
    def test_reviewer_and_fixer_receive_original_requirement(self):
        marker = "验收标记：排序规则必须可追踪"
        with tempfile.TemporaryDirectory() as tmp:
            client = RecordingClient()
            engine = WorkflowEngine.from_file(
                PAR_FLOW, default_registry(client), log_dir=tmp, reducers=REDUCERS,
            )
            state = engine.run({"requirement": marker})
        self.assertEqual(state.status, "success")
        reviewer_seen = any(
            "正确性审查员" in request[0]["content"] and marker in request[1]["content"]
            for request in client.requests
        )
        fixer_seen = any(
            "修复" in request[0]["content"] and marker in request[1]["content"]
            for request in client.requests
        )
        self.assertTrue(reviewer_seen)
        self.assertTrue(fixer_seen)


class ConcurrentAgent:
    def __init__(self, barrier):
        self.barrier = barrier

    def run(self, inputs):
        self.barrier.wait(timeout=2)
        return inputs["value"]


class FailingAgent:
    def run(self, inputs):
        raise RuntimeError("parallel task exploded")


class ImmediateAgent:
    def run(self, inputs):
        return "ok"


class TestEngineRobustness(unittest.TestCase):
    @staticmethod
    def parallel_flow(first, second):
        return {
            "name": "parallel-test", "start": "parallel",
            "nodes": [{
                "id": "parallel", "type": "parallel",
                "tasks": [
                    {"id": "a", "agent": first, "input": {"value": "value"}, "output": "a"},
                    {"id": "b", "agent": second, "input": {"value": "value"}, "output": "b"},
                ],
            }],
            "edges": [],
        }

    def test_tasks_really_run_concurrently(self):
        # Both tasks must enter run() together; a sequential executor breaks the barrier.
        barrier = threading.Barrier(2)
        flow = self.parallel_flow("a", "b")
        with tempfile.TemporaryDirectory() as tmp:
            engine = WorkflowEngine(
                flow, {"a": ConcurrentAgent(barrier), "b": ConcurrentAgent(barrier)},
                log_dir=tmp,
            )
            state = engine.run({"value": 7})
        self.assertEqual(state.status, "success")
        self.assertEqual((state.get("a"), state.get("b")), (7, 7))

    def test_parallel_task_failure_is_logged(self):
        flow = self.parallel_flow("good", "bad")
        with tempfile.TemporaryDirectory() as tmp:
            engine = WorkflowEngine(
                flow, {"good": ImmediateAgent(), "bad": FailingAgent()},
                log_dir=tmp, node_retries=0,
            )
            state = engine.run({"value": 7})
            with open(state.get("_run_log"), encoding="utf-8") as handle:
                events = [json.loads(line) for line in handle]
        self.assertEqual(state.status, "failed")
        self.assertIn("parallel task exploded", state.error)
        self.assertEqual(events[-1]["status"], "failed")

    def test_bad_edge_condition_becomes_run_failure(self):
        flow = {
            "name": "bad-route", "start": "node",
            "nodes": [{"id": "node", "agent": "agent", "input": {}, "output": "review"}],
            "edges": [{"from": "node", "to": "END", "when": "state['review']['missing']"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            engine = WorkflowEngine(flow, {"agent": ImmediateAgent()}, log_dir=tmp)
            state = engine.run()
            with open(state.get("_run_log"), encoding="utf-8") as handle:
                events = [json.loads(line) for line in handle]
        self.assertEqual(state.status, "failed")
        self.assertIn("出边求值失败", state.error)
        self.assertTrue(any(event["event"] == "route_error" for event in events))


class TestArtifactReplay(unittest.TestCase):
    def test_full_versions_and_final_artifacts_are_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = WorkflowEngine.from_file(
                PAR_FLOW, default_registry(FakeClient()), log_dir=tmp, reducers=REDUCERS,
            )
            state = engine.run({"requirement": "写一个除法函数"})
            with open(state.get("_run_log"), encoding="utf-8") as handle:
                events = [json.loads(line) for line in handle]
            artifacts = artifacts_from_events(events)
            out = os.path.join(tmp, "replay.html")
            render_replay(engine.flow, events, artifacts, out)
            with open(out, encoding="utf-8") as handle:
                page = handle.read()
        code_versions = [
            event["value"] for event in events
            if event["event"] == "artifact" and event["key"] == "code"
        ]
        self.assertEqual(len(code_versions), 2)
        self.assertIn("def divide", code_versions[0])
        self.assertIn("def fixed", code_versions[1])
        self.assertIn("def fixed", artifacts["code"])
        self.assertTrue(artifacts["review"]["passed"])
        self.assertIn("工件版本记录", page)
        self.assertIn("def fixed", page)


class TestExecutionEnvironment(unittest.TestCase):
    def test_sensitive_environment_values_are_removed(self):
        cleaned = _scrub_sensitive_env({
            "PATH": "keep", "OPENAI_API_KEY": "secret", "SESSION_TOKEN": "secret",
            "DB_PASSWORD": "secret", "NORMAL_VALUE": "keep",
        })
        self.assertEqual(cleaned, {"PATH": "keep", "NORMAL_VALUE": "keep"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
