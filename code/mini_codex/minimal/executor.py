"""Run one Python program. A subprocess is not an operating-system sandbox."""

import json
from pathlib import Path
import subprocess
import sys


class Executor:
    def run_python(self, workspace: Path, path: str, payload: dict,
                   timeout: float = 2) -> dict:
        workspace = Path(workspace).resolve()
        relative = Path(path)
        target = (workspace / relative).resolve()
        if relative.is_absolute() or not target.is_relative_to(workspace):
            raise ValueError("Python 文件必须位于工作目录内，且使用相对路径")
        try:
            completed = subprocess.run(
                [sys.executable, "-I", str(target)],
                input=json.dumps(payload, ensure_ascii=False),
                cwd=workspace, env={}, capture_output=True,
                text=True, encoding="utf-8", timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            # TimeoutExpired can carry bytes even when text=True.
            stdout = error.stdout or b""
            stderr = error.stderr or b""
            return {
                "exit_code": None,
                "stdout": stdout.decode("utf-8", errors="replace")
                if isinstance(stdout, bytes) else stdout,
                "stderr": stderr.decode("utf-8", errors="replace")
                if isinstance(stderr, bytes) else stderr,
                "error": f"Python 执行超过 {timeout} 秒",
                "timed_out": True,
            }
        return {"exit_code": completed.returncode,
                "stdout": completed.stdout, "stderr": completed.stderr}
