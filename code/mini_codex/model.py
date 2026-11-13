"""Responses API full-response adapter. No Codex CLI and no transport retries."""
import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .contracts import ModelResult


class ResponsesModel:
    mode = "live"

    def __init__(self, *, endpoint=None):
        base_url = os.environ.get("OPENAI_BASE_URL", "")
        profiles = {
            "https://api.openai.com/v1": ("openai", "api.openai.com", "/v1/responses"),
            "https://api.deepseek.com": ("deepseek", "api.deepseek.com", "/responses"),
        }
        selected = profiles.get("https://api.openai.com/v1" if base_url == "" else base_url.rstrip("/"))
        if selected is None:
            # Never silently forward a different provider's key to the official API.
            # Do not echo the configured URL; it may contain embedded credentials.
            raise ValueError("unsupported_OPENAI_BASE_URL: chapter01 requires an explicit supported provider")
        self.provider, host, self.api_path = selected
        self.key = os.environ.get("OPENAI_API_KEY", "")
        self.model = os.environ.get("OPENAI_MODEL", "")
        if not self.key or not self.model:
            raise ValueError("live requires OPENAI_API_KEY and OPENAI_MODEL")
        if endpoint is not None and not (len(endpoint) == 3 and endpoint[0] == "http"
                                         and endpoint[1] == "127.0.0.1"):
            raise ValueError("endpoint override is reserved for loopback HTTP tests")
        self.endpoint = endpoint or ("https", host, 443)
        self.last_cleanup = "not_started"

    def sample(self, request):
        # Never log headers. The controller's secret is not in prompts or worker env.
        payload = {"model": self.model, "instructions": request.instructions,
                   "input": list(request.messages), "tools": list(request.tool_specs),
                   "stream": False, "max_output_tokens": 2048}
        if self.provider == "openai":
            payload.update(store=False, include=["reasoning.encrypted_content"])
        # DeepSeek Responses is stateless and accepts plain reasoning items in
        # input; it does not implement OpenAI's store/include controls.
        if request.model_options.get("reasoning_effort"):
            payload["reasoning"] = {"effort": request.model_options["reasoning_effort"]}
        remaining = request.call_context.deadline - time.monotonic()
        if remaining <= 0:
            return ModelResult(None, "failed", (), provider_error="model_deadline")
        packet = {"payload": payload, "key": self.key, "timeout": remaining,
                  "endpoint": self.endpoint, "api_path": self.api_path}
        try:
            child = subprocess.Popen([sys.executable, "-I", str(Path(__file__).with_name("model_http.py"))],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True, env={"PATH": "/usr/bin:/bin"})
        except OSError:
            return ModelResult(None, "failed", (), provider_error="model_child_start_failed")
        self.last_cleanup = "unknown"
        def reap():
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                child.communicate(timeout=2)
                self.last_cleanup = "confirmed"
            except subprocess.TimeoutExpired:
                self.last_cleanup = "unknown"
        try:
            out, _ = child.communicate(json.dumps(packet).encode(),
                timeout=max(0.001, request.call_context.deadline - time.monotonic()))
            self.last_cleanup = "confirmed"
            decoded = json.loads(out)
            return ModelResult(decoded.get("request_id"), decoded["response_status"],
                               tuple(decoded["output_items"]), decoded.get("usage"),
                               decoded.get("provider_error"), response_id=decoded.get("response_id"),
                               usage_presence=decoded.get("usage_presence", "not_observable"))
        except subprocess.TimeoutExpired:
            # Socket idle timeouts cannot bound DNS or trickled headers. Killing the
            # trusted request owner enforces the absolute deadline for every phase.
            reap()
            return ModelResult(None, "unknown", (), provider_error="model_deadline")
        except (OSError, ValueError, KeyError):
            return ModelResult(None, "failed", (), provider_error="model_child_response_format")
        finally:
            if child.poll() is None:
                reap()


def function_call(call_id, name, **arguments):
    return {"type": "function_call", "call_id": call_id, "name": name,
            "arguments": json.dumps(arguments), "status": "completed"}


def message(text):
    return {"type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


class ReplayModel:
    """Replaces model output only. Workers/verification remain real in integration runs."""
    mode = "replay"

    def __init__(self, responses=None):
        self.index = 0
        self.requests = []
        self.responses = responses if responses is not None else [
            [function_call("search-1", "search", query="status")],
            [function_call("read-1", "read", path="task_list.py")],
            [function_call("test-red", "exec", command=
                "printf '%s' '{\"tasks\":[{\"id\":2,\"status\":\"done\"}],\"status\":\"done\"}' | python3 -I task_list.py")],
            [function_call("patch-1", "apply_patch", path="task_list.py",
                           old='task.get("state")', new='task.get("status")')],
            [function_call("test-green", "exec", command=
                "printf '%s' '{\"tasks\":[{\"id\":2,\"status\":\"done\"}],\"status\":\"done\"}' | python3 -I task_list.py")],
            [message("已修复 status 筛选，请独立验证。")],
        ]

    def sample(self, request):
        self.requests.append(copy.deepcopy(request))
        items = self.responses[self.index] if self.index < len(self.responses) else []
        self.index += 1
        return ModelResult(f"replay-{self.index}", "completed", tuple(copy.deepcopy(items)))
