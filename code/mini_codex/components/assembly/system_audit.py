"""Final-package inventory and evidence gate; no authoring-contract dependency.

This corroborates the protected Verifier's retained results; it does not rerun
candidate code or accept unconfigured services. Controller ownership of evidence
is required: byte hashes alone cannot establish producer trust.
"""
from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def capture_artifact(path: Path):
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError("artifact_missing")
    return {"path": str(path), "size": path.stat().st_size, "sha256": digest(path.read_bytes())}


def package_inventory():
    """Actually import final modules; availability is not runtime acceptance."""
    expected = {
        "context.context": (), "transport.streaming": (), "tools.toolbox": (),
        "processes.process_probe": (), "scheduling.scheduler": (),
        "configuration.snapshots": (), "permissions.policy": (), "sandbox.planner": (),
        "sessions.journal": (), "memory.ledger": (), "goals.goals": (),
        "skills.skill_catalog": (), "mcp.ui_bridge": (), "plugins.catalog": (),
        "orchestration.scheduler_probe": (), "agents.coordinator": (),
        "rpc.relay": (), "clients.protocol_lab": (), "environments.ownership_probe": (),
        "hosts.local_host": ("LocalHost",), "artifacts.artifact_gate": ("ArtifactGate",),
        "media.media_core": ("AttachmentStore", "Jobs"),
        "realtime.events": ("TranscriptReducer", "EpochChannel"),
        "search.citations": ("CitationTable",),
        "computer_use.observation_ledger": ("ObservationLedger",),
        "telemetry.meter": ("Ledger", "observe_raw"), "cache.cache_stats": ("summarize",),
        "evaluation.aggregate": ("aggregate",), "distribution.upgrade_lab": ("upgrade",),
        "assembly.system_audit": ("evaluate_run",),
    }
    package_root = Path(__file__).resolve().parents[2]
    rows = []
    component_root = package_root / "components"
    available = {".".join(path.relative_to(component_root).with_suffix("").parts)
                 for path in component_root.rglob("*.py") if path.name != "__init__.py"}
    if set(expected) - available:
        raise ValueError("required_component_module_missing:" + ",".join(sorted(set(expected) - available)))
    for relative in sorted(available):
        interfaces = expected.get(relative, ())
        module = importlib.import_module("mini_codex.components." + relative)
        path = Path(module.__file__).resolve()
        if not path.is_relative_to(package_root):
            raise ValueError("component_import_outside_final_package")
        missing = [name for name in interfaces if not hasattr(module, name)]
        declared = sorted(name for name, value in vars(module).items()
                          if not name.startswith("_") and callable(value)
                          and getattr(value, "__module__", None) == module.__name__)
        if not declared:
            missing.append("any_declared_public_interface")
        rows.append({"module": module.__name__, "path": str(path), "sha256": digest(path.read_bytes()),
                     "declared_public_interfaces": declared,
                     "required_interfaces": list(interfaces), "missing_interfaces": missing,
                     "status": "imported" if not missing else "interface_missing"})
    return {"scope": "final_package_imports", "modules": rows,
            "passed": all(row["status"] == "imported" for row in rows),
            "limit": "Import presence is not runtime integration or service acceptance."}


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def evaluate_run(run_dir: Path, expected_cases=None):
    """Reject missing/inconsistent evidence. Expected cases are controller-owned."""
    run_dir = Path(run_dir).resolve()
    from ...worker import RESOURCE_ROOT
    contract_root = RESOURCE_ROOT / "checks"
    expected_cases = _load(contract_root / "cases.json") if expected_cases is None else expected_cases
    expected_ids = [case["id"] for case in expected_cases]
    reasons, artifacts = [], []
    if not expected_ids or len(expected_ids) != len(set(expected_ids)):
        return {"status": "rejected", "scope": "final_demo", "reasons": ["invalid_expected_case_contract"]}
    required = {"result": run_dir / "result.json", "verification": run_dir / "final-verification.json"}
    for key, path in required.items():
        if not path.is_file():
            reasons.append("missing_evidence:" + key)
        else:
            artifacts.append(capture_artifact(path))
    if reasons:
        return {"status": "rejected", "scope": "final_demo", "reasons": reasons, "artifacts": artifacts}
    try:
        result, verification = _load(required["result"]), _load(required["verification"])
    except (ValueError, OSError):
        return {"status": "rejected", "scope": "final_demo", "reasons": ["invalid_evidence_json"]}
    state = result.get("task_status")
    if state != "passed":
        return {"status": state if state in {"blocked", "failed", "cancelled", "unknown"} else "rejected",
                "scope": "final_demo", "reasons": ["run_did_not_pass"], "artifacts": artifacts}
    if result.get("cleanup_status") != "confirmed":
        reasons.append("run_cleanup_unconfirmed")
    if verification.get("passed") is not True:
        reasons.append("verification_not_passed")
    if verification.get("case_ids") != expected_ids:
        reasons.append("case_identity_mismatch")
    for field in ("expected_count", "executed_count", "assertion_count"):
        if type(verification.get(field)) is not int or verification[field] != len(expected_ids):
            reasons.append("case_count_mismatch:" + field)
    for field in ("snapshot_unchanged", "protection_intact"):
        if verification.get(field) is not True:
            reasons.append(field + "_unconfirmed")
    if verification.get("cleanup_status") != "confirmed":
        reasons.append("verifier_cleanup_unconfirmed")
    rows = verification.get("cases", [])
    if not isinstance(rows, list) or [row.get("id") for row in rows] != expected_ids:
        reasons.append("case_rows_mismatch")
    else:
        for expected, row in zip(expected_cases, rows):
            execution = row.get("execution", {})
            if (canonical(row.get("input")) != canonical(expected["input"]) or
                    canonical(row.get("expected")) != canonical(expected["expected"])):
                reasons.append("case_contract_changed:" + expected["id"])
            if (row.get("passed") is not True or row.get("error") is not None or
                    canonical(row.get("actual")) != canonical(expected["expected"])):
                reasons.append("case_assertion_failed:" + expected["id"])
            if (execution.get("process_status") != "exited" or type(execution.get("exit_code")) is not int or
                    execution.get("exit_code") != 0 or
                    execution.get("cleanup_status") != "confirmed" or execution.get("truncated") is not False):
                reasons.append("case_execution_unconfirmed:" + expected["id"])
    snapshot = run_dir / "snapshot"
    if not snapshot.is_dir():
        reasons.append("missing_frozen_snapshot")
    else:
        files = {}
        for path in sorted(snapshot.rglob("*")):
            if path.is_symlink():
                reasons.append("snapshot_symlink")
            elif path.is_file():
                files[str(path.relative_to(snapshot))] = digest(path.read_bytes())
        observed = {"hash": digest(canonical(files).encode()), "files": files}
        if observed != verification.get("candidate") or observed["hash"] != verification.get("frozen_hash"):
            reasons.append("frozen_snapshot_hash_mismatch")
        if not files:
            reasons.append("empty_frozen_snapshot")
    return {"status": "rejected" if reasons else "passed", "scope": "final_demo", "reasons": reasons,
            "run_id": result.get("run_id"), "mode": result.get("mode"),
            "verified_case_ids": expected_ids, "artifacts": artifacts,
            "limits": ["Corroborates retained protected-run evidence; does not rerun the candidate.",
                       "Does not accept external services or unselected capabilities."]}
