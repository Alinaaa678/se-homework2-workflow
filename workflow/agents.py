"""The three workflow agents: coder -> reviewer -> fixer.

Each agent is a thin, prompt-driven wrapper around one LLM call that
returns a *structured* artifact (code string / review dict). Agents do
not talk to each other directly — they read and write the shared
WorkflowState blackboard via the engine, which records every hand-off
as a message.

Agent outputs are parsed defensively (fenced-code extraction, JSON
repair) so a slightly off-spec LLM answer degrades gracefully instead
of crashing the workflow.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from .llm import ChatClient
from .tools import run_python


# ---------------------------------------------------------------------- #
# output parsing helpers
# ---------------------------------------------------------------------- #
def extract_code(text: str) -> str:
    """Pull the first fenced code block out of an LLM reply.

    Falls back to the raw text when no fence is present.
    """
    match = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.DOTALL)
    return match.group(1).strip() if match else text.strip()


def extract_json(text: str) -> Dict[str, Any]:
    """Parse the first JSON object in an LLM reply.

    Tolerates markdown fences and leading/trailing prose. Raises
    ValueError when no parseable object is found.
    """
    cleaned = re.sub(r"```(?:json)?\s*", "", text).replace("```", "")
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"回复中未找到 JSON 对象: {text[:200]}")
    return json.loads(cleaned[start : end + 1])


VALID_SEVERITIES = {"high", "medium", "low"}
BLOCKING_SEVERITIES = {"high", "medium"}


def normalize_review(review: Dict[str, Any]) -> Dict[str, Any]:
    """Validate an LLM review and derive its verdict from blocking issues.

    The model-reported ``passed`` flag is checked for type but is not trusted
    as the final decision. This keeps contradictory replies such as a medium
    issue with ``passed=true`` from incorrectly ending the workflow.
    """
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
        issue["severity"] in BLOCKING_SEVERITIES
        for issue in normalized_issues
    )
    return {
        # A blocking issue always forces failure; a model-reported failure
        # without an itemized issue remains a conservative failure as well.
        "passed": review["passed"] and not has_blocking_issue,
        "issues": normalized_issues,
        "summary": review["summary"],
    }


# ---------------------------------------------------------------------- #
# agents
# ---------------------------------------------------------------------- #
class CoderAgent:
    """Turns a natural-language requirement into runnable Python code."""

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
        resp = self.client.chat(
            [
                {"role": "system", "content": self.SYSTEM},
                {"role": "user", "content": f"需求：\n{requirement}"},
            ]
        )
        return extract_code(resp["choices"][0]["message"]["content"] or "")


class CorrectnessReviewAgent:
    """Reviews code correctness: actually executes it, then asks the LLM.

    Output schema::

        {"passed": bool,
         "issues": [{"severity": "high|medium|low", "desc": str}],
         "summary": str}
    """

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
        # Execution supplies evidence; the original requirement supplies the oracle.
        run_result = run_python(code, timeout=self.exec_timeout)
        resp = self.client.chat(
            [
                {"role": "system", "content": self.SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"原始需求：\n{requirement}\n\n"
                        f"待审查代码：\n```python\n{code}\n```\n\n"
                        f"实际运行结果：\n{run_result}"
                    ),
                },
            ]
        )
        review = normalize_review(
            extract_json(resp["choices"][0]["message"]["content"] or "")
        )
        review["run_result"] = run_result[:500]
        return review


class StyleReviewAgent:
    """Reviews code style only (naming, structure, readability) — no execution.

    Runs in parallel with CorrectnessReviewAgent; same output schema so the
    merge_reviews reducer can combine both verdicts.
    """

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
        resp = self.client.chat(
            [
                {"role": "system", "content": self.SYSTEM},
                {"role": "user", "content": f"待审查代码：\n```python\n{code}\n```"},
            ]
        )
        return normalize_review(
            extract_json(resp["choices"][0]["message"]["content"] or "")
        )


# Backward-compatible alias: the original sequential flow uses "reviewer".
ReviewAgent = CorrectnessReviewAgent


def merge_reviews(outputs: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Validate and merge reviews; any high/medium issue blocks success."""
    normalized = {
        source: normalize_review(review)
        for source, review in sorted(outputs.items())
    }
    issues: List[Dict[str, Any]] = []
    for source, review in normalized.items():
        for issue in review["issues"]:
            issues.append({**issue, "source": source})
    merged: Dict[str, Any] = {
        "passed": all(review["passed"] for review in normalized.values()),
        "issues": issues,
        "summary": "；".join(
            f"[{source}] {review['summary']}"
            for source, review in normalized.items()
        ),
    }
    # Preserve execution evidence, which normalize_review intentionally omits.
    for source, review in sorted(outputs.items()):
        if review.get("run_result"):
            merged["run_result"] = review["run_result"]
            break
    return merged


class FixAgent:
    """Repairs code given the reviewer's issue list."""

    name = "fixer"
    SYSTEM = (
        "你是一名资深 Python 工程师。根据审查意见修复代码。"
        "逐一解决所有 high/medium 级问题，保持原有功能不变。"
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
        resp = self.client.chat(
            [
                {"role": "system", "content": self.SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"原始需求（修复后仍须完整满足）：\n{requirement}\n\n"
                        f"原始代码：\n```python\n{code}\n```\n\n"
                        f"审查意见：\n{issue_text}"
                    ),
                },
            ]
        )
        return extract_code(resp["choices"][0]["message"]["content"] or "")


# Registry: flow definitions refer to agents by these names.
def default_registry(client: ChatClient) -> Dict[str, Any]:
    correctness = CorrectnessReviewAgent(client)
    return {
        "coder": CoderAgent(client),
        "reviewer": correctness,  # alias，兼容顺序版流程
        "reviewer_correctness": correctness,
        "reviewer_style": StyleReviewAgent(client),
        "fixer": FixAgent(client),
    }


# Reducer registry: parallel nodes refer to reducers by these names.
REDUCERS = {
    "merge_reviews": merge_reviews,
}
