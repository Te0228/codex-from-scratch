"""Teaching event core only: no network, device, codec, Scheduler or task execution."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class Transcript:
    session_id: str
    attempt: int
    speaker: str
    utterance: str
    revision: int
    text: str
    final: bool


@dataclass(frozen=True)
class SubmissionCandidate:
    key: tuple[str, int, str, str]
    thread_id: str
    text: str


class TranscriptReducer:
    """Input text is a snapshot, not a delta. Finals are immutable per utterance.

    Candidates are pending proposals, never task receipts. The integrating adapter
    must deliver them to the existing Scheduler with stable idempotency keys.
    """

    def __init__(self, session_id: str, attempt: int, thread_id: str):
        self.session_id, self.attempt, self.thread_id = session_id, attempt, thread_id
        self.active = True
        self.captions: dict[tuple[str, str], Transcript] = {}
        self.candidates: dict[tuple[str, int, str, str], SubmissionCandidate] = {}

    def receive(self, event: Transcript) -> str:
        if not self.active:
            return "closed"
        if (event.session_id, event.attempt) != (self.session_id, self.attempt):
            return "stale_session"
        if event.speaker not in {"user", "assistant"} or not event.utterance:
            raise ValueError("invalid transcript identity")
        if event.revision < 0:
            raise ValueError("invalid revision")
        key = event.speaker, event.utterance
        old = self.captions.get(key)
        if old and old.final:
            if event.final and event.text != old.text:
                raise ValueError("conflicting final")
            return "duplicate_final" if event.final else "late_partial"
        if old and event.revision < old.revision:
            return "stale_revision"
        if old and event.revision == old.revision and not event.final:
            if event.text != old.text:
                raise ValueError("conflicting revision")
            return "duplicate_partial"
        self.captions[key] = event
        if event.final and event.speaker == "user" and event.text.strip():
            candidate_key = self.session_id, self.attempt, event.speaker, event.utterance
            self.candidates[candidate_key] = SubmissionCandidate(
                candidate_key, self.thread_id, event.text
            )
            return "candidate_pending"
        return "caption_updated"

    def close(self) -> None:
        self.active = False
        # Pending candidates remain inspectable. This prototype never auto-replays them.


class ChannelClosed(Exception):
    pass


class StaleEpoch(Exception):
    pass


class EpochChannel:
    """A bounded in-process media queue; interrupt invalidates queued/waiting writes.

    Uses a single condition so close and queue insertion have one linearization
    point. There is no physical audio output or remote cancellation in this type.
    """

    def __init__(self, capacity: int):
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.capacity, self.epoch = capacity, 0
        self.closed = False
        self._queue = deque()
        self._condition = asyncio.Condition()

    @property
    def size(self) -> int:
        return len(self._queue)

    async def put(self, epoch: int, payload: bytes) -> None:
        async with self._condition:
            await self._condition.wait_for(
                lambda: self.closed or epoch != self.epoch or self.size < self.capacity
            )
            if self.closed:
                raise ChannelClosed
            if epoch != self.epoch:
                raise StaleEpoch
            self._queue.append(payload)
            self._condition.notify_all()

    async def get(self) -> tuple[int, bytes]:
        async with self._condition:
            await self._condition.wait_for(lambda: self.closed or self.size > 0)
            if self.closed:
                raise ChannelClosed
            value = self._queue.popleft()
            self._condition.notify_all()
            return self.epoch, value

    async def interrupt(self) -> tuple[int, int]:
        async with self._condition:
            if self.closed:
                raise ChannelClosed
            discarded = self.size
            self.epoch += 1
            self._queue.clear()
            self._condition.notify_all()
            return self.epoch, discarded

    async def close(self) -> int:
        async with self._condition:
            discarded = self.size
            self.closed = True
            self._queue.clear()
            self._condition.notify_all()
            return discarded
