"""Shared workflow state: blackboard, messages and versioned artifacts."""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .artifacts import content_hash


class WorkflowState:
    """Mutable state shared by all nodes of one workflow run.

    Blackboard values stay unwrapped for node compatibility. ``artifacts``
    stores each value's producer, version and content hash so evidence can be
    tied to the exact source version it examined.
    """

    def __init__(self, initial: Optional[Dict[str, Any]] = None) -> None:
        self.data: Dict[str, Any] = dict(initial or {})
        self.messages: List[Dict[str, Any]] = []
        self.artifacts: Dict[str, Dict[str, Any]] = {}
        self.status: str = "pending"  # pending | running | success | failed
        self.error: Optional[str] = None

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    def track_artifact(
        self, key: str, value: Any, producer: str, visit: int
    ) -> Dict[str, Any]:
        previous = self.artifacts.get(key, {})
        metadata = {
            "version": int(previous.get("version") or 0) + 1,
            "hash": content_hash(value),
            "producer": producer,
            "visit": visit,
            "ts": time.time(),
        }
        self.artifacts[key] = metadata
        return metadata

    def post_message(
        self, sender: str, receiver: str, kind: str, payload: Any
    ) -> Dict[str, Any]:
        envelope = {
            "ts": round(time.time(), 3),
            "from": sender,
            "to": receiver,
            "kind": kind,
            "payload": payload,
        }
        self.messages.append(envelope)
        return envelope

    def to_dict(self) -> Dict[str, Any]:
        return {
            "data": self.data,
            "messages": self.messages,
            "artifacts": self.artifacts,
            "status": self.status,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "WorkflowState":
        state = cls(payload.get("data") or {})
        state.messages = list(payload.get("messages") or [])
        state.artifacts = dict(payload.get("artifacts") or {})
        state.status = payload.get("status") or "pending"
        state.error = payload.get("error")
        return state

    def summary(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "error": self.error,
            "keys": sorted(self.data.keys()),
            "message_count": len(self.messages),
            "artifact_count": len(self.artifacts),
        }
