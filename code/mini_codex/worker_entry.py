"""Trusted launcher, mounted read-only. Run with Python -I, never import workspace code.

This process is already inside bubblewrap. Only `candidate` execs candidate Python.
"""
import json
import os
from pathlib import Path
import resource
import sys
import tempfile

ROOT = Path("/workspace")


def workspace_path(name):
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise ValueError("path must be relative to /workspace")
    path = (ROOT / name).resolve()
    if not path.is_relative_to(ROOT) or path == ROOT:
        raise ValueError("outside_workspace")
    return path


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    packet = json.loads(sys.stdin.readline())
    operation, args = packet["operation"], packet["arguments"]
    if operation == "exec":
        os.execv("/bin/sh", ["/bin/sh", "-c", args["command"]])
    if operation == "candidate":
        # Candidate stdin is separate from the trusted launcher packet.
        # exec replaces this process; the host comparator never imports the candidate.
        fd = os.memfd_create("candidate-input", 0)
        os.write(fd, json.dumps(args["input"]).encode())
        os.lseek(fd, 0, os.SEEK_SET)
        os.dup2(fd, 0)
        os.close(fd)
        os.execv(sys.executable, [sys.executable, "-I", "/workspace/task_list.py"])
    if operation == "read":
        result = {"text": workspace_path(args["path"]).read_text()[:24000]}
    elif operation == "search":
        result = {"matches": []}
        for path in sorted(ROOT.rglob("*.py")):
            if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
                continue
            for number, line in enumerate(path.read_text()[:100000].splitlines(), 1):
                if args["query"] in line:
                    result["matches"].append({"path": str(path.relative_to(ROOT)),
                                              "line": number, "text": line[:400]})
                if len(result["matches"]) >= 100:
                    break
            if len(result["matches"]) >= 100:
                break
    elif operation == "apply_patch":
        path = workspace_path(args["path"])
        before = path.read_text()
        if not args["old"] or before.count(args["old"]) != 1:
            raise ValueError("patch requires exactly one old-text match")
        after = before.replace(args["old"], args["new"], 1)
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as tmp:
            tmp.write(after)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp.name, path)
        result = {"path": args["path"], "replacements": 1}
    else:
        raise ValueError("unknown operation")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"error": type(exc).__name__, "detail": str(exc)[:500]}))
        sys.exit(2)
