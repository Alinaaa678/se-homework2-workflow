"""Sandboxed Python execution tool, reused from Homework 1.

The ReviewAgent uses it to actually *run* the candidate code before
judging it — the same "verify by executing" idea as Homework 1's
``run_python`` tool, kept dependency-free and time-boxed.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import os


def run_python(code: str, timeout: int = 10) -> str:
    """Execute *code* in an isolated subprocess; return stdout+stderr.

    The code runs in a temp directory with a hard timeout. Output is
    truncated to 4000 chars so a runaway print loop can't flood the
    agent context.
    """
    with tempfile.TemporaryDirectory(prefix="wf_run_") as tmp:
        script = os.path.join(tmp, "candidate.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(code)
        # Force UTF-8 on both ends: Chinese Windows defaults to GBK and
        # would otherwise crash the reader thread on non-GBK bytes.
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
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
