"""Teaching policy core. No shell, filesystem enforcement, model or alternate Loop.

Resource names are exact opaque IDs, not OS paths or executable identities.
The host adapter must canonicalize and enforce them before integrating this core.
"""
from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json


class Source(str, Enum):
    USER = "user"
    HOOK = "hook"
    GUARDIAN = "guardian"
    POLICY = "policy"


class Requirement(str, Enum):
    ALLOW = "allow"
    REVIEW = "review"
    HUMAN = "human_once"
    FORBID = "forbid"


@dataclass(frozen=True)
class Scope:
    read: frozenset[str] = frozenset()
    write: frozenset[str] = frozenset()
    network: frozenset[str] = frozenset()
    deny: frozenset[str] = frozenset()

    def covers(self, requested):
        return (requested.read <= self.read | self.write
                and requested.write <= self.write
                and requested.network <= self.network
                and not ((requested.read | requested.write) & self.deny))


@dataclass(frozen=True)
class Action:
    session: str
    call: str
    environment: str
    operation: str
    # Canonical JSON serialized by a trusted adapter, including cwd, tty and argv.
    arguments_json: str
    scope: Scope

    def content_key(self):
        canonical = json.dumps(json.loads(self.arguments_json), sort_keys=True,
                               ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(json.dumps(
            [self.environment, self.operation, canonical],
            ensure_ascii=False, separators=(",", ":")
        ).encode()).hexdigest()


@dataclass(frozen=True)
class Policy:
    version: str
    managed_deny: frozenset[str] = frozenset()
    strict_auto: bool = False


@dataclass(frozen=True)
class Permit:
    source: Source
    reviewer: str
    action_key: str
    session: str
    call: str | None
    policy_version: str
    scope: Scope
    # User/Hook retry allowance must be represented, never inferred from a bool.
    retry_scope: Scope | None = None


def route(requirement, policy, hook_decisions=()):
    """Returns a route, never treats a missing reviewer result as approval."""
    if requirement == Requirement.FORBID:
        return "deny"
    if requirement == Requirement.HUMAN and policy.strict_auto:
        return "incompatible_review_requirements"
    if "deny" in hook_decisions:
        return "deny"
    if requirement == Requirement.HUMAN:
        return "user"
    if "allow" in hook_decisions:
        return "hook"
    if requirement == Requirement.ALLOW and not policy.strict_auto:
        return "policy"
    return "reviewer"  # Guardian may decide, abstain to user, fail or be cancelled.


def issue(action, policy, requirement, source, reviewer, *, session_wide=False,
          retry_scope=None):
    if requirement == Requirement.FORBID:
        raise ValueError("forbidden")
    if requirement == Requirement.HUMAN:
        if source != Source.USER or session_wide or policy.strict_auto:
            raise ValueError("fresh human decision required")
    if retry_scope is not None and source not in (Source.USER, Source.HOOK):
        raise ValueError("retry scope requires user or hook authority")
    if (action.scope.read | action.scope.write) & policy.managed_deny:
        raise ValueError("managed deny")
    return Permit(source, reviewer, action.content_key(), action.session,
                  None if session_wide else action.call, policy.version,
                  action.scope, retry_scope)


def check(permit, actual, current_policy, supported, *, cancelled=False):
    """A matching decision is distinct from an enforceable execution scope."""
    if cancelled:
        return "cancelled"
    if (permit.session != actual.session or permit.policy_version != current_policy.version
            or permit.action_key != actual.content_key()
            or (permit.call is not None and permit.call != actual.call)
            or not permit.scope.covers(actual.scope)):
        return "mismatch"
    required_deny = current_policy.managed_deny | permit.scope.deny | actual.scope.deny
    if (actual.scope.read | actual.scope.write) & required_deny:
        return "denied"
    if not supported.covers(actual.scope) or not required_deny <= supported.deny:
        return "unsupported"
    return "matched"


@dataclass(frozen=True)
class RetryContext:
    failure_kind: str
    approval_policy: str
    tool_escalates: bool = True
    human_once: bool = False
    network_review: bool = False


def plan_retry(permit, actual, policy, supported, context, *, cancelled=False):
    """Returns stop / review / reuse with an explicit derived permit.

    OnRequest network approvals are left to the real network owner in ch09.
    This prototype models the non-network escalation route only.
    """
    if cancelled:
        return "stop:cancelled", None
    if context.failure_kind != "sandbox_denied" or not context.tool_escalates:
        return "stop:not_retryable", None
    if context.network_review:
        return "review:network_owner", None
    if context.approval_policy in {"never", "on_request"}:
        return "stop:approval_policy", None
    if context.approval_policy != "unless_trusted":
        return "stop:unsupported_policy", None
    # Approval may legitimately widen to an expressly represented retry ceiling.
    candidate = replace(permit, scope=permit.retry_scope or permit.scope)
    verdict = check(candidate, actual, policy, supported)
    if verdict == "unsupported":
        return "stop:unsupported", None
    if verdict == "denied":
        return "stop:managed_deny", None
    if (verdict == "matched" and not context.human_once and not policy.strict_auto
            and permit.source in (Source.USER, Source.HOOK)):
        return "reuse", candidate
    return "review:actual_retry", None


@dataclass(frozen=True)
class Pending:
    request_id: str
    action: Action
    policy_version: str
    state: str = "waiting"

    def cancel(self):
        return replace(self, state="cancelled")

    def resolve(self, request_id, actual, policy, source, reviewer):
        if (self.state != "waiting" or request_id != self.request_id
                or actual != self.action or self.policy_version != policy.version):
            return self, None
        permit = issue(actual, policy, Requirement.REVIEW, source, reviewer)
        return replace(self, state="resolved"), permit


@dataclass(frozen=True)
class ManagedRequirements:
    rules: frozenset[tuple[str, str]]
    network_rules: frozenset[tuple[str, str]]
    executable_ids: frozenset[tuple[str, str]]


def inherit(source_requirements, destination_requirements, snapshot,
            *, trusted_host, managed_only, has_local_rule_files):
    """Exact toy rule IDs; real prefix-shadow analysis belongs to Codex execpolicy."""
    if not trusted_host or not managed_only or has_local_rule_files:
        return False
    if source_requirements != destination_requirements:
        return False
    if source_requirements is None:
        return True
    # This toy format forbids changing a managed rule while permitting new rule IDs.
    return (source_requirements.rules <= snapshot.rules
            and source_requirements.network_rules <= snapshot.network_rules
            and source_requirements.executable_ids == snapshot.executable_ids
            and all(not any(key == k and value != v for key, value in snapshot.rules)
                    for k, v in source_requirements.rules))
