"""Replace the model without changing the loop, tools, or history."""

from copy import deepcopy
import json
from pathlib import Path
from typing import Protocol
import urllib.error
import urllib.request


class Model(Protocol):
    def sample(self, request: dict) -> list[dict]: ...


class ScriptedModel:
    """Replay explicit outputs; exhausting a script never changes any file."""

    def __init__(self, script: list):
        self.script = deepcopy(script)
        self.requests = []

    def sample(self, request: dict) -> list[dict]:
        index = len(self.requests)
        self.requests.append(deepcopy(request))
        if index < len(self.script):
            return deepcopy(self.script[index])
        return [{"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "脚本已结束。"}]}]


class DeepSeekModel:
    def __init__(self, api_key: str, model: str):
        if not api_key or not model:
            raise ValueError("必须提供 API key 和模型名称")
        self._api_key = api_key
        self.model = model

    @classmethod
    def from_env_file(cls, path: Path):
        settings = {}
        for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if not separator:
                raise ValueError("配置行必须使用 KEY=VALUE 格式")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            settings[key.strip()] = value
        if settings.get("OPENAI_BASE_URL") != "https://api.deepseek.com":
            raise ValueError("本章要求 OPENAI_BASE_URL=https://api.deepseek.com")
        return cls(settings.get("OPENAI_API_KEY", ""), settings.get("OPENAI_MODEL", ""))

    def sample(self, request: dict) -> list[dict]:
        payload = {**deepcopy(request), "model": self.model,
                   "reasoning": {"effort": "none"}, "max_output_tokens": 2048,
                   "stream": False}
        http_request = urllib.request.Request(
            "https://api.deepseek.com/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self._api_key}",
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(http_request, timeout=60) as response:
                body = json.load(response)
        except urllib.error.HTTPError as error:
            error.close()
            raise RuntimeError(f"DeepSeek 请求失败，HTTP {error.code}") from None
        except urllib.error.URLError:
            raise RuntimeError("无法连接 DeepSeek") from None
        if body.get("status") != "completed":
            raise RuntimeError("DeepSeek 响应未完成")
        output = body.get("output")
        if not isinstance(output, list) or not all(isinstance(item, dict) for item in output):
            raise RuntimeError("DeepSeek output 必须是对象列表")
        return deepcopy(output)
