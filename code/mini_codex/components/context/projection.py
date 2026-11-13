"""Runtime request projection using exact local JSON byte budgets, not token estimates."""
from dataclasses import dataclass
import copy
import hashlib
import json


class ContextBudgetExceeded(ValueError):
    pass


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode()


@dataclass(frozen=True)
class Projection:
    snapshot_id: str
    raw_items: tuple
    report: dict


class ContextProjector:
    """Keep protected inputs and whole model/tool groups; select newest optional units."""
    def __init__(self, budget):
        if not isinstance(budget, int) or budget <= 0:
            raise ValueError('context budget must be a positive byte count')
        self.budget = budget
        self.last_report = None

    def project(self, snapshot, instructions, tool_specs):
        items, sources = snapshot.raw_items, snapshot.sources
        if not sources:
            sources = tuple({'event_id': str(i), 'kind': 'raw'} for i in range(len(items)))
        parents = list(range(len(items)))
        def root(i):
            while parents[i] != i:
                parents[i] = parents[parents[i]]
                i = parents[i]
            return i
        def union(a, b):
            parents[root(b)] = root(a)
        by_event, by_call, calls, outputs = {}, {}, {}, {}
        for index, (item, source) in enumerate(zip(items, sources)):
            event = source['event_id']
            if event in by_event:
                union(index, by_event[event])
            by_event[event] = index
            call_id = item.get('call_id')
            if call_id and item.get('type') in ('function_call', 'function_call_output'):
                # Same call ID in distinct runs is a distinct operation.
                key = (source.get('run_id'), call_id)
                if key in by_call:
                    union(index, by_call[key])
                by_call[key] = index
                (calls if item['type'] == 'function_call' else outputs).setdefault(key, []).append(index)
        if set(outputs) - set(calls):
            raise ContextBudgetExceeded('orphan_tool_output_in_history')
        groups = {}
        for index in range(len(items)):
            groups.setdefault(root(index), []).append(index)
        protected_kinds = {'task_input', 'verification_feedback'}
        pending = {index for key, indices in calls.items() if key not in outputs for index in indices}
        tool_indices = [i for i, item in enumerate(items)
                        if item.get('type') in ('function_call', 'function_call_output')]
        latest_tool_group = root(max(tool_indices)) if tool_indices else None
        mandatory, optional = [], []
        for group in sorted(groups.values(), key=max):
            required = (root(group[0]) == latest_tool_group or
                        any(sources[i]['kind'] in protected_kinds or i in pending for i in group))
            (mandatory if required else optional).append(group)
        selected = {i for group in mandatory for i in group}
        def request_size(indices):
            return len(encoded({'instructions': instructions, 'messages': [item for i, item in enumerate(items) if i in indices],
                                'tools': list(tool_specs)}))
        if request_size(selected) > self.budget:
            raise ContextBudgetExceeded('mandatory_context_exceeds_budget')
        for group in reversed(optional):
            trial = selected | set(group)
            if request_size(trial) <= self.budget:
                selected = trial
        projected = tuple(copy.deepcopy(item) for index, item in enumerate(items) if index in selected)
        report = {'source_snapshot_id': snapshot.snapshot_id, 'source_revision': snapshot.revision,
                  'budget_bytes': self.budget, 'used_bytes': request_size(selected),
                  'selected_indices': sorted(selected), 'omitted_indices': [i for i in range(len(items)) if i not in selected],
                  'budget_kind': 'local_json_bytes_not_provider_tokens'}
        self.last_report = report
        return Projection(hashlib.sha256(encoded(projected)).hexdigest(), projected, report)
