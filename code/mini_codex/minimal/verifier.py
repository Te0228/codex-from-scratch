"""Compare candidate output with fixed cases owned by the controller."""

from copy import deepcopy
import json
from pathlib import Path

from .executor import Executor


class Verifier:
    def __init__(self, workspace: Path, executor: Executor, cases: list):
        if not cases:
            raise ValueError("必须提供非空的固定测试案例")
        for case in cases:
            if not isinstance(case.get("name"), str) or not isinstance(case.get("input"), dict):
                raise ValueError("每个案例必须包含 name 字符串与 input 对象")
            if "expected" not in case:
                raise ValueError("每个案例必须包含 expected")
        self.workspace = Path(workspace).resolve()
        self.executor = executor
        self._cases = deepcopy(cases)

    def check(self) -> dict:
        results = [self._check_case(case) for case in self._cases]
        return {"passed": all(row["passed"] for row in results), "cases": results}

    def _check_case(self, case: dict) -> dict:
        result = {"name": case["name"], "expected": deepcopy(case["expected"]),
                  "actual": None, "passed": False, "exit_code": None}
        try:
            execution = self.executor.run_python(self.workspace, "task_list.py", case["input"])
            result.update(execution)
            if execution["exit_code"] == 0:
                result["actual"] = json.loads(execution["stdout"])
                result["passed"] = self._same_json(result["actual"], case["expected"])
        except (OSError, ValueError) as error:
            result["error"] = f"{type(error).__name__}: {error}"
        return result

    @staticmethod
    def _same_json(actual, expected) -> bool:
        # Python considers True == 1; JSON serialization preserves their types.
        options = {"sort_keys": True, "ensure_ascii": False, "allow_nan": False}
        return json.dumps(actual, **options) == json.dumps(expected, **options)
