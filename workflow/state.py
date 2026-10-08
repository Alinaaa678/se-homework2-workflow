"""Shared workflow state: a blackboard plus an agent message log.

Two kinds of information flow between agents:

1. **Blackboard** (``state.data``): named artifacts produced by nodes
   (e.g. ``code``, ``review``). Nodes declare which keys they read and
   which key they write, so data lineage is explicit.
2. **Messages** (``state.messages``): every hand-off between two nodes is
   recorded as a message envelope, giving a complete audit trail of
   agent-to-agent communication.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional


class WorkflowState:
    """Mutable state shared by all nodes of one workflow run."""

    def __init__(self, initial: Optional[Dict[str, Any]] = None) -> None:
        self.data: Dict[str, Any] = dict(initial or {})
        self.messages: List[Dict[str, Any]] = []
        self.status: str = "pending"  # pending | running | success | failed
        self.error: Optional[str] = None

    # ------------------------------------------------------------------ #
    # blackboard
    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    # ------------------------------------------------------------------ #
    # message passing
    def post_message(
        self, sender: str, receiver: str, kind: str, payload: Any
    ) -> Dict[str, Any]:
        """Record one agent-to-agent message; returns the envelope."""
        envelope = {
            "ts": round(time.time(), 3),
            "from": sender,
            "to": receiver,
            "kind": kind,
            "payload": payload,
        }
        self.messages.append(envelope)
        return envelope

    # ------------------------------------------------------------------ #
    def summary(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "error": self.error,
            "keys": sorted(self.data.keys()),
            "message_count": len(self.messages),
        }
