"""LLM agents and deterministic gates used by the workflow.

LLM nodes propose code, reviews or tests. Deterministic nodes build the spec,
execute acceptance tests and derive the final verdict from current evidence.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from .artifacts import content_hash
from .llm import ChatClient
from .spec import SpecBuilder
from .tools import run_acceptance, run_python, static_check


# ---------------------------------------------------------------------- #
# Output parsing helpers
# ---------------------------------------------------------------------- #
def extract_code(text: str) -> str:
    match = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.DOTALL)
    return match.group(1).strip() if match else text.strip()


def extract_json(text: str) -> Dict[str, Any]:
    cleaned = re.sub(r"```(?:json)?\s*", "", text).replace("```", "")
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"回复中未找到 JSON 对象: {text[:200]}")
    return json.loads(cleaned[start : end + 1])


VALID_SEVERITIES = {"high", "medium", "low"}
BLOCKING_SEVERITIES = {"high", "medium"}


def normalize_review(review: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a review and conservatively derive its local verdict."""
    if not isinstance(review, dict):
        raise ValueError("审查结果必须是 JSON 对象")
    if "passed" not in review or type(review["passed"]) is not bool:
        raise ValueError("审查结果缺少布尔字段 passed")
    if not isinstance(review.get("issues"), list):
        raise ValueError("审查结果缺少数组字段 issues")
    if not isinstance(review.get("summary"), str):
        raise ValueError("审查结果缺少字符串字段 summary")

    normalized_issues: List[Dict[str, str]] = []
    for issue in review["issues"]:
        if not isinstance(issue, dict):
            raise ValueError("审查问题必须是 JSON 对象")
        severity = issue.get("severity")
        desc = issue.get("desc")
        if severity not in VALID_SEVERITIES:
            raise ValueError(f"非法问题级别: {severity!r}")
        if not isinstance(desc, str) or not desc.strip():
            raise ValueError("审查问题缺少非空描述 desc")
        normalized_issues.append({"severity": severity, "desc": desc.strip()})

    has_blocking_issue = any(
        issue["severity"] in BLOCKING_SEVERITIES for issue in normalized_issues
    )
    return {
        "passed": review["passed"] and not has_blocking_issue,
        "issues": normalized_issues,
        "summary": review["summary"],
    }


# ---------------------------------------------------------------------- #
# LLM-backed nodes
# ---------------------------------------------------------------------- #
class CoderAgent:
    name = "coder"
    SYSTEM = (
        "你是一名资深 Python 工程师。根据用户需求写出完整、可直接运行的 "
        "Python 代码。只输出一个 ```python 代码块，不要输出解释。"
        "代码需包含必要的输入校验与示例用法（放在 if __name__ == '__main__': 中）。"
    )

    def __init__(self, client: ChatClient) -> None:
        self.client = client

    def run(self, inputs: Dict[str, Any]) -> str:
        requirement = inputs["requirement"]
        resp = self.client.chat([
            {"role": "system", "content": self.SYSTEM},
            {"role": "user", "content": f"需求：\n{requirement}"},
        ])
        return extract_code(resp["choices"][0]["message"]["content"] or "")


class CorrectnessReviewAgent:
    name = "reviewer_correctness"
    SYSTEM = (
        "你是一名严格的代码正确性审查员。你会拿到原始需求、代码和实际运行结果。"
        "逐项对照原始需求，检查功能完整性、逻辑错误、边界条件和异常处理；"
        "不能仅凭程序没有崩溃就判定满足需求。"
        "只输出一个 JSON 对象，格式："
        '{"passed": true/false, "issues": [{"severity": "high|medium|low", '
        '"desc": "问题描述及对应需求"}], "summary": "一句话总结"}。'
        "存在 high/medium 级问题时 passed 必须为 false；"
        "没有问题时 issues 为空数组且 passed 为 true。不要输出 JSON 以外的任何内容。"
    )

    def __init__(self, client: ChatClient, exec_timeout: int = 10) -> None:
        self.client = client
        self.exec_timeout = exec_timeout

    def run(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        code = inputs["code"]
        requirement = inputs["requirement"]
        run_result = run_python(code, timeout=self.exec_timeout)
        resp = self.client.chat([
            {"role": "system", "content": self.SYSTEM},
            {"role": "user", "content": (
                f"原始需求：\n{requirement}\n\n"
                f"待审查代码：\n```python\n{code}\n```\n\n"
                f"实际运行结果：\n{run_result}"
            )},
        ])
        review = normalize_review(
            extract_json(resp["choices"][0]["message"]["content"] or "")
        )
        review["run_result"] = run_result[:500]
        review["target_code_hash"] = content_hash(code)
        return review


class StyleReviewAgent:
    name = "reviewer_style"
    SYSTEM = (
        "你是一名严格的代码风格审查员。只关注可读性与工程规范：命名、函数长度、"
        "重复代码、注释与文档字符串、Magic Number、接口设计一致性。"
        "不要评价功能正确性（另有专人负责）。"
        "只输出一个 JSON 对象，格式："
        '{"passed": true/false, "issues": [{"severity": "high|medium|low", '
        '"desc": "问题描述"}], "summary": "一句话总结"}。'
        "只有 high/medium 级风格问题才判 false；小问题记入 issues 但 passed 可为 true。"
        "不要输出 JSON 以外的任何内容。"
    )

    def __init__(self, client: ChatClient) -> None:
        self.client = client

    def run(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        code = inputs["code"]
        resp = self.client.chat([
            {"role": "system", "content": self.SYSTEM},
            {"role": "user", "content": f"待审查代码：\n```python\n{code}\n```"},
        ])
        review = normalize_review(
            extract_json(resp["choices"][0]["message"]["content"] or "")
        )
        review["target_code_hash"] = content_hash(code)
        return review


# Backward-compatible alias for the sequential flow.
ReviewAgent = CorrectnessReviewAgent


def merge_reviews(outputs: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Validate and merge reviews; any high/medium issue blocks success."""
    normalized = {
        source: normalize_review(review) for source, review in sorted(outputs.items())
    }
    issues: List[Dict[str, Any]] = []
    for source, review in normalized.items():
        for issue in review["issues"]:
            issues.append({**issue, "source": source})
    merged: Dict[str, Any] = {
        "passed": all(review["passed"] for review in normalized.values()),
        "issues": issues,
        "summary": "；".join(
            f"[{source}] {review['summary']}" for source, review in normalized.items()
        ),
    }
    for source, review in sorted(outputs.items()):
        if review.get("run_result") and "run_result" not in merged:
            merged["run_result"] = review["run_result"]
    hashes = {
        review.get("target_code_hash")
        for review in outputs.values()
        if review.get("target_code_hash")
    }
    hashed_reviews = sum(
        bool(review.get("target_code_hash")) for review in outputs.values()
    )
    if len(hashes) == 1 and hashed_reviews == len(outputs):
        merged["target_code_hash"] = hashes.pop()
    return merged


class AcceptanceTestAgent:
    """Generate tests from requirements, then let a process derive the result."""

    name = "acceptance"
    SYSTEM = (
        "你是一名独立测试工程师。根据原始需求和候选代码接口编写 Python unittest。"
        "测试必须覆盖每项编号需求、重要边界条件和非法输入；不要只测试程序能否启动。"
        "使用 from candidate import ... 导入被测对象，不要修改 candidate.py。"
        "只输出一个完整 ```python 测试代码块，不要输出解释。"
    )

    def __init__(self, client: ChatClient, exec_timeout: int = 15) -> None:
        self.client = client
        self.exec_timeout = exec_timeout

    def run(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        code = inputs["code"]
        requirement = inputs["requirement"]
        spec = inputs["spec"]
        resp = self.client.chat([
            {"role": "system", "content": self.SYSTEM},
            {"role": "user", "content": (
                f"原始需求与验收范围：\n{requirement}\n\n"
                f"候选代码（仅用于确定接口）：\n```python\n{code}\n```"
            )},
        ])
        test_code = extract_code(resp["choices"][0]["message"]["content"] or "")
        static = static_check(code)
        if static["passed"]:
            execution = run_acceptance(code, test_code, timeout=self.exec_timeout)
        else:
            execution = {
                "passed": False,
                "exit_code": None,
                "output": f"静态检查失败：{static['error']}",
            }
        return {
            "passed": bool(static["passed"] and execution["passed"]),
            "target_code_hash": content_hash(code),
            "spec_hash": spec["spec_hash"],
            "requirement_ids": [item["id"] for item in spec["requirements"]],
            "static_check": static,
            "test_code": test_code,
            "run_result": execution,
        }


class FixAgent:
    name = "fixer"
    SYSTEM = (
        "你是一名资深 Python 工程师。根据审查意见和失败验收修复代码。"
        "逐一解决所有 high/medium 级问题与失败测试，保持原有功能不变。"
        "只输出一个 ```python 代码块（修复后的完整代码），不要输出解释。"
    )

    def __init__(self, client: ChatClient) -> None:
        self.client = client

    def run(self, inputs: Dict[str, Any]) -> str:
        code = inputs["code"]
        requirement = inputs["requirement"]
        issues: List[Dict[str, Any]] = inputs["review"].get("issues", [])
        issue_text = "\n".join(
            f"- [{i.get('severity', '?')}] {i.get('desc', '')}" for i in issues
        ) or "(无具体问题描述)"
        acceptance = inputs.get("acceptance") or {}
        acceptance_result = acceptance.get("run_result") or {}
        failure_text = acceptance_result.get("output") or "(无验收输出)"
        resp = self.client.chat([
            {"role": "system", "content": self.SYSTEM},
            {"role": "user", "content": (
                f"原始需求（修复后仍须完整满足）：\n{requirement}\n\n"
                f"原始代码：\n```python\n{code}\n```\n\n"
                f"审查意见：\n{issue_text}\n\n"
                f"独立验收输出：\n{failure_text}"
            )},
        ])
        return extract_code(resp["choices"][0]["message"]["content"] or "")


# ---------------------------------------------------------------------- #
# Deterministic final gate
# ---------------------------------------------------------------------- #
class VerdictGate:
    name = "verdict"

    def run(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        code = inputs["code"]
        review = inputs["review"]
        acceptance = inputs["acceptance"]
        spec = inputs["spec"]
        current_hash = content_hash(code)
        checks = {
            "review_passed": review.get("passed") is True,
            "acceptance_passed": acceptance.get("passed") is True,
            "review_current": review.get("target_code_hash") == current_hash,
            "acceptance_current": acceptance.get("target_code_hash") == current_hash,
            "spec_current": acceptance.get("spec_hash") == spec.get("spec_hash"),
        }
        passed = all(checks.values())
        reasons = [name for name, ok in checks.items() if not ok]
        return {
            "passed": passed,
            "status": "completed_verified" if passed else "needs_attention",
            "target_code_hash": current_hash,
            "spec_hash": spec.get("spec_hash"),
            "checks": checks,
            "reasons": reasons,
        }


def default_registry(client: ChatClient) -> Dict[str, Any]:
    correctness = CorrectnessReviewAgent(client)
    return {
        "spec": SpecBuilder(),
        "coder": CoderAgent(client),
        "reviewer": correctness,
        "reviewer_correctness": correctness,
        "reviewer_style": StyleReviewAgent(client),
        "acceptance": AcceptanceTestAgent(client),
        "verdict": VerdictGate(),
        "fixer": FixAgent(client),
    }


REDUCERS = {"merge_reviews": merge_reviews}
