"""Bounded teaching probe: real Git worktrees + no-clobber owner publication.

No executor, sandbox, remote transport, Loop, or file restore is implemented here.
All Git mutations target a new disposable repository under the requested run dir.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading


def git(cwd: Path, *args: str, check: bool = True):
    # Exclude inherited repository redirection and user/global Git config.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0")
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args],
                          cwd=cwd, env=env, check=check, capture_output=True, text=True)


def owner_path(checkout: Path) -> Path:
    p = Path(git(checkout, "rev-parse", "--git-path", "codex-thread.json").stdout.strip())
    return p if p.is_absolute() else checkout / p


def owner(checkout: Path) -> str | None:
    try:
        record = json.loads(owner_path(checkout).read_bytes())
    except FileNotFoundError:
        return None
    if (set(record) != {"version", "ownerThreadId"} or record["version"] != 1
            or not isinstance(record["ownerThreadId"], str) or not record["ownerThreadId"]):
        raise ValueError("invalid owner record")
    return record["ownerThreadId"]


def bind(checkout: Path, thread_id: str) -> str:
    """Publish complete JSON with an atomic, same-filesystem no-clobber link.

    This POSIX probe illustrates publication, not a cross-platform public API.
    Caller is responsible for managed-layout/Git-backlink/authorization validation.
    """
    if not thread_id:
        raise ValueError("empty thread id")
    target = owner_path(checkout)
    fd, raw_temp = tempfile.mkstemp(prefix="owner-", dir=target.parent)
    temp = Path(raw_temp)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"version": 1, "ownerThreadId": thread_id}, f)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(temp, target)
        except FileExistsError:
            if owner(checkout) != thread_id:
                raise ValueError("different owner")
        return thread_id
    finally:
        temp.unlink()


def restore_plan(snapshot: bytes, expected_current: bytes, current: bytes) -> dict:
    """Read-only conflict decision; DOES NOT apply a restore or close its TOCTOU race."""
    digest = lambda b: sha256(b).hexdigest()
    if current != expected_current:
        return {"status": "conflict", "current": digest(current),
                "expected": digest(expected_current), "snapshot": digest(snapshot)}
    return {"status": "ready_for_commit_recheck", "current": digest(current),
            "snapshot": digest(snapshot)}


def run(run_dir: Path) -> dict:
    run_dir.mkdir(parents=True, exist_ok=False)
    repo = run_dir / "task-list"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "task_list.py").write_text("def remaining(tasks):\n    return [t for t in tasks if not t['done']]\n")
    (repo / "checks").mkdir()
    (repo / "checks" / "check.py").write_text("from pathlib import Path\nprint(Path.cwd().name)\n")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Teaching Probe", "-c", "user.email=probe@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-qm", "initial disposable fixture")
    revision = git(repo, "rev-parse", "HEAD").stdout.strip()
    worktrees = []
    for bucket in ("a1b2", "c3d4"):
        wt = run_dir / "managed" / bucket / "task-list"
        wt.parent.mkdir(parents=True)
        git(repo, "worktree", "add", "--detach", str(wt), revision)
        worktrees.append(wt)
    checks = []
    def record(name, passed, **facts):
        checks.append({"name": name, "passed": bool(passed), **facts})
        assert passed, name
    a, b = worktrees
    record("distinct_worktrees_same_commit", a != b and all(
        git(w, "rev-parse", "HEAD").stdout.strip() == revision for w in worktrees))
    record("created_before_binding_is_unowned", owner(a) is None and owner(b) is None)
    bind(a, "thread-a")
    before = owner_path(a).read_bytes()
    bind(a, "thread-a")
    record("same_owner_idempotent", owner_path(a).read_bytes() == before)
    try:
        bind(a, "thread-other")
    except ValueError:
        record("different_owner_rejected", owner(a) == "thread-a")
    else:
        record("different_owner_rejected", False)
    barrier = threading.Barrier(2)
    def contender(tid):
        barrier.wait()
        try:
            return {"thread": tid, "result": bind(b, tid)}
        except ValueError:
            return {"thread": tid, "result": "rejected"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(contender, ["thread-b", "thread-c"]))
    record("competing_owner_one_winner", sum(r["result"] != "rejected" for r in results) == 1,
           results=results, winner=owner(b))
    for wt, marker in [(a, "A"), (b, "B")]:
        # The real process resolves its cwd; this is two local worktrees, not two OS environments.
        p = subprocess.run(["/bin/sh", "-c", "printf '%s' \"$1\" > marker.txt", "probe", marker],
                           cwd=wt, capture_output=True, text=True, check=True)
        record("process_marker_" + marker, (wt / "marker.txt").read_text() == marker,
               exit_code=p.returncode)
    denied = git(repo, "worktree", "remove", str(a), check=False)
    record("git_refuses_dirty_removal", denied.returncode != 0 and (a / "marker.txt").exists(),
           expected_failure_exit_code=denied.returncode, stderr=denied.stderr.strip())
    target = a / "task_list.py"
    snapshot = target.read_bytes()
    target.write_bytes(snapshot + b"# concurrent user edit\n")
    actual = target.read_bytes()
    plan = restore_plan(snapshot, snapshot, actual)
    record("restore_conflict_plan_does_not_write", plan["status"] == "conflict"
           and target.read_bytes() == actual, plan=plan)
    record("restore_matching_plan_is_not_a_commit",
           restore_plan(snapshot, actual, actual)["status"] == "ready_for_commit_recheck")
    # Deliberately retain both dirty worktrees and the unmodified fixture source for inspection.
    record("source_unchanged", git(repo, "status", "--porcelain").stdout == "")
    result = {"kind": "teaching_data_mechanism_probe", "source_codex_executed": False,
              "remote_or_os_isolation_verified": False, "git_version": git(repo, "--version").stdout.strip(),
              "fixture_revision": revision, "checks": checks,
              "passed": all(c["passed"] for c in checks), "count": len(checks)}
    (run_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.run_dir.resolve())
    print(json.dumps({"passed": result["passed"], "checks": result["count"]}))
