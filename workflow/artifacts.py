"""Content-addressed artifact helpers.

Artifacts remain ordinary values on the blackboard for compatibility. Their
version and content hash live in ``WorkflowState.artifacts`` and in run logs.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_bytes(value: Any) -> bytes:
    if isinstance(value, str):
        return value.encode("utf-8")
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def content_hash(value: Any) -> str:
    """Return a stable hash for source code, specs and structured evidence."""
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()
