"""Own the Responses message list and serialize matching tool results."""

from copy import deepcopy
import json


class History:
    def __init__(self, task: str):
        self._items = [{"role": "user", "content": task}]

    def request(self, instructions: str, tools: list) -> dict:
        return {"instructions": instructions, "input": self.snapshot(),
                "tools": deepcopy(tools)}

    def add_output(self, items: list) -> None:
        self._items.extend(deepcopy(items))

    def add_tool_result(self, call_id: str, result: dict) -> None:
        self._items.append({"type": "function_call_output", "call_id": call_id,
                            "output": json.dumps(result, ensure_ascii=False)})

    def feedback(self, text: str) -> None:
        self._items.append({"role": "user", "content": text})

    def snapshot(self) -> list:
        return deepcopy(self._items)
