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


class ReviewAgent:
    """Reviews code: actually executes it, then asks the LLM for a verdict.

    Output schema::

        {"passed": bool,
         "issues": [{"severity": "high|medium|low", "desc": str}],
         "summary": str}
    """

    name = "reviewer"
    SYSTEM = (
        "你是一名严格的代码审查员。你会拿到一段代码和它的实际运行结果。"
        "从正确性、健壮性（边界条件/异常处理）、可读性三方面审查。"
        "只输出一个 JSON 对象，格式："
        '{"passed": true/false, "issues": [{"severity": "high|medium|low", '
        '"desc": "问题描述"}], "summary": "一句话总结"}。'
        "存在 high 级问题时 passed 必须为 false；没有问题时 issues 为空数组且 passed 为 true。"
        "不要输出 JSON 以外的任何内容。"
    )

    def __init__(self, client: ChatClient, exec_timeout: int = 10) -> None:
        self.client = client
        self.exec_timeout = exec_timeout

    def run(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        code = inputs["code"]
        # Reuse Homework 1's "verify by executing" idea.
        run_result = run_python(code, timeout=self.exec_timeout)
        resp = self.client.chat(
            [
                {"role": "system", "content": self.SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"待审查代码：\n```python\n{code}\n```\n\n"
                        f"实际运行结果：\n{run_result}"
                    ),
                },
            ]
        )
        review = extract_json(resp["choices"][0]["message"]["content"] or "")
        # Normalise the schema so downstream nodes can rely on it.
        review.setdefault("passed", False)
        review.setdefault("issues", [])
        review.setdefault("summary", "")
        review["run_result"] = run_result[:500]
        return review


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
                        f"原始代码：\n```python\n{code}\n```\n\n"
                        f"审查意见：\n{issue_text}"
                    ),
                },
            ]
        )
        return extract_code(resp["choices"][0]["message"]["content"] or "")


# Registry: flow definitions refer to agents by these names.
def default_registry(client: ChatClient) -> Dict[str, Any]:
    return {
        "coder": CoderAgent(client),
        "reviewer": ReviewAgent(client),
        "fixer": FixAgent(client),
    }
