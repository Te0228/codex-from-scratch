"""A single-event-loop teaching prototype; not a Codex implementation or a Loop.

Methods without await are atomic with respect to this event loop. External
threads must post commands to that loop instead of calling these methods.
"""
import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TurnRef:
    run_id: str
    turn_id: str
    epoch: int


@dataclass(frozen=True)
class Input:
    input_id: str
    order: int
    text: str
    origin: str


@dataclass
class Turn:
    ref: TurnRef
    phase: str = "reserved"
    queue: list[Input] = field(default_factory=list)
    consumed: list[Input] = field(default_factory=list)
    participants: list[Any] = field(default_factory=list)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    finalizer: asyncio.Task | None = None


@dataclass
class Request:
    request_id: str
    owner: TurnRef
    future: asyncio.Future
    timer: asyncio.TimerHandle | None = None


class PendingRequests:
    """Owns request terminal transitions, timers, futures, and tombstones."""
    def __init__(self):
        self.active = {}
        self.terminal = {}
        self.rejected = []

    def request(self, request_id, owner, timeout=None):
        if request_id in self.active or request_id in self.terminal:
            raise ValueError("request_id cannot be reused")
        loop = asyncio.get_running_loop()
        req = Request(request_id, owner, loop.create_future())
        self.active[request_id] = req
        if timeout is not None:
            req.timer = loop.call_later(timeout, self.settle,
                                        request_id, owner, "timeout", None)
        return req

    def settle(self, request_id, owner, status, answer=None):
        if status not in {"answered", "rejected", "timeout", "cancelled", "closed"}:
            raise ValueError("invalid terminal")
        req = self.active.get(request_id)
        if req is None or req.owner != owner:
            self.rejected.append((request_id, owner, "missing_or_wrong_owner"))
            return False
        self.active.pop(request_id)
        result = {"owner": owner, "status": status,
                  "answer": answer if status == "answered" else None}
        self.terminal[request_id] = result
        if req.timer:
            req.timer.cancel()
        req.future.set_result(result)
        return True

    async def wait(self, request):
        # Cancelling one observer must not silently cancel the shared request.
        return await asyncio.shield(request.future)

    def close_turn(self, owner, status):
        for key, req in list(self.active.items()):
            if req.owner == owner:
                self.settle(key, owner, status)


class Scheduler:
    """Owns turn admission, input ownership, phase and the one terminal event.

No model, subprocess, journal or workspace is replaced by this prototype.
Participants supply only controlled cleanup for scheduling experiments.
"""
    def __init__(self, run_id="prototype"):
        self.run_id = run_id
        self.active = None
        self.next_epoch = 0
        self.next_order = 0
        self.last_started = None
        self.pending = PendingRequests()
        self.accepted = {}
        self.deferred = []
        self.unclaimed = {}
        self.terminal = {}
        self.events = []

    def _input(self, input_id, text, origin):
        if input_id in self.accepted:
            raise ValueError("duplicate input_id")
        self.next_order += 1
        item = Input(input_id, self.next_order, text, origin)
        self.accepted[input_id] = item
        self.events.append(("accepted", input_id, item.order))
        return item

    def start_if_idle(self, input_id, text, *, previous=None, origin="user"):
        if self.active is not None:
            return "not_idle", None
        if previous is not None and previous != self.last_started:
            return "superseded", None
        item = self._input(input_id, text, origin)
        self.next_epoch += 1
        ref = TurnRef(self.run_id, f"turn-{self.next_epoch}", self.next_epoch)
        self.active = Turn(ref, queue=[*self.deferred, item])
        self.deferred = []
        self.last_started = ref
        return "started", ref

    def start_or_steer(self, input_id, text, *, origin="user"):
        if self.active is None:
            return self.start_if_idle(input_id, text, origin=origin)
        item = self._input(input_id, text, origin)
        if self.active.phase in {"reserved", "running"}:
            self.active.queue.append(item)
            return "steered", self.active.ref
        # Teaching choice: explicit next-turn receipt, not automatic restart.
        self.deferred.append(item)
        return "deferred", None

    def steer(self, expected, input_id, text):
        if not self.active or self.active.ref != expected:
            return "wrong_turn"
        if self.active.phase not in {"reserved", "running"}:
            return "admission_closed"
        self.active.queue.append(self._input(input_id, text, "user"))
        return "steered"

    def attach_participant(self, ref, participant):
        turn = self._running_or_reserved(ref)
        turn.participants.append(participant)

    def _running_or_reserved(self, ref):
        if not self.active or self.active.ref != ref:
            raise ValueError("wrong_turn")
        if self.active.phase not in {"reserved", "running"}:
            raise ValueError("admission_closed")
        return self.active

    def claim(self, ref):
        turn = self._running_or_reserved(ref)
        turn.phase = "running"
        items, turn.queue = turn.queue, []
        turn.consumed.extend(items)
        return tuple(items)

    def question(self, ref, request_id, timeout=None):
        self._running_or_reserved(ref)
        return self.pending.request(request_id, ref, timeout)

    def answer(self, ref, request_id, answer):
        if not self.active or self.active.ref != ref or self.active.phase != "running":
            self.events.append(("late_answer", request_id, ref))
            return False
        return self.pending.settle(request_id, ref, "answered", answer)

    def observe_result(self, ref, result):
        accepted = bool(self.active and self.active.ref == ref
                        and self.active.phase == "running")
        self.events.append(("result" if accepted else "late_result", ref, result))
        return accepted

    def begin_finish(self, ref):
        turn = self._running_or_reserved(ref)
        # This check and closing admission must be one non-awaiting transition.
        if turn.queue:
            return False
        turn.phase = "finishing"
        return True

    async def finish(self, ref):
        turn = self.active
        if not turn or turn.ref != ref or turn.phase != "finishing":
            return False
        if turn.finalizer is None:
            turn.finalizer = asyncio.create_task(self._finalize(turn, "completed"))
        await asyncio.shield(turn.finalizer)
        return True

    async def interrupt(self, ref):
        turn = self.active
        if not turn or turn.ref != ref or ref in self.terminal:
            return False
        turn.cancel.set()
        turn.phase = "cancelling"
        # Reject answers immediately; release pending waiters before cleanup waits.
        self.pending.close_turn(ref, "cancelled")
        if turn.finalizer is None:
            turn.finalizer = asyncio.create_task(self._finalize(turn, "cancelled"))
        await asyncio.shield(turn.finalizer)
        return True

    async def _finalize(self, turn, requested):
        reports = []
        for participant in turn.participants:
            try:
                reports.append(await participant.close_and_join(turn.ref, turn.cancel))
            except Exception as error:
                reports.append({"cleanup": "unknown", "error": type(error).__name__})
        # Finalizer is sole owner of terminal publication. Interrupt may supersede
        # completion only before this non-awaiting commit point.
        status = "cancelled" if turn.cancel.is_set() else requested
        if any(report.get("cleanup") != "confirmed" for report in reports):
            status = "cleanup_unknown"
        self.pending.close_turn(turn.ref, "cancelled" if turn.cancel.is_set() else "closed")
        self.unclaimed[turn.ref] = tuple(turn.queue)
        turn.queue = []
        assert turn.ref not in self.terminal
        self.terminal[turn.ref] = {"status": status, "reports": reports,
                                   "consumed": tuple(turn.consumed)}
        self.events.append(("turn_terminal", turn.ref, status))
        if status != "cleanup_unknown":
            assert self.active is turn
            self.active = None
        else:
            turn.phase = "blocked_cleanup"  # resource uncertainty keeps the slot closed
