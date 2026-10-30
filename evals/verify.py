"""Independent artifact verification using tests hidden from the workflow."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from workflow.artifacts import content_hash
from workflow.tools import run_acceptance


def verify_code(code: str, hidden_test_path: str, timeout: int = 20) -> Dict[str, Any]:
    test_code = Path(hidden_test_path).read_text(encoding="utf-8")
    result = run_acceptance(code, test_code, timeout=timeout)
    return {
        "verified": result["passed"],
        "target_code_hash": content_hash(code),
        "run_result": result,
        "hidden_test_path": str(hidden_test_path),
    }
