"""Small, inspectable helpers for the executable textbook.

Lesson mechanisms live in _examples or the real checkpoint, not in this helper.
No credential file is read unless the reader explicitly requests a live run.
"""
from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]


def fresh_run_dir(label: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", label):
        raise ValueError("Use a short filename-safe experiment label")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = ROOT / "runs" / "notebooks" / f"{label}-{stamp}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def load_example(chapter: str, module: str):
    """Import the very same implementation module used by the final package."""
    from .components import load_component
    return load_component(chapter, module)


def show_source(obj):
    from IPython.display import Code, display

    source = inspect.getsource(obj)
    display(Code(source, language="python"))
    return None


def run_python(script: Path, args=(), cwd=None, *, timeout=120, check=True):
    """Run a known lesson script, retaining its actual output and exit status."""
    script = Path(script).resolve(strict=True)
    command = [sys.executable, str(script), *(str(arg) for arg in args)]
    run_dir = fresh_run_dir("python")
    with (run_dir / "stdout.log").open("w") as out, (run_dir / "stderr.log").open("w") as err:
        process = subprocess.run(command, cwd=cwd or script.parent, stdout=out, stderr=err,
                                 text=True, timeout=timeout)
    result = {"returncode": process.returncode,
              "stdout": (run_dir / "stdout.log").read_text(),
              "stderr": (run_dir / "stderr.log").read_text(), "run_dir": str(run_dir)}
    (run_dir / "process.json").write_text(json.dumps({"command": command,
        "returncode": process.returncode}, ensure_ascii=False, indent=2) + "\n")
    if check and process.returncode:
        raise RuntimeError(f"Lesson script exited {process.returncode}; logs: {run_dir}\n"
                           + result["stderr"][-2000:])
    return result


def run_chapter(chapter: str, action: str, env_file=None):
    """Run the real Docker checkpoint. Missing Docker is a visible blocked result.

    Live requests require an explicit credential-file argument; they are never
    silently substituted by replay. Values from that file are handled by the
    checkpoint runner, not loaded into the notebook kernel.
    """
    chapter = str(chapter).zfill(2)
    if not re.fullmatch(r"\d{2}", chapter) or action not in ("doctor", "replay", "test", "live"):
        raise ValueError("Unsupported checkpoint operation")
    if action == "live" and env_file is None:
        raise ValueError("For live, explicitly select your private env_file outside code/ and the workspace")
    if not (ROOT / "code/mini_codex/__main__.py").is_file():
        return {"status": "blocked", "reason": "checkpoint_not_integrated", "chapter": chapter}
    if not shutil.which("docker"):
        return {"status": "blocked", "reason": "docker_cli_missing", "chapter": chapter}
    try:
        ready = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                               capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return {"status": "blocked", "reason": "docker_engine_timeout", "chapter": chapter}
    if ready.returncode:
        return {"status": "blocked", "reason": "docker_engine_unavailable", "chapter": chapter}
    command = [sys.executable, str(ROOT / "code/run.py"), action]
    if env_file is not None:
        command += ["--env-file", str(Path(env_file).expanduser().resolve())]
    run_dir = fresh_run_dir(f"checkpoint-{chapter}-{action}")
    print(f"Running checkpoint {chapter}/{action}; logs: {run_dir}", flush=True)
    with (run_dir / "stdout.log").open("w") as out, (run_dir / "stderr.log").open("w") as err:
        process = subprocess.run(command, cwd=ROOT, stdout=out, stderr=err, text=True, timeout=960)
    stdout = (run_dir / "stdout.log").read_text()
    try:
        summary = json.loads(stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise RuntimeError(f"Runner produced no result; logs: {run_dir}") from exc
    evidence = Path(summary["evidence"]) if summary.get("evidence") else None
    record = json.loads((evidence / "runner.json").read_text()) if evidence else None
    result = {**summary, "returncode": process.returncode, "record": record,
              "artifacts": evidence / "artifacts" if evidence else None,
              "notebook_logs": run_dir}
    if process.returncode not in (0, 2):
        raise RuntimeError(f"Actual checkpoint failed/unknown (exit {process.returncode}); "
                           f"evidence: {evidence}; logs: {run_dir}")
    if process.returncode == 2:
        result["status"] = "blocked"
    return result
