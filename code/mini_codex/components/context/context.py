"""Local context projection exercise, not Codex's compaction implementation.

Budget = UTF-8 text bytes, not tokenizer tokens or complete request size.
The strict revision rule deliberately rejects any append during compaction.
"""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from threading import RLock


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Rejected(ValueError):
    pass


@dataclass(frozen=True)
class Candidate:
    base_revision: int
    source_digest: str
    covered: tuple[str, ...]
    protected: tuple[str, ...]
    text: str


class History:
    def __init__(self):
        self._items = []
        self.revision = 0
        self.projection = None
        self._lock = RLock()

    def append(self, role, text, *, call_id=None, protected=False):
        with self._lock:
            self.revision += 1
            item = dict(id=f'e{self.revision}', role=role, text=text,
                        call_id=call_id, protected=protected)
            self._items.append(item)
            self.projection = None
            return item['id']

    def snapshot(self):
        with self._lock:
            return dict(revision=self.revision, items=deepcopy(self._items))

    def install(self, candidate, *, cancelled=False):
        with self._lock:
            if cancelled:
                raise Rejected('cancelled')
            if candidate.base_revision != self.revision:
                raise Rejected('stale_revision')
            if candidate.source_digest != digest(self._items):
                raise Rejected('source_mismatch')
            ids = [item['id'] for item in self._items]
            if not candidate.covered or list(candidate.covered) != ids[:len(candidate.covered)]:
                raise Rejected('summary_must_cover_existing_prefix')
            covered = self._items[:len(candidate.covered)]
            required = tuple(item['text'] for item in covered if item['protected'])
            if candidate.protected != required:
                raise Rejected('missing_or_changed_protected_text')
            calls = {i['call_id'] for i in covered if i['role'] == 'call'}
            results = {i['call_id'] for i in covered if i['role'] == 'result'}
            if calls != results:
                raise Rejected('summary_would_split_tool_pair')
            self.projection = candidate
            return candidate

    def view(self):
        with self._lock:
            if self.projection is None:
                return deepcopy(self._items)
            c = self.projection
            kept = [i for i in self._items[:len(c.covered)] if i['protected']]
            summary = dict(id='summary', role='summary', text=c.text, call_id=None,
                           protected=False, sources=c.covered)
            return deepcopy(kept + [summary] + self._items[len(c.covered):])


def propose(snapshot, text, count=None):
    items = snapshot['items']
    covered = items if count is None else items[:count]
    return Candidate(snapshot['revision'], digest(items),
                     tuple(i['id'] for i in covered),
                     tuple(i['text'] for i in covered if i['protected']), text)


def select(items, budget):
    """Select newest whole units; rules and pending calls are mandatory."""
    if budget < 0:
        raise ValueError('negative budget')
    units, by_call = [], {}
    for item in items:
        call = item.get('call_id')
        if call:
            if call not in by_call:
                by_call[call] = []
                units.append(by_call[call])
            by_call[call].append(item)
        else:
            units.append([item])
    required, optional = [], []
    for unit in units:
        roles = [i['role'] for i in unit]
        if 'result' in roles and ('call' not in roles or roles.count('call') != 1 or roles.count('result') != 1):
            raise Rejected('orphan_or_duplicate_tool_result')
        mandatory = any(i['protected'] for i in unit) or ('call' in roles and 'result' not in roles)
        (required if mandatory else optional).append(unit)
    size = lambda unit: sum(len(i['text'].encode()) for i in unit)
    used = sum(map(size, required))
    if used > budget:
        raise Rejected('mandatory_context_exceeds_budget')
    chosen = [*required]
    for unit in reversed(optional):
        if used + size(unit) <= budget:
            chosen.append(unit)
            used += size(unit)
    selected_ids = {i['id'] for unit in chosen for i in unit}
    return dict(items=deepcopy([i for i in items if i['id'] in selected_ids]),
                used_bytes=used, budget_bytes=budget,
                omitted=[i['id'] for i in items if i['id'] not in selected_ids])
