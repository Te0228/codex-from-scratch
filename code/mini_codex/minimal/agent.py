"""Coordinate model, history, tools and independent completion checks."""

import json

from .history import History
from .model import Model
from .tools import Tools
from .verifier import Verifier


class Agent:
    def __init__(self, model: Model, tools: Tools, verifier: Verifier, instructions: str):
        self.model = model
        self.tools = tools
        self.verifier = verifier
        self.instructions = instructions

    def run(self, task: str, max_steps: int = 8) -> dict:
        if type(max_steps) is not int or max_steps < 0:
            raise ValueError("max_steps 必须是非负整数")
        history = History(task)
        for step in range(1, max_steps + 1):
            verification = self._step(history)
            if verification is not None and verification["passed"]:
                return self._result("passed", step, history, verification)
        return self._result("step_limit", max_steps, history, self.verifier.check())

    def _step(self, history: History):
        output = self.model.sample(history.request(self.instructions, self.tools.specs()))
        if not output or not isinstance(output, list) or not all(isinstance(i, dict) for i in output):
            raise ValueError("模型必须返回非空 output 对象列表")
        history.add_output(output)
        calls = [item for item in output if item.get("type") == "function_call"]
        if calls:
            self._execute_tools(history, calls)
            return None
        # Teaching simplification: output without tool calls requests completion.
        return self._verify_completion(history)

    def _execute_tools(self, history: History, calls: list) -> None:
        for call in calls:
            call_id = call.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                raise ValueError("工具调用必须包含非空 call_id")
            history.add_tool_result(call_id, self.tools.execute(call))

    def _verify_completion(self, history: History) -> dict:
        verification = self.verifier.check()
        if not verification["passed"]:
            history.feedback("控制器重跑测试仍失败，请继续修复。" +
                             json.dumps(verification, ensure_ascii=False))
        return verification

    @staticmethod
    def _result(status: str, steps: int, history: History, verification: dict) -> dict:
        return {"status": status, "steps": steps, "history": history.snapshot(),
                "verification": verification}
