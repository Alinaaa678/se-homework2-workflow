"""Time-boxed Python subprocess tool, reused from Homework 1.

The ReviewAgent uses it to execute candidate code before judging it. This is
not a security sandbox; its purpose is reproducible evidence collection.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from typing import Dict


def _scrub_sensitive_env(env: Dict[str, str]) -> Dict[str, str]:
    """Remove credentials before executing model-generated code."""
    markers = ("API_KEY", "SECRET", "PASSWORD", "TOKEN", "CREDENTIAL")
    return {
        key: value for key, value in env.items()
        if not any(marker in key.upper() for marker in markers)
    }


def run_python(code: str, timeout: int = 10) -> str:
    """Execute *code* in a temporary subprocess; return stdout+stderr.

    This is process isolation with a timeout, not a security sandbox: the
    candidate can still access the filesystem and network. Sensitive
    environment variables are removed, and output is truncated to 4000 chars.
    """
    with tempfile.TemporaryDirectory(prefix="wf_run_") as tmp:
        script = os.path.join(tmp, "candidate.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(code)
        # Force UTF-8 on both ends: Chinese Windows defaults to GBK and
        # would otherwise crash the reader thread on non-GBK bytes.
        env = _scrub_sensitive_env(dict(os.environ))
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            proc = subprocess.run(
                [sys.executable, script],
                capture_output=True,
                timeout=timeout,
                cwd=tmp,
                env=env,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            return f"[TIMEOUT] 执行超过 {timeout}s，已终止。"
        except Exception as e:  # pragma: no cover - defensive
            return f"[ERROR] 无法启动子进程: {e}"

    out = (proc.stdout or "") + (proc.stderr or "")
    if len(out) > 4000:
        out = out[:4000] + "\n...[输出已截断]"
    header = f"[exit_code={proc.returncode}]"
    return f"{header}\n{out.strip() or '(无输出)'}"


def static_check(code: str) -> Dict[str, object]:
    """Compile candidate source without executing it."""
    try:
        compile(code, "candidate.py", "exec")
    except (SyntaxError, ValueError, TypeError) as exc:
        return {"passed": False, "error": str(exc)}
    return {"passed": True, "error": None}


def run_acceptance(code: str, test_code: str, timeout: int = 15) -> Dict[str, object]:
    """Run independent unittest code against candidate.py in a fresh process.

    The returned verdict is derived only from the test process exit code. The
    LLM may propose tests, but it cannot directly mark those tests as passed.
    This remains time-boxed process isolation, not a security sandbox.
    """
    with tempfile.TemporaryDirectory(prefix="wf_accept_") as tmp:
        candidate = os.path.join(tmp, "candidate.py")
        tests = os.path.join(tmp, "test_candidate.py")
        with open(candidate, "w", encoding="utf-8") as handle:
            handle.write(code)
        with open(tests, "w", encoding="utf-8") as handle:
            handle.write(test_code)
        env = _scrub_sensitive_env(dict(os.environ))
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "unittest", "-v", "test_candidate"],
                capture_output=True,
                timeout=timeout,
                cwd=tmp,
                env=env,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            return {
                "passed": False,
                "exit_code": None,
                "output": f"[TIMEOUT] 验收测试超过 {timeout}s，已终止。",
            }
        except Exception as exc:  # pragma: no cover - defensive
            return {"passed": False, "exit_code": None, "output": f"[ERROR] {exc}"}

    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if len(output) > 6000:
        output = output[:6000] + "\n...[输出已截断]"
    return {
        "passed": proc.returncode == 0,
        "exit_code": proc.returncode,
        "output": output or "(无输出)",
    }
