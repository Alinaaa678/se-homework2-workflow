"""Thread-safe, provider-neutral LLM and node run metrics."""
from __future__ import annotations

import threading
from typing import Any, Dict


class RunMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.llm_attempts = 0
        self.llm_calls = 0
        self.llm_retries = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.node_calls = 0
        self.node_duration_ms = 0

    def handle_event(self, event: Dict[str, Any]) -> None:
        kind = event.get("type")
        with self._lock:
            if kind == "llm_attempt":
                self.llm_attempts += 1
            elif kind == "llm_retry":
                self.llm_retries += 1
            elif kind == "llm_response":
                self.llm_calls += 1
                usage = event.get("usage") or {}
                self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
                self.completion_tokens += int(usage.get("completion_tokens") or 0)
                self.total_tokens += int(
                    usage.get("total_tokens")
                    or (usage.get("prompt_tokens") or 0)
                    + (usage.get("completion_tokens") or 0)
                )

    def record_node(self, step: Dict[str, Any]) -> None:
        with self._lock:
            self.node_calls += 1
            self.node_duration_ms += int(step.get("duration_ms") or 0)

    def summary(self) -> Dict[str, int]:
        with self._lock:
            return {
                "llm_attempts": self.llm_attempts,
                "llm_calls": self.llm_calls,
                "llm_retries": self.llm_retries,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
                "node_calls": self.node_calls,
                "node_duration_ms": self.node_duration_ms,
            }


def merge_metrics(*snapshots: Dict[str, Any]) -> Dict[str, int]:
    keys = (
        "llm_attempts", "llm_calls", "llm_retries", "prompt_tokens",
        "completion_tokens", "total_tokens", "node_calls", "node_duration_ms",
    )
    return {
        key: sum(int(snapshot.get(key) or 0) for snapshot in snapshots)
        for key in keys
    }
