"""A tiny NDJSON transport fixture. It is NOT an SSE/Responses API adapter.

Unlike Codex's item-level dispatch, this exercise releases tools only after the
whole response completes. No model, network or tool side effect is performed.
"""
import codecs
from dataclasses import dataclass
import hashlib
import json


def wire(events):
    return b''.join((json.dumps(e, ensure_ascii=False) + '\n').encode() for e in events)


def chunks(data, width):
    if width <= 0:
        raise ValueError('width must be positive')
    return [data[i:i + width] for i in range(0, len(data), width)]


def fingerprint(request):
    return hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()


class Assembler:
    def __init__(self):
        self.fragments = {}
        self.ready = {}
        self.terminal = None
        self.usage = {'presence': 'missing', 'raw': None}

    def accept(self, event):
        if self.terminal is not None:
            raise ValueError('event_after_terminal')
        kind = event['type']
        if kind == 'delta':
            call = event['call_id']
            if call in self.ready:
                raise ValueError('delta_after_item_done')
            self.fragments[call] = self.fragments.get(call, '') + event['text']
        elif kind == 'item_done':
            call = event['call_id']
            if call in self.ready:
                raise ValueError('duplicate_item_done')
            args = json.loads(self.fragments.get(call, ''))
            if not isinstance(args, dict):
                raise ValueError('arguments_must_be_object')
            self.ready[call] = dict(call_id=call, name=event['name'], arguments=args)
        elif kind in {'completed', 'incomplete', 'cancelled'}:
            self.terminal = kind
            if 'usage' in event:
                value = event['usage']
                self.usage = dict(presence='null' if value is None else
                                  'object' if isinstance(value, dict) else 'invalid', raw=value)
            if kind == 'completed' and set(self.fragments) != set(self.ready):
                raise ValueError('completed_with_unfinished_item')
        else:
            raise ValueError('unknown_event')

    def outcome(self, error=None):
        status = error or self.terminal or 'missing_terminal'
        return dict(status=status, calls=list(self.ready.values()) if status == 'completed' else [],
                    ready_count=len(self.ready), terminal=self.terminal, usage=self.usage)


def consume(parts, *, cancel_after=None):
    decoder = codecs.getincrementaldecoder('utf-8')()
    assembler, buffer = Assembler(), ''
    try:
        for index, part in enumerate(parts):
            if cancel_after is not None and index >= cancel_after:
                return assembler.outcome('cancelled')
            buffer += decoder.decode(part)
            while '\n' in buffer:
                line, buffer = buffer.split('\n', 1)
                assembler.accept(json.loads(line))
        buffer += decoder.decode(b'', final=True)
        if buffer:
            return assembler.outcome('partial_frame')
    except (ValueError, UnicodeError, KeyError) as exc:
        result = assembler.outcome('protocol_error')
        result['error'] = str(exc)
        return result
    except ConnectionError as exc:
        result = assembler.outcome('transport_error')
        result['error'] = str(exc)
        return result
    return assembler.outcome()


def sample_fixture(request, attempts, *, max_retries=1):
    """Retries only before release; records logical identity separately from attempts."""
    reports = []
    immutable = json.loads(json.dumps(request))
    for index, parts in enumerate(attempts):
        if index > max_retries:
            break
        result = consume(parts)
        reports.append(dict(attempt_id=f"{immutable['request_id']}:a{index + 1}",
                            request_digest=fingerprint(immutable), outcome=result['status']))
        if result['status'] not in {'transport_error', 'missing_terminal', 'partial_frame'}:
            break
    if not reports:
        raise ValueError('at_least_one_attempt_required')
    return dict(result=result, attempts=reports)
