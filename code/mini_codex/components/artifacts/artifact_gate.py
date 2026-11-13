"""Teaching prototype: trusted local fixtures only, no network or model.

Implements artifact identity/baseline checks and real Git patch application.
Not an OS sandbox, service adapter, persistent journal, or complete file tool.
The single supported patch path is deliberately task_list.py.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


@dataclass(frozen=True)
class Artifact:
    task_id: str
    environment_id: str
    attempt_id: str
    result_revision: str
    base_commit: str
    patch_sha256: str
    patch: bytes


class Refused(Exception):
    pass


class Git:
    def __init__(self, log):
        self.log = log

    def run(self, cwd, *args, check=True):
        env = {"PATH": os.defpath,
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
               "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0"}
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
            cwd=cwd, env=env, capture_output=True, timeout=10,
        )
        self.log.append({"argv": ["git", *args], "cwd": str(cwd),
                         "exit_code": result.returncode,
                         "stdout": result.stdout.decode(errors="replace"),
                         "stderr": result.stderr.decode(errors="replace")})
        if check and result.returncode:
            raise Refused("git_failed: " + " ".join(args[:2]))
        return result

    def snapshot(self, repo):
        # Include tracked, untracked and index state, rather than HEAD alone.
        files = {}
        for path in sorted(Path(repo).rglob("*")):
            rel = path.relative_to(repo)
            if rel.parts[0] == ".git":
                continue
            if path.is_symlink():
                raise Refused("symlink_out_of_scope")
            if path.is_file():
                files[str(rel)] = {"sha256": digest(path.read_bytes()),
                                   "mode": path.stat().st_mode & 0o777}
        head = self.run(repo, "rev-parse", "HEAD").stdout.decode().strip()
        index = self.run(repo, "ls-files", "--stage", "-z").stdout
        return {"head": head, "files": files, "index_sha256": digest(index)}


class ArtifactGate:
    def __init__(self, git, repo, evidence_dir, expected_identity):
        self.git = git
        self.repo = Path(repo)
        self.evidence = Path(evidence_dir)
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.expected = expected_identity
        self.state_path = self.evidence / "application.json"

    def record(self, value):
        # Atomic file replacement for this local exercise, not a crash-durable Journal.
        path = self.evidence / "application.next.json"
        path.write_bytes(canonical(value) + b"\n")
        path.replace(self.state_path)

    def prepare(self, artifact):
        if artifact is None:
            raise Refused("artifact_missing")
        for key, wanted in self.expected.items():
            if getattr(artifact, key) != wanted:
                raise Refused("identity_mismatch:" + key)
        if digest(artifact.patch) != artifact.patch_sha256:
            raise Refused("artifact_hash_mismatch")
        if self.state_path.exists():
            raise Refused("application_record_exists:recover_first")
        # Intentionally narrow grammar; this is not a general unified-diff parser.
        text = artifact.patch.decode("utf-8")
        if (text.count("diff --git ") != 1 or
                not text.startswith("diff --git a/task_list.py b/task_list.py\n") or
                "--- a/task_list.py\n+++ b/task_list.py\n" not in text or
                any(marker in text for marker in ("new file mode ", "deleted file mode ",
                                                   "rename from ", "rename to ", "GIT binary patch",
                                                   "old mode ", "new mode "))):
            raise Refused("patch_scope_unsupported")
        before = self.git.snapshot(self.repo)
        if before["head"] != artifact.base_commit:
            raise Refused("base_commit_changed")
        if self.git.run(self.repo, "status", "--porcelain=v1", "--untracked-files=all").stdout:
            raise Refused("workspace_dirty")
        patch_path = self.evidence / "artifact.diff"
        patch_path.write_bytes(artifact.patch)
        self.git.run(self.repo, "apply", "--check", str(patch_path))
        # Independent disposable repository computes post-image from actual patch bytes.
        stage = self.evidence / "staging"
        self.git.run(self.evidence, "clone", "--no-hardlinks", "--quiet", str(self.repo), str(stage))
        self.git.run(stage, "apply", str(patch_path))
        after = self.git.snapshot(stage)
        result = {"phase": "prepared", "identity": {k: getattr(artifact, k) for k in self.expected},
                  "patch_sha256": artifact.patch_sha256, "before": before,
                  "expected_after": after, "patch_path": str(patch_path), "apply_count": 0}
        self.record(result)
        return result

    def commit(self, lost_ack=False):
        lock = self.evidence / "apply.lock"
        try:
            handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as error:
            raise Refused("apply_busy") from error
        try:
            state = json.loads(self.state_path.read_text())
            if state["phase"] != "prepared":
                raise Refused("duplicate_or_uncertain_apply:recover_first")
            if self.git.snapshot(self.repo) != state["before"]:
                raise Refused("workspace_changed_since_preview")
            patch = Path(state["patch_path"])
            if digest(patch.read_bytes()) != state["patch_sha256"]:
                raise Refused("preview_bytes_changed")
            self.git.run(self.repo, "apply", "--check", str(patch))
            state["phase"] = "applying"
            self.record(state)
            result = self.git.run(self.repo, "apply", str(patch), check=False)
            state["apply_count"] += 1
            if lost_ack:
                # Simulates the gap after Git modifies files and before a receipt is recorded.
                raise Refused("apply_ack_lost")
            observed = self.git.snapshot(self.repo)
            state.update(phase="applied" if result.returncode == 0 and observed == state["expected_after"]
                         else "apply_unknown", observed_after=observed)
            self.record(state)
            return state
        finally:
            os.close(handle)
            lock.unlink()

    def recover(self):
        state = json.loads(self.state_path.read_text())
        observed = self.git.snapshot(self.repo)
        if observed == state["expected_after"]:
            result = "applied_observed_requires_verification"
        elif observed == state["before"]:
            result = "baseline_observed_requires_explicit_retry"
        else:
            result = "unknown_requires_reconciliation"
        state.update(phase=result, recovered_snapshot=observed)
        self.record(state)
        return state
