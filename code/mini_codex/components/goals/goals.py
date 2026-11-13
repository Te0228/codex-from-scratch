"""Chapter 12 pure decision prototype. No model, scheduler, network, or storage IO.

Immutable snapshots make transitions testable. This is a teaching design, not a
port of Codex. The host must serialize goal mutations and scheduler admission;
the functions here do not provide that lock or crash-safe persistence.
"""
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Plan:
    revision: int = 0
    steps: tuple[tuple[str, str], ...] = ()


def update_plan(plan, steps, *, enabled=True):
    if not enabled:
        raise ValueError("plan_tool_disabled")
    if any(not text.strip() or status not in {"pending", "in_progress", "completed"}
           for text, status in steps):
        raise ValueError("invalid_plan")
    if sum(status == "in_progress" for _, status in steps) > 1:
        raise ValueError("multiple_in_progress_steps")
    return Plan(plan.revision + 1, tuple(steps))


@dataclass(frozen=True)
class Goal:
    goal_id: str
    objective: str
    revision: int = 1
    status: str = "active"
    limit: int | None = None  # None means explicitly unbounded, never unknown.
    used: int = 0
    # (producer ID, sequence, cumulative input, cached input, output).
    # Producer ID includes request/turn identity and belongs to this goal only.
    highwater: tuple[tuple[str, int, int, int, int], ...] = ()
    empty_turns: int = 0
    ended_turns: tuple[str, ...] = ()
    admitted: tuple[str, ...] = ()
    reason: str | None = None


def set_goal(goal_id, objective, *, authority, limit=None):
    # authority is a host-derived fact; never accept it from model tool arguments.
    if authority not in {"user", "system", "developer"}:
        raise ValueError("goal_not_authorized")
    if not goal_id or not objective.strip() or (limit is not None and limit <= 0):
        raise ValueError("invalid_goal")
    return Goal(goal_id, objective, limit=limit)


@dataclass(frozen=True)
class Usage:
    goal_id: str
    producer: str
    sequence: int
    input: int
    cached: int
    output: int


def account(goal, fact):
    """Apply a known cumulative fact once; facts never contain eligibility."""
    if fact.goal_id != goal.goal_id:
        raise ValueError("wrong_goal")
    if not fact.producer or min(fact.sequence, fact.input, fact.cached, fact.output) < 0:
        raise ValueError("invalid_usage")
    if fact.cached > fact.input:
        raise ValueError("cached_exceeds_input")
    rows = {row[0]: row[1:] for row in goal.highwater}
    previous = rows.get(fact.producer)
    now = (fact.sequence, fact.input, fact.cached, fact.output)
    if previous is not None:
        if fact.sequence < previous[0]:
            return goal  # a delayed older cumulative observation
        if fact.sequence == previous[0]:
            if now != previous:
                raise ValueError("conflicting_duplicate")
            return goal
        old_input, old_cached, old_output = previous[1:]
    else:
        old_input = old_cached = old_output = 0
    if any(a < b for a, b in zip(now[1:], (old_input, old_cached, old_output))):
        raise ValueError("usage_counter_regressed")
    delta = (fact.input - old_input) - (fact.cached - old_cached) + (fact.output - old_output)
    if delta < 0:
        raise ValueError("negative_goal_delta")
    rows[fact.producer] = now
    used = goal.used + delta
    status, reason = goal.status, goal.reason
    if status == "active" and goal.limit is not None and used >= goal.limit:
        status, reason = "budget_limited", "token_budget"
    return replace(goal, revision=goal.revision + 1, used=used, status=status,
                   reason=reason, highwater=tuple((key, *value) for key, value in sorted(rows.items())))


def pause(goal, *, explicit_user_request):
    if not explicit_user_request:
        raise ValueError("pause_not_authorized")
    if goal.status != "active":
        return goal
    return replace(goal, status="paused", reason="user_pause", revision=goal.revision + 1)


def complete(goal, *, verified_snapshot):
    # The integrated host resolves this trusted verifier reference, not the model.
    if not verified_snapshot:
        raise ValueError("completion_not_verified")
    if goal.status != "active":
        raise ValueError("goal_not_active")
    return replace(goal, status="complete", reason="verified_completion", revision=goal.revision + 1)


def turn_ended(goal, turn_id, *, automatic, had_activity, empty_final, error=None):
    if turn_id in goal.ended_turns:
        return goal
    count = goal.empty_turns + 1 if automatic and not had_activity and empty_final else 0
    status, reason = goal.status, goal.reason
    if status == "active":
        if error:
            status, reason = ("usage_limited" if error == "usage_limit" else "blocked"), error
        elif count >= 3:
            status, reason = "blocked", "three_empty_automatic_turns"
    return replace(goal, revision=goal.revision + 1, status=status, reason=reason,
                   empty_turns=count, ended_turns=goal.ended_turns + (turn_id,))


@dataclass(frozen=True)
class IdleFacts:
    event_id: str
    scheduler_epoch: int
    idle: bool = True
    pending_user_input: bool = False
    plan_mode: bool = False
    deferred: bool = False
    recovery_confirmed: bool = True
    budget_known: bool = True


@dataclass(frozen=True)
class Candidate:
    goal_id: str
    revision: int
    event_id: str
    scheduler_epoch: int


@dataclass(frozen=True)
class Decision:
    eligible: bool
    reason: str
    candidate: Candidate | None = None


def qualify_continue(goal, facts):
    if goal is None:
        return Decision(False, "no_goal")
    if goal.status != "active":
        return Decision(False, goal.status)
    for denied, reason in (
        (not facts.recovery_confirmed, "recovery_unconfirmed"),
        (not facts.budget_known, "budget_unknown"),
        (facts.deferred, "deferred"),
        (facts.plan_mode, "plan_mode"),
        (facts.pending_user_input, "pending_user_input"),
        (not facts.idle, "not_idle"),
        (facts.event_id in goal.admitted, "duplicate_idle_event"),
    ):
        if denied:
            return Decision(False, reason)
    if goal.limit is not None and goal.used >= goal.limit:
        return Decision(False, "budget_limited")
    return Decision(True, "eligible", Candidate(goal.goal_id, goal.revision,
                                                facts.event_id, facts.scheduler_epoch))


def revalidate(goal, candidate, current):
    """Run under host permit immediately before calling existing StartIfIdle."""
    decision = qualify_continue(goal, current)
    return decision.eligible and decision.candidate == candidate


def record_started(goal, candidate):
    """Host calls only after Scheduler Started, within that same permit.

    This records a receipt; it is not a scheduler and cannot establish that any
    turn actually started. Rejected submissions must never call this function.
    """
    if (goal.status != "active" or goal.goal_id != candidate.goal_id or
            goal.revision != candidate.revision or candidate.event_id in goal.admitted):
        raise ValueError("stale_admission_receipt")
    return replace(goal, revision=goal.revision + 1,
                   admitted=goal.admitted + (candidate.event_id,))
