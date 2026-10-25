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

from workflow.agents import REDUCERS, default_registry, extract_code, extract_json, merge_reviews
from workflow.engine import FlowError, WorkflowEngine

SEQ_FLOW = os.path.join(os.path.dirname(__file__), "..", "flows", "code_review.json")
PAR_FLOW = os.path.join(os.path.dirname(__file__), "..", "flows", "code_review_parallel.json")


class FakeClient:
    """Scripted LLM stand-in: answers by inspecting the system prompt.

    The correctness reviewer fails the first review and passes the second,
    so the workflow exercises the conditional branch *and* the loop edge
    (reviews -> fixer -> reviews). The style reviewer always passes with
    one low-severity nit.
    """

    def __init__(self) -> None:
        self.review_calls = 0
        self.style_calls = 0

    def chat(self, messages, temperature: float = 0.2):  # noqa: ARG002
        system = messages[0]["content"]
        if "工程师" in system and "修复" in system:
            content = "```python\ndef fixed():\n    return 42\n```"
        elif "风格审查员" in system:
            self.style_calls += 1
            content = json.dumps({
                "passed": True,
                "issues": [{"severity": "low", "desc": "函数名可以更语义化"}],
                "summary": "风格良好",
            })
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


class TestReducer(unittest.TestCase):
    def test_merge_reviews_all_pass(self):
        out = merge_reviews({
            "correctness": {"passed": True, "issues": [], "summary": "ok"},
            "style": {"passed": True, "issues": [], "summary": "ok"},
        })
        self.assertTrue(out["passed"])
        self.assertEqual(out["issues"], [])

    def test_merge_reviews_any_fail_blocks(self):
        out = merge_reviews({
            "correctness": {"passed": True, "issues": [], "summary": "ok",
                            "run_result": "[exit_code=0]"},
            "style": {"passed": False,
                      "issues": [{"severity": "medium", "desc": "命名差"}],
                      "summary": "bad"},
        })
        self.assertFalse(out["passed"])
        self.assertEqual(out["issues"][0]["source"], "style")
        self.assertEqual(out["run_result"], "[exit_code=0]")


class TestSequentialWorkflow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client = FakeClient()
        self.engine = WorkflowEngine.from_file(
            SEQ_FLOW, default_registry(self.client),
            log_dir=self.tmp.name, reducers=REDUCERS,
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
        # run_start 携带完整流程定义（供离线回放），消息也写入日志
        self.assertIn("flow_def", events[0])
        self.assertTrue(any(e["event"] == "message" for e in events))

    def test_loop_guard(self):
        # reviewer 永远不通过 -> 触发节点访问上限，优雅失败而非死循环
        class NeverPass(FakeClient):
            def chat(self, messages, temperature=0.2):
                self.review_calls += 1
                return {"choices": [{"message": {"content": json.dumps(
                    {"passed": False, "issues": [], "summary": "x"})}}]}

        engine = WorkflowEngine.from_file(
            SEQ_FLOW, default_registry(NeverPass()),
            log_dir=self.tmp.name, reducers=REDUCERS, max_node_visits=3,
        )
        state = engine.run({"requirement": "x"})
        self.assertEqual(state.status, "failed")
        self.assertIn("中止", state.error)

    def test_node_failure_is_caught(self):
        class Boom:
            def chat(self, messages, temperature=0.2):
                raise RuntimeError("API 挂了")

        engine = WorkflowEngine.from_file(
            SEQ_FLOW, default_registry(Boom()),
            log_dir=self.tmp.name, reducers=REDUCERS, node_retries=0,
        )
        state = engine.run({"requirement": "x"})
        self.assertEqual(state.status, "failed")
        self.assertIn("API 挂了", state.error)

    def test_bad_flow_rejected(self):
        with self.assertRaises(FlowError):
            WorkflowEngine({"start": "ghost", "nodes": [], "edges": []}, agents={})

    def test_mermaid(self):
        text = self.engine.to_mermaid()
        self.assertIn("flowchart TD", text)
        self.assertIn("reviewer", text)
        self.assertIn("END", text)


class TestParallelWorkflow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client = FakeClient()
        self.engine = WorkflowEngine.from_file(
            PAR_FLOW, default_registry(self.client),
            log_dir=self.tmp.name, reducers=REDUCERS,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_parallel_reviews_run_and_merge(self):
        state = self.engine.run({"requirement": "写一个除法函数"})
        self.assertEqual(state.status, "success")
        # 两轮审查：第一轮正确性不通过 -> 修复 -> 第二轮通过
        self.assertEqual(self.client.review_calls, 2)
        self.assertEqual(self.client.style_calls, 2)  # 风格审查每轮都并行跑了
        # 两个并行任务的输出都写入了黑板各自的键
        self.assertIn("review_correctness", state.data)
        self.assertIn("review_style", state.data)
        # 合并结果：通过 + 风格 issue 带来了 source 标签
        merged = state.get("review")
        self.assertTrue(merged["passed"])
        self.assertEqual(merged["issues"][0]["source"], "style")
        # 并行步骤日志里有两个任务的耗时明细
        with open(state.get("_run_log"), encoding="utf-8") as f:
            steps = [json.loads(l) for l in f
                     if json.loads(l)["event"] == "step"]
        par = next(s for s in steps if s["node"] == "reviews")
        self.assertEqual({t["task"] for t in par["tasks"]},
                         {"correctness", "style"})

    def test_style_failure_also_triggers_fix(self):
        class StyleFail(FakeClient):
            def chat(self, messages, temperature=0.2):
                system = messages[0]["content"]
                if "风格审查员" in system:
                    self.style_calls += 1
                    passed = self.style_calls > 1  # 第一轮风格不通过
                    return {"choices": [{"message": {"content": json.dumps(
                        {"passed": passed,
                         "issues": [] if passed else [
                             {"severity": "medium", "desc": "命名不规范"}],
                         "summary": "x"})}}]}
                return super().chat(messages, temperature)

        client = StyleFail()
        engine = WorkflowEngine.from_file(
            PAR_FLOW, default_registry(client),
            log_dir=self.tmp.name, reducers=REDUCERS,
        )
        state = engine.run({"requirement": "x"})
        self.assertEqual(state.status, "success")
        # 即使正确性第一轮通过，风格不通过也会进入修复循环
        self.assertGreaterEqual(client.review_calls, 2)

    def test_parallel_validation(self):
        with open(PAR_FLOW, encoding="utf-8") as f:
            base = json.load(f)
        # 输出键冲突
        dup = json.loads(json.dumps(base))
        dup["nodes"][1]["tasks"][1]["output"] = "review_correctness"
        with self.assertRaises(FlowError):
            WorkflowEngine(dup, default_registry(FakeClient()), reducers=REDUCERS)
        # 未注册 reducer
        bad = json.loads(json.dumps(base))
        bad["nodes"][1]["reduce"] = "不存在的reducer"
        with self.assertRaises(FlowError):
            WorkflowEngine(bad, default_registry(FakeClient()), reducers=REDUCERS)
        # 任务数不足
        few = json.loads(json.dumps(base))
        few["nodes"][1]["tasks"] = few["nodes"][1]["tasks"][:1]
        with self.assertRaises(FlowError):
            WorkflowEngine(few, default_registry(FakeClient()), reducers=REDUCERS)

    def test_mermaid_parallel(self):
        text = self.engine.to_mermaid()
        self.assertIn("并行", text)
        self.assertIn("reviewer_style", text)


class TestViz(unittest.TestCase):
    def test_replay_html(self):
        from workflow.viz import render_replay
        with tempfile.TemporaryDirectory() as d:
            client = FakeClient()
            engine = WorkflowEngine.from_file(
                PAR_FLOW, default_registry(client),
                log_dir=d, reducers=REDUCERS,
            )
            state = engine.run({"requirement": "x"})
            with open(state.get("_run_log"), encoding="utf-8") as f:
                events = [json.loads(l) for l in f]
            out = os.path.join(d, "replay.html")
            render_replay(engine.flow, events,
                          {"code": state.get("code"), "review": state.get("review")},
                          out)
            with open(out, encoding="utf-8") as f:
                page = f.read()
            self.assertIn("<svg", page)          # 流程图
            self.assertIn("reviews", page)       # 并行节点
            self.assertIn("运行回放", page)
            self.assertIn("def fixed", page)     # 最终代码产物


if __name__ == "__main__":
    unittest.main(verbosity=2)
