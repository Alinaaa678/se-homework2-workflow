"""Offline tests: drive the whole workflow with a FakeLLM (no API key).

Run:  python tests/test_workflow.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from workflow.agents import default_registry, extract_code, extract_json
from workflow.engine import FlowError, WorkflowEngine


class FakeClient:
    """Scripted LLM stand-in: answers by inspecting the system prompt.

    The reviewer fails the first review and passes the second, so the
    workflow exercises the conditional branch *and* the loop edge
    (reviewer -> fixer -> reviewer).
    """

    def __init__(self) -> None:
        self.review_calls = 0

    def chat(self, messages, temperature: float = 0.2):  # noqa: ARG002
        system = messages[0]["content"]
        if "工程师" in system and "修复" in system:
            content = "```python\ndef fixed():\n    return 42\n```"
        elif "审查员" in system:
            self.review_calls += 1
            if self.review_calls == 1:
                content = json.dumps({
                    "passed": False,
                    "issues": [{"severity": "high", "desc": "除零未处理"}],
                    "summary": "存在严重问题",
                })
            else:
                content = json.dumps({
                    "passed": True, "issues": [], "summary": "没有问题",
                })
        else:  # coder
            content = "```python\ndef divide(a, b):\n    return a / b\n```"
        return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class TestParsers(unittest.TestCase):
    def test_extract_code_fenced(self):
        self.assertEqual(extract_code("前言\n```python\nx = 1\n```\n后记"), "x = 1")

    def test_extract_code_raw_fallback(self):
        self.assertEqual(extract_code("x = 1"), "x = 1")

    def test_extract_json_with_prose(self):
        out = extract_json('好的 ```json {"passed": true} ``` 完毕')
        self.assertEqual(out, {"passed": True})

    def test_extract_json_missing(self):
        with self.assertRaises(ValueError):
            extract_json("没有 JSON")


class TestWorkflow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        flow_path = os.path.join(
            os.path.dirname(__file__), "..", "flows", "code_review.json"
        )
        self.client = FakeClient()
        self.engine = WorkflowEngine.from_file(
            flow_path,
            default_registry(self.client),
            log_dir=self.tmp.name,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_loop_coder_reviewer_fixer_reviewer(self):
        state = self.engine.run({"requirement": "写一个除法函数"})
        self.assertEqual(state.status, "success")
        # coder 1 次，reviewer 2 次（不通过 -> 通过），fixer 1 次
        self.assertEqual(self.client.review_calls, 2)
        self.assertEqual(state.get("review")["passed"], True)
        self.assertIn("fixed", state.get("code"))
        # 消息传递留痕：handoff + artifact 消息都存在
        kinds = {m["kind"] for m in state.messages}
        self.assertEqual(kinds, {"handoff", "artifact"})
        senders = {m["from"] for m in state.messages}
        self.assertTrue({"coder", "reviewer", "fixer"} <= senders)
        # 运行日志已落盘且每步有状态
        log_path = state.get("_run_log")
        self.assertTrue(os.path.exists(log_path))
        with open(log_path, encoding="utf-8") as f:
            events = [json.loads(line) for line in f]
        steps = [e for e in events if e["event"] == "step"]
        self.assertEqual(len(steps), 4)
        self.assertTrue(all(s["status"] == "ok" for s in steps))
        self.assertEqual(events[-1]["event"], "run_end")

    def test_loop_guard(self):
        # reviewer 永远不通过 -> 触发节点访问上限，优雅失败而非死循环
        class NeverPass(FakeClient):
            def chat(self, messages, temperature=0.2):
                self.review_calls += 1
                return {"choices": [{"message": {"content": json.dumps(
                    {"passed": False, "issues": [], "summary": "x"})}}]}

        engine = WorkflowEngine.from_file(
            os.path.join(os.path.dirname(__file__), "..", "flows", "code_review.json"),
            default_registry(NeverPass()),
            log_dir=self.tmp.name,
            max_node_visits=3,
        )
        state = engine.run({"requirement": "x"})
        self.assertEqual(state.status, "failed")
        self.assertIn("中止", state.error)

    def test_node_failure_is_caught(self):
        class Boom:
            def chat(self, messages, temperature=0.2):
                raise RuntimeError("API 挂了")

        engine = WorkflowEngine.from_file(
            os.path.join(os.path.dirname(__file__), "..", "flows", "code_review.json"),
            default_registry(Boom()),
            log_dir=self.tmp.name,
            node_retries=0,
        )
        state = engine.run({"requirement": "x"})
        self.assertEqual(state.status, "failed")
        self.assertIn("API 挂了", state.error)

    def test_bad_flow_rejected(self):
        with self.assertRaises(FlowError):
            WorkflowEngine(
                {"start": "ghost", "nodes": [], "edges": []}, agents={}
            )

    def test_mermaid(self):
        text = self.engine.to_mermaid()
        self.assertIn("flowchart TD", text)
        self.assertIn("reviewer", text)
        self.assertIn("END", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
