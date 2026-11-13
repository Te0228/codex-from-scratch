"""One history owner; optional SQLite journal is authoritative, JSONL diagnostic."""
import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from .components.sessions.journal import Journal


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class HistorySnapshot:
    snapshot_id: str
    raw_items: tuple[dict, ...]  # deep copied: callers cannot mutate History
    revision: int = 0
    sources: tuple[dict, ...] = ()


class History:
    def __init__(self, run_id: str, path: Path | None = None, secrets=(), *, journal_path=None):
        self.run_id, self.path, self.secrets = run_id, path, secrets
        self._events, self._items, self._sources = [], [], []
        self._lock = RLock()
        self.journal = Journal(Path(journal_path), check_same_thread=False) if journal_path else None
        self.revision = 0
        if self.journal:
            for event in self.journal.replay():
                stored = event['payload']
                self._accept(event['event_id'], event['kind'], stored['data'],
                             event['items'], stored['run_id'])

    def _accept(self, event_id, kind, payload, items, run_id):
        self._events.append({'event_id': event_id, 'sequence': len(self._events) + 1,
                             'run_id': run_id, 'kind': kind, 'payload': copy.deepcopy(payload)})
        self._items.extend(copy.deepcopy(items))
        self._sources.extend({'event_id': event_id, 'kind': kind, 'run_id': run_id} for _ in items)
        if items:
            self.revision += 1

    def _safe(self, value):
        text = canonical(value)
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, '[REDACTED]')
        return json.loads(text)

    def append(self, kind: str, payload: dict, items=()):
        with self._lock:
            payload, items = self._safe(payload), self._safe(list(items))
            if self.journal:
                event_id = self.journal.append(kind, {'run_id': self.run_id, 'data': payload}, items)
                # Commit before publishing to readers or entering any side effect.
                self.journal.flush()
            else:
                event_id = f"{self.run_id}:e{len(self._events) + 1}"
            self._accept(event_id, kind, payload, items, self.run_id)
            if self.path:
                with self.path.open('a', encoding='utf-8') as stream:
                    stream.write(canonical(self._events[-1]) + '\n')
            return event_id

    def snapshot(self):
        with self._lock:
            return HistorySnapshot(digest(self._items), tuple(copy.deepcopy(self._items)),
                                   self.revision, tuple(copy.deepcopy(self._sources)))

    @property
    def events(self):
        with self._lock:
            return tuple(copy.deepcopy(self._events))

    def unresolved_actions(self):
        """Read-only recovery; never re-enters a handler."""
        with self._lock:
            finished = {e['payload']['operation_id'] for e in self._events
                        if e['kind'] == 'tool_result' and 'operation_id' in e['payload']}
            return [copy.deepcopy(e['payload']) for e in self._events
                    if e['kind'] == 'tool_intent' and e['payload']['operation_id'] not in finished]

    def close(self):
        with self._lock:
            if self.journal:
                self.journal.close()
                self.journal = None
