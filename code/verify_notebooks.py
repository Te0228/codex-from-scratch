#!/usr/bin/env python3
"""Validate and execute textbook notebooks in fresh kernels; retain actual outputs."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import time
import traceback
import uuid

import nbformat
from nbclient import NotebookClient
from jupyter_client import KernelManager

ROOT = Path(__file__).resolve().parents[1]


def source_hash(notebook):
    inputs = [{"cell_type": c.cell_type, "source": c.source, "metadata": c.metadata}
              for c in notebook.cells]
    return hashlib.sha256(json.dumps(inputs, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def execute(path: Path, destination: Path, *, timeout: int, publish_outputs: bool):
    started = time.monotonic()
    nb = nbformat.read(path, as_version=4)
    nbformat.validate(nb)
    original_hash = source_hash(nb)
    for cell in nb.cells:
        if cell.cell_type == "code":
            cell.execution_count = None
            cell.outputs = []
    result = {"notebook": str(path.relative_to(ROOT)), "input_source_sha256": original_hash,
              "scope": nb.metadata.get("codex_from_scratch", {}).get("scope", "unspecified"),
              "status": "running"}
    # Use this command's Python, even if a different global python3 kernel exists.
    manager = KernelManager(kernel_name="python3")
    manager.kernel_spec.argv = [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"]
    try:
        client = NotebookClient(nb, km=manager, timeout=timeout, allow_errors=False,
                                resources={"metadata": {"path": str(ROOT / "notebooks")}})
        client.execute(cwd=str(ROOT / "notebooks"))
        executed = [c for c in nb.cells if c.cell_type == "code"]
        if not executed or any(c.execution_count is None for c in executed):
            raise AssertionError("Every code cell must have an actual execution count")
        if any(o.output_type == "error" for c in executed for o in c.outputs):
            raise AssertionError("Unexpected error output")
        result.update(status="passed", code_cells=len(executed))
    except Exception as exc:
        result.update(status="failed", error_type=type(exc).__name__, error=str(exc))
        (destination / f"{path.stem}.traceback.log").write_text(traceback.format_exc())
    finally:
        # NotebookClient does not own a supplied manager, so explicitly reap it.
        if manager.has_kernel:
            try:
                manager.shutdown_kernel(now=True)
            except Exception as exc:
                result.update(status="failed", cleanup_error=type(exc).__name__)
        result["seconds"] = round(time.monotonic() - started, 3)
        result["output_notebook"] = str((destination / path.name).relative_to(ROOT))
        nbformat.write(nb, destination / path.name)
        if result["status"] == "passed" and publish_outputs:
            nbformat.write(nb, path)
        (destination / f"{path.stem}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(f"{path.name}: {result['status']} ({result['seconds']}s)", flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chapters", help="Comma-separated chapter IDs; default all 31")
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=900, help="Per-cell deadline in seconds")
    parser.add_argument("--publish-outputs", action="store_true", help="Save verified outputs into source notebooks")
    args = parser.parse_args()
    selected = [f"{int(n):02d}" for n in args.chapters.split(",")] if args.chapters else [f"{n:02d}" for n in range(1, 32)]
    if len(selected) != len(set(selected)) or any(not 1 <= int(n) <= 31 for n in selected):
        parser.error("Select unique chapter IDs from 01 to 31")
    paths = []
    for chapter in selected:
        matches = list((ROOT / "notebooks").glob(f"{chapter}-*.ipynb"))
        if len(matches) != 1:
            parser.error(f"Chapter {chapter} needs exactly one notebook; found {len(matches)}")
        paths.append(matches[0])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = ROOT / "runs/notebooks" / f"execute-{stamp}-{uuid.uuid4().hex[:8]}"
    destination.mkdir(parents=True)
    print(f"Execution evidence: {destination}", flush=True)
    environment = {"python": sys.version, "platform": platform.platform(), "cwd": str(ROOT),
                   "paid_model_calls": "disabled by notebook defaults; no .env loaded by this runner"}
    (destination / "environment.json").write_text(json.dumps(environment, indent=2) + "\n")
    with ThreadPoolExecutor(max_workers=max(1, min(args.jobs, 4))) as pool:
        futures = {pool.submit(execute, p, destination, timeout=args.timeout,
                               publish_outputs=args.publish_outputs): p for p in paths}
        results = []
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                path = futures[future]
                result = {"notebook": str(path.relative_to(ROOT)), "status": "failed",
                          "error_type": type(exc).__name__, "error": str(exc)}
                results.append(result)
                print(f"{path.name}: failed before/during notebook setup: {type(exc).__name__}", flush=True)
    report = {"environment": environment, "selected_chapters": selected,
              "passed": sum(r["status"] == "passed" for r in results),
              "failed": sum(r["status"] != "passed" for r in results),
              "results": sorted(results, key=lambda r: r["notebook"]),
              "scope_note": "Notebook cell execution; local mechanism experiments do not establish integrated or external-service acceptance."}
    (destination / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"passed": report["passed"], "failed": report["failed"], "report": str(destination / 'report.json')}), flush=True)
    return 0 if not report["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
