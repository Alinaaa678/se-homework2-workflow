"""Versioned requirement specifications with stable requirement identifiers."""
from __future__ import annotations

import re
from typing import Any, Dict, List

from .artifacts import content_hash


_NUMBERED = re.compile(r"^\s*(?:\d+[.、)]|[-*•])\s+(.+?)\s*$")


def build_spec(requirement: str) -> Dict[str, Any]:
    """Turn numbered requirements into a small traceable specification.

    This deterministic node does not claim to understand arbitrary natural
    language. It preserves the source text and assigns stable IDs to numbered
    or bulleted lines; unnumbered paragraphs are retained as individual items.
    """
    lines = [line for line in requirement.splitlines() if line.strip()]
    items: List[str] = []
    for line in lines:
        match = _NUMBERED.match(line)
        if match:
            items.append(match.group(1).strip())
    if not items:
        items = [line.strip() for line in lines]
    if not items:
        raise ValueError("需求不能为空")

    requirements = [
        {
            "id": f"R{index:03d}",
            "text": text,
            "acceptance": f"独立验收必须覆盖 {f'R{index:03d}'}：{text}",
        }
        for index, text in enumerate(items, 1)
    ]
    return {
        "schema_version": "spec/v1",
        "spec_hash": content_hash(requirement),
        "source": requirement,
        "requirements": requirements,
    }


class SpecBuilder:
    name = "spec"

    def run(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        return build_spec(inputs["requirement"])
