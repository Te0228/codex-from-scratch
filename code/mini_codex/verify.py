"""Protected controller: fixed inputs and comparisons, never imports candidate code."""
import difflib
import hashlib
import json
from pathlib import Path
import shutil
import time

from .history import digest
from .worker import CHECKPOINT, RESOURCE_ROOT, LinuxWorker


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree_hash(root):
    files = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_symlink():
            raise ValueError("candidate_symlink_not_supported")
        if path.is_file():
            if len(files) >= 100 or path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("candidate_snapshot_limit")
            files[str(path.relative_to(root))] = sha256(path)
        elif not path.is_dir():
            raise ValueError("candidate_special_file_not_supported")
    return {"hash": digest(files), "files": files}


def save_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


class Verifier:
    def __init__(self):
        self.manifest = json.loads((RESOURCE_ROOT / "checks/manifest.json").read_text())
        self.cases = json.loads((RESOURCE_ROOT / "checks/cases.json").read_text())
        self.initial_protection = self.protection()
        ids = [case["id"] for case in self.cases]
        if (not ids or len(ids) != len(set(ids)) or ids != self.manifest["case_ids"] or
                len(ids) != self.manifest["expected_count"] or
                self.initial_protection != self.manifest["protected_hashes"]):
            raise ValueError("protected_manifest_mismatch")

    def protection(self):
        return {name: sha256((RESOURCE_ROOT if name.startswith(("checks/", "fixtures/")) else CHECKPOINT) / name)
                for name in self.manifest["protected_hashes"]}

    def check(self, workspace, deadline):
        before = tree_hash(workspace)
        worker = LinuxWorker(Path(workspace), read_only=True)
        rows = []
        for case in self.cases:
            observed = worker.probe("candidate", {"input": case["input"]},
                                    timeout=3, deadline=deadline)
            value, error = None, None
            if observed.process_status != "exited" or observed.cleanup_status != "confirmed":
                error = "execution_or_cleanup_unconfirmed"
            elif observed.exit_code != 0:
                error = "candidate_exit_nonzero"
            elif observed.truncated:
                error = "candidate_output_truncated"
            else:
                try:
                    value = json.loads(observed.output)
                except (ValueError, TypeError):
                    error = "invalid_candidate_json"
            # Strict JSON comparison: Python True == 1 must not accept a type mismatch.
            passed = error is None and digest(value) == digest(case["expected"])
            rows.append({"id": case["id"], "input": case["input"],
                         "expected": case["expected"], "actual": value,
                         "passed": passed, "error": error or (None if passed else "assertion_mismatch"),
                         "execution": observed.__dict__, "cleanup_evidence": worker.last_cleanup})
        after = tree_hash(workspace)
        protected = self.protection()
        intact = protected == self.initial_protection == self.manifest["protected_hashes"]
        exact = [row["id"] for row in rows] == self.manifest["case_ids"]
        stable = before == after
        return {"passed": bool(rows) and exact and intact and stable and all(r["passed"] for r in rows),
                "candidate": before, "snapshot_unchanged": stable,
                "case_ids": [r["id"] for r in rows], "expected_count": self.manifest["expected_count"],
                "executed_count": sum(r["execution"]["process_status"] == "exited" for r in rows),
                "assertion_count": sum(r["error"] in (None, "assertion_mismatch") for r in rows),
                "protected_hashes": protected, "protection_intact": intact,
                "cleanup_status": "confirmed" if worker.clean else "unknown", "cases": rows}

    def baseline(self, workspace, deadline):
        result = self.check(workspace, deadline)
        expected_red = [c["id"] for c in self.cases if c["red"]]
        actual_red = [r["id"] for r in result["cases"] if not r["passed"]]
        result["baseline_valid"] = (
            result["candidate"]["files"].get("task_list.py") == self.manifest["baseline_sha256"] and
            actual_red == expected_red and all(r["error"] in (None, "assertion_mismatch")
                                               for r in result["cases"]) and
            result["protection_intact"] and result["snapshot_unchanged"] and
            result["assertion_count"] == self.manifest["expected_count"])
        return result

    def freeze_and_check(self, workspace, destination, deadline):
        # Called only after dispatch is closed and the mutable worker is clean.
        before = tree_hash(workspace)
        shutil.copytree(workspace, destination, symlinks=True)
        frozen = tree_hash(destination)
        if before != frozen:
            raise ValueError("snapshot_copy_hash_mismatch")
        result = self.check(destination, deadline)
        result["frozen_snapshot"] = str(destination)
        result["frozen_hash"] = frozen["hash"]
        result["passed"] = result["passed"] and result["candidate"] == frozen
        old = (RESOURCE_ROOT / "fixtures/task-list/task_list.py").read_text().splitlines(True)
        new = (destination / "task_list.py").read_text().splitlines(True)
        (destination.parent / "candidate.diff").write_text("".join(
            difflib.unified_diff(old, new, "baseline/task_list.py", "snapshot/task_list.py")))
        return result
