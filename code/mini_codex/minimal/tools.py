"""Three tools with explicit file and test responsibilities."""

from copy import deepcopy
import json
from pathlib import Path

from .verifier import Verifier


TOOL_SPECS = [
    {"type": "function", "name": "read_file", "description": "读取工作目录内的 UTF-8 文件",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                    "required": ["path"], "additionalProperties": False}, "strict": True},
    {"type": "function", "name": "replace_text", "description": "精确替换文件中唯一出现的文本",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
         "required": ["path", "old", "new"], "additionalProperties": False}, "strict": True},
    {"type": "function", "name": "run_tests", "description": "运行控制器保存的固定测试",
     "parameters": {"type": "object", "properties": {}, "required": [],
                    "additionalProperties": False}, "strict": True},
]


class Tools:
    def __init__(self, workspace: Path, verifier: Verifier):
        self.workspace = Path(workspace).resolve()
        self.verifier = verifier

    def specs(self) -> list:
        return deepcopy(TOOL_SPECS)

    def execute(self, call: dict) -> dict:
        try:
            arguments = json.loads(call["arguments"])
            if not isinstance(arguments, dict):
                raise ValueError("工具参数必须是 JSON 对象")
            name = call["name"]
            if name == "read_file":
                return self.read_file(**arguments)
            if name == "replace_text":
                return self.replace_text(**arguments)
            if name == "run_tests":
                return self.run_tests(**arguments)
            raise ValueError(f"未知工具：{name}")
        except (KeyError, TypeError, ValueError, OSError) as error:
            return {"error": f"{type(error).__name__}: {error}"}

    def read_file(self, path: str) -> dict:
        return {"path": path, "content": self._path(path).read_text(encoding="utf-8")}

    def replace_text(self, path: str, old: str, new: str) -> dict:
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise ValueError("old 必须是非空字符串，new 必须是字符串")
        target = self._path(path)
        content = target.read_text(encoding="utf-8")
        if content.count(old) != 1:
            raise ValueError("old 必须在文件中恰好出现一次")
        target.write_text(content.replace(old, new, 1), encoding="utf-8")
        return {"path": path, "replacements": 1}

    def run_tests(self) -> dict:
        return self.verifier.check()

    def _path(self, path: str) -> Path:
        if not isinstance(path, str) or not path:
            raise ValueError("path 必须是非空相对路径")
        relative = Path(path)
        target = (self.workspace / relative).resolve()
        if relative.is_absolute() or not target.is_relative_to(self.workspace):
            raise ValueError("文件必须位于工作目录内，且使用相对路径")
        return target
