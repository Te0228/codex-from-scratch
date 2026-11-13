"""Aggregate trusted judge records against an externally frozen plan.

Teaching prototype, not a Loop, sandbox, process runner, or semantic judge.
Hashes bind retained evidence bytes; they do not establish their truth/producer.
The caller must keep the manifest, judge and evidence root outside Agent control.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

DIMENSIONS = ("task_quality", "mechanism", "service_runtime", "artifact_identity")
STATUSES = ("passed", "failed", "blocked", "unknown", "interrupted", "not_run")
IDENTITY = ("case_id", "attempt_id", "mode", "target_id", "platform")


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def check_evidence(root: Path, ref: dict) -> None:
    relative = Path(ref["path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("evidence_path_escape")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("evidence_missing_or_escape")
    if hashlib.sha256(path.read_bytes()).hexdigest() != ref["sha256"]:
        raise ValueError("evidence_hash_mismatch")


def aggregate(plan: dict, records: list[dict], root: Path) -> dict:
    if plan.get("schema") != "evaluation-plan-v1":
        raise ValueError("unsupported_plan_schema")
    slots = plan["slots"]
    if not slots:
        raise ValueError("empty_plan")
    by_id = {}
    attempts = set()
    for slot in slots:
        if slot["slot_id"] in by_id or slot["attempt_id"] in attempts:
            raise ValueError("duplicate_plan_identity")
        required = slot["required_dimensions"]
        if (not required or len(required) != len(set(required)) or
                any(d not in DIMENSIONS for d in required)):
            raise ValueError("invalid_required_dimensions")
        if slot["mode"] not in ("fixture", "replay", "live"):
            raise ValueError("invalid_mode")
        by_id[slot["slot_id"]] = slot
        attempts.add(slot["attempt_id"])
    plan_hash = digest(plan)
    observed = {}
    for record in records:
        key = record["slot_id"]
        if key not in by_id or key in observed:
            raise ValueError("extra_or_duplicate_record")
        slot = by_id[key]
        if record.get("schema") != "evaluation-record-v1":
            raise ValueError("unsupported_record_schema")
        if record.get("plan_sha256") != plan_hash:
            raise ValueError("wrong_plan")
        if any(record.get(k) != slot[k] for k in IDENTITY):
            raise ValueError("record_identity_mismatch")
        if set(record["dimensions"]) != set(DIMENSIONS):
            raise ValueError("dimension_missing_or_extra")
        for dimension in record["dimensions"].values():
            status = dimension["status"]
            if status not in STATUSES or not dimension.get("reason"):
                raise ValueError("invalid_dimension_state")
            refs = dimension.get("evidence", [])
            if status in ("passed", "failed") and not refs:
                raise ValueError("conclusive_state_without_evidence")
            for ref in refs:
                check_evidence(root, ref)
        observed[key] = record

    counts = {d: Counter({s: 0 for s in STATUSES}) for d in DIMENSIONS}
    rows = []
    for slot in slots:
        record = observed.get(slot["slot_id"])
        states = {d: record["dimensions"][d]["status"] if record else "not_run"
                  for d in DIMENSIONS}
        for d, status in states.items():
            counts[d][status] += 1
        required_states = [states[d] for d in slot["required_dimensions"]]
        # The gate status describes this slot's declared contract only. A negative
        # probe can pass its detection contract while task_quality remains failed.
        gate = next((s for s in ("failed", "unknown", "interrupted", "blocked", "not_run")
                     if s in required_states), "passed")
        rows.append({"slot_id": slot["slot_id"], "attempt_id": slot["attempt_id"],
                     "dimensions": states, "case_gate": gate})
    return {"schema": "evaluation-summary-v1", "plan_sha256": plan_hash,
            "planned_attempts": len(slots), "recorded_attempts": len(observed),
            "dimensions": {d: {"denominator": len(slots), "counts": dict(c),
                               "verified_pass_fraction": c["passed"] / len(slots)}
                           for d, c in counts.items()},
            "case_gates": dict(Counter(r["case_gate"] for r in rows)), "rows": rows,
            "limit": "Records require a trusted external judge; this aggregator proves no runtime behavior."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = aggregate(json.loads(args.plan.read_text()),
                       json.loads(args.records.read_text()), args.evidence_root)
    # Exclusive creation preserves prior reports. Exit 0 means aggregation completed,
    # not that the measured tasks passed; inspect the separate dimensions and gates.
    with args.out.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
