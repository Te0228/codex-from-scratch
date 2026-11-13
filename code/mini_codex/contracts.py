"""Small value objects. These Python contracts are teaching choices, not Codex APIs."""
from dataclasses import asdict, dataclass
from typing import Any

VERSION = "ch01-v1"


@dataclass(frozen=True)
class TaskInput:
    task_id: str
    text: str
    workspace_ref: str


@dataclass(frozen=True)
class RunLimits:
    max_steps: int
    deadline: float  # absolute time.monotonic(), never extended by a new step


@dataclass(frozen=True)
class CallContext:
    run_id: str
    turn_id: str
    sampling_id: str
    deadline: float
    call_id: str | None = None
    contract_version: str = VERSION


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class FixedPermit:
    run_id: str
    workspace_ref: str
    allowed: tuple[str, ...] = ("read", "search", "apply_patch", "exec")
    contract_version: str = VERSION


@dataclass(frozen=True)
class AuthorizedAction:
    action_id: str
    call_id: str
    operation: str
    arguments: dict[str, Any]
    workspace_ref: str
    scope: str = "workspace-only;network-none"


@dataclass(frozen=True)
class ExecutionLimits:
    deadline: float
    timeout: float = 8.0
    output_bytes: int = 32768


@dataclass
class CancelToken:
    cancelled: bool = False


@dataclass(frozen=True)
class ExecutionObservation:
    exit_code: int | None
    output: str
    truncated: bool
    process_status: str
    side_effect_status: str
    cleanup_status: str
    error_code: str | None = None


@dataclass(frozen=True)
class ToolObservation:
    call_id: str
    outcome: str
    content: dict[str, Any]
    side_effect_status: str
    cleanup_status: str
    error_code: str | None = None


@dataclass(frozen=True)
class ModelRequest:
    call_context: CallContext
    history_snapshot_id: str
    instructions: str
    messages: tuple[dict, ...]
    tool_specs: tuple[dict, ...]
    model_options: dict


@dataclass(frozen=True)
class ModelResult:
    request_id: str | None
    response_status: str
    output_items: tuple[dict, ...]
    usage: dict | None = None
    provider_error: str | None = None
    response_id: str | None = None  # provider response body id; not the HTTP request id
    usage_presence: str = "not_observable"  # missing/null/object/invalid at the raw JSON boundary


@dataclass(frozen=True)
class RunResult:
    run_id: str
    task_status: str
    stop_reason: str
    verification_ref: str | None
    cleanup_status: str
    steps: int
    mode: str


def data(value):
    return asdict(value)
