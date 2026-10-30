"""Smoke tests for the external, hidden-test evaluation harness."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from evals.run_eval import summarize
from evals.verify import verify_code

ROOT = Path(__file__).resolve().parents[1]


class TestExternalVerification(unittest.TestCase):
    def test_existing_todo_artifact_passes_hidden_contract(self):
        code = (ROOT / "output" / "final_code.py").read_text(encoding="utf-8")
        result = verify_code(code, str(ROOT / "evals" / "tasks" / "todo" / "hidden_tests.py"))
        self.assertTrue(result["verified"], result["run_result"]["output"])

    def test_hidden_tests_reject_wrong_calculator(self):
        wrong = (
            "def divide(a, b): return a / b\n"
            "def average(numbers): return sum(numbers) / len(numbers)\n"
            "def clamp(value, low, high): return value\n"
        )
        result = verify_code(
            wrong, str(ROOT / "evals" / "tasks" / "calculator" / "hidden_tests.py")
        )
        self.assertFalse(result["verified"])

    def test_summary_exposes_false_success(self):
        records = [{
            "reported_success": True,
            "verification": {"verified": False},
            "duration_ms": 100,
        }]
        summary = summarize("parallel", records, {"total_tokens": 20})
        self.assertEqual(summary["verified_success_rate"], 0)
        self.assertEqual(summary["false_success_rate"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
