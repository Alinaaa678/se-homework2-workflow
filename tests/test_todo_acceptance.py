"""Acceptance tests for the tracked Todo reference implementation."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
CANDIDATE = os.path.join(ROOT, "tests", "fixtures", "todo_candidate.py")
SPEC = importlib.util.spec_from_file_location("workflow_todo_candidate", CANDIDATE)
todo_module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = todo_module
SPEC.loader.exec_module(todo_module)


class TestTodoAcceptance(unittest.TestCase):
    def setUp(self):
        self.todo = todo_module.TodoManager()
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()

    def tearDown(self):
        self.redirect.__exit__(None, None, None)

    def test_add_supports_title_priority_default_and_unique_id(self):
        first = self.todo.add("  写周报  ", "high")
        second = self.todo.add("买牛奶")
        self.assertEqual(first.title, "写周报")
        self.assertEqual(first.priority, "high")
        self.assertEqual(second.priority, "medium")
        self.assertEqual((first.id, second.id), (1, 2))

    def test_complete_by_id_and_repeated_complete_are_friendly(self):
        task = self.todo.add("完成任务")
        self.assertTrue(self.todo.complete(task.id))
        self.assertTrue(task.done)
        self.todo.complete(task.id)
        self.assertTrue(task.done)
        self.assertFalse(self.todo.complete(999))
        self.assertIn("已经是完成状态", self.output.getvalue())
        self.assertIn("找不到编号", self.output.getvalue())

    def test_list_sorts_unfinished_first_then_priority_then_id(self):
        low = self.todo.add("低优先级", "low")
        finished_high = self.todo.add("已完成高优先级", "high")
        medium = self.todo.add("中优先级")
        pending_high = self.todo.add("未完成高优先级", "high")
        self.todo.complete(finished_high.id)
        ordered = self.todo.list_tasks()
        self.assertEqual(
            [task.title for task in ordered],
            ["未完成高优先级", "中优先级", "低优先级", "已完成高优先级"],
        )
        self.assertEqual(
            [task.id for task in ordered[:3]],
            [pending_high.id, medium.id, low.id],
        )

    def test_invalid_inputs_do_not_crash_or_add_tasks(self):
        self.assertIsNone(self.todo.add("   "))
        self.assertIsNone(self.todo.add("无效优先级", "urgent"))
        self.assertEqual(self.todo.list_tasks(), [])
        self.assertIn("标题不能为空", self.output.getvalue())
        self.assertIn("非法优先级", self.output.getvalue())
        self.assertIn("暂无任务", self.output.getvalue())

    def test_main_demonstration_runs_successfully(self):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        result = subprocess.run(
            [sys.executable, CANDIDATE], capture_output=True, timeout=10,
            encoding="utf-8", errors="replace", env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("标题不能为空", result.stdout)
        self.assertIn("非法优先级", result.stdout)
        self.assertIn("任务列表", result.stdout)
        self.assertTrue("[x]" in result.stdout or "[✓]" in result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
