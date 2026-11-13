"""Controlled scheduling probe; no JS evaluator, model calls, or sandbox claims.

Run with Python 3.11+: python3 scheduler_probe.py --output NEW_RUN_DIRECTORY
All actual file effects stay in that fresh directory. No arbitrary code input.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
from pathlib import Path
import sys


class Runtime:
    """One admission path for both direct and nested calls (teaching design)."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.ledger: list[dict] = []
        self.gate = asyncio.Semaphore(3)
        self.reads_started = 0
        self.both_reads = asyncio.Event()
        self.release_reads = asyncio.Event()
        self.write_committed = asyncio.Event()
        self.release_response = asyncio.Event()

    async def invoke(self, name: str, path: str, *, origin: str, cell=None):
        record = dict(call_id=f"call-{len(self.ledger) + 1}", origin=origin,
                      cell_id=cell.cell_id if cell else None, requested=path,
                      actual=path, handler_count=0, outcome="queued", effect="none")
        self.ledger.append(record)
        try:
            if cell:
                await cell.ready.wait()
                if cell.stopping:
                    raise asyncio.CancelledError
            # The hook is authored test input, not executable untrusted data.
            if path == "rewrite.txt":
                path = "protected.txt"
            record["actual"] = path
            target = (self.root / path).resolve()
            if not target.is_relative_to(self.root) or target.name == "protected.txt":
                record["outcome"] = "denied"
                record["reason"] = "protected_target"
                raise PermissionError("protected_target")
            if name not in {"read", "check", "write_then_block"}:
                record["outcome"] = "denied"
                record["reason"] = "tool_not_enabled"
                raise PermissionError("tool_not_enabled")
            async with self.gate:
                if cell and cell.stopping:
                    raise asyncio.CancelledError
                record["handler_count"] = 1
                record["outcome"] = "running"
                if name == "read":
                    self.reads_started += 1
                    if self.reads_started >= 2:
                        self.both_reads.set()
                    await self.release_reads.wait()
                    value = target.read_text(encoding="utf-8")
                elif name == "check":
                    # Fixed, authored program; no interpolation/eval/shell.
                    code = ("import json,sys; d=json.load(open(sys.argv[1])); "
                            "assert len(d['tasks']) == 2; print('CHECK_OK')")
                    proc = await asyncio.create_subprocess_exec(
                        sys.executable, "-c", code, str(target),
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    try:
                        stdout, stderr = await proc.communicate()
                    except BaseException:
                        if proc.returncode is None:
                            proc.kill()
                        await proc.wait()
                        raise
                    value = dict(exit_code=proc.returncode,
                                 stdout=stdout.decode(), stderr=stderr.decode())
                    if proc.returncode:
                        raise RuntimeError(value)
                else:
                    target.write_text("committed once\n", encoding="utf-8")
                    record["effect"] = "committed"
                    self.write_committed.set()
                    await self.release_response.wait()
                    value = "write acknowledged"
                record["outcome"] = "succeeded"
                return value
        except asyncio.CancelledError:
            record["outcome"] = "unknown" if record["effect"] == "committed" else "cancelled"
            raise
        except Exception:
            if record["outcome"] != "denied":
                record["outcome"] = "failed"
            raise


class Cell:
    """Cell lifecycle only. It does not execute JavaScript or own an OS worker."""

    def __init__(self, cell_id: str, runtime: Runtime, session_store: dict):
        self.cell_id = cell_id
        self.runtime = runtime
        self.ready = asyncio.Event()
        self.stopping = False
        self.tasks: list[asyncio.Task] = []
        self.snapshot = copy.deepcopy(session_store)
        self.writes: dict = {}
        self.session_store = session_store
        self.output: list[str] = []
        self.cursor = 0

    def start_call(self, name: str, path: str):
        if self.stopping:
            raise RuntimeError("cell_closed")
        task = asyncio.create_task(self.runtime.invoke(name, path, origin="nested", cell=self))
        self.tasks.append(task)
        return task

    def store(self, key, value):
        # Validate before mutating, and copy instead of keeping mutable references.
        value = json.loads(json.dumps(value, allow_nan=False))
        self.snapshot[key] = value
        self.writes[key] = copy.deepcopy(value)

    def load(self, key):
        return copy.deepcopy(self.snapshot.get(key))

    def observe(self):
        delta = self.output[self.cursor:]
        self.cursor = len(self.output)
        return {"cell_id": self.cell_id, "kind": "yielded", "items": delta}

    async def close(self, *, terminate=False):
        # Closing the cell does not replace each nested tool's recorded outcome.
        self.stopping = True
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if not terminate:
            self.session_store.update(copy.deepcopy(self.writes))
        return {"cell_id": self.cell_id, "kind": "terminated" if terminate else "completed"}


async def probe(root: Path):
    (root / "tasks.json").write_text(json.dumps({"tasks": ["add", "list"]}), encoding="utf-8")
    (root / "README.md").write_text("task-list: add and list tasks\n", encoding="utf-8")
    runtime, session = Runtime(root), {}
    observations, checks = [], []
    cell = Cell("cell-1", runtime, session)
    a = cell.start_call("read", "tasks.json")
    b = cell.start_call("read", "README.md")
    await asyncio.sleep(0)  # Give both tasks an opportunity to reach ready.
    assert len(runtime.ledger) == 2 and all(r["handler_count"] == 0 for r in runtime.ledger)
    checks.append("ready_gate_blocks_handlers")
    cell.ready.set()
    await asyncio.wait_for(runtime.both_reads.wait(), 2)
    assert not a.done() and not b.done()
    checks.append("two_reads_are_inflight_together")
    cell.output.append("A: reads started")
    observations.append(cell.observe())
    assert observations[-1]["kind"] == "yielded" and not a.done()
    check = cell.start_call("check", "tasks.json")
    runtime.release_reads.set()
    results = await asyncio.gather(a, b, check)
    assert json.loads(results[0])["tasks"] == ["add", "list"]
    assert "add and list" in results[1]
    assert results[2]["exit_code"] == 0 and results[2]["stdout"] == "CHECK_OK\n"
    cell.output.append("B: reads and process check completed")
    observations.append(cell.observe())
    assert observations[-1]["items"] == ["B: reads and process check completed"]
    observations.append(await cell.close())
    checks.extend(["real_files_and_process_checked", "yield_preserves_pending_calls_and_delta"])

    denied = Cell("cell-denied", runtime, session)
    denied.ready.set()
    for path in ("protected.txt", "rewrite.txt"):
        for origin in ("direct", "nested"):
            try:
                if origin == "direct":
                    await runtime.invoke("read", path, origin=origin)
                else:
                    await denied.start_call("read", path)
            except PermissionError:
                pass
            else:
                raise AssertionError("protected action reached handler")
    denial_records = runtime.ledger[-4:]
    assert all(r["handler_count"] == 0 and r["actual"] == "protected.txt" for r in denial_records)
    assert {r["reason"] for r in denial_records} == {"protected_target"}
    checks.append("direct_nested_actual_target_denials_match")
    # An allowed control proves the nested path was not globally disabled.
    assert "add and list" in await denied.start_call("read", "README.md")
    failures = await asyncio.gather(denied.start_call("read", "missing.txt"),
                                    denied.start_call("read", "README.md"), return_exceptions=True)
    assert isinstance(failures[0], FileNotFoundError) and isinstance(failures[1], str)
    checks.append("partial_failure_preserves_sibling_result")
    await denied.close()

    writer = Cell("cell-writer", runtime, session)
    writer.ready.set()
    writer.store("k", {"n": 1})
    stale_reader = Cell("cell-snapshot", runtime, session)
    assert stale_reader.load("k") is None
    await writer.close()
    reader = Cell("cell-reader", runtime, session)
    value = reader.load("k")
    value["n"] = 9
    assert reader.load("k") == {"n": 1} and stale_reader.load("k") is None
    try:
        reader.store("k", {"bad": object()})
    except TypeError:
        pass
    else:
        raise AssertionError("nonserializable value stored")
    assert reader.load("k") == {"n": 1}
    checks.append("store_is_snapshot_and_copy_not_shared_heap")

    ambiguous = Cell("cell-cancel", runtime, session)
    ambiguous.ready.set()
    ambiguous.store("uncommitted", "lost")
    ambiguous.start_call("write_then_block", "effect.txt")
    await asyncio.wait_for(runtime.write_committed.wait(), 2)
    observations.append(ambiguous.observe())
    observations.append(await ambiguous.close(terminate=True))
    assert runtime.ledger[-1]["outcome"] == "unknown"
    assert (root / "effect.txt").read_text() == "committed once\n"
    assert runtime.ledger[-1]["handler_count"] == 1 and "uncommitted" not in session
    assert all(t.done() for t in ambiguous.tasks)
    try:
        ambiguous.start_call("read", "README.md")
    except RuntimeError as error:
        assert str(error) == "cell_closed"
    else:
        raise AssertionError("closed cell accepted a new call")
    checks.append("cancel_preserves_effect_and_unknown_without_replay")
    return dict(status="passed_local_prototype_only", checks=checks, observations=observations,
                process_result=results[2], ledger=runtime.ledger,
                limitations=["No model or JavaScript evaluator", "No isolated OS worker",
                             "No credentials, external service, or Codex Rust tests",
                             "No MIN-30, live lab, lifecycle, or checkpoint gate discharged"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    try:
        result = asyncio.run(asyncio.wait_for(probe(args.output), 10))
    except BaseException as error:
        (args.output / "failure.txt").write_text(repr(error), encoding="utf-8")
        raise
    (args.output / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                                            encoding="utf-8")
    print(json.dumps({"status": result["status"], "checks": len(result["checks"]),
                      "evidence": str(args.output / "result.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
