"""Local registration/authorization/dispatch exercise; no OS sandbox guarantee.

The registry is a single-threaded in-memory owner. Its ledger is not durable.
Permit issuance is a trusted function in this exercise, not a security boundary
against arbitrary Python running in the same interpreter.
"""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


@dataclass(frozen=True)
class Action:
    call_id: str
    name: str
    arguments_json: str
    capability: str
    version: str

    @property
    def digest(self):
        return hashlib.sha256(canonical([self.name, self.arguments_json, self.capability, self.version]).encode()).hexdigest()


@dataclass(frozen=True)
class Permit:
    call_id: str
    digest: str


class Registry:
    def __init__(self):
        self.tools, self.ledger = {}, {}
        self.handler_entries = 0
        self.json_schemas = {}

    def register(self, name, schema, capability, handler, *, version='1'):
        if name in self.tools:
            raise ValueError('duplicate_tool_name')
        self.tools[name] = (dict(schema), capability, handler, version)

    def register_spec(self, spec, capability, handler, *, version='1'):
        """Supported JSON schema subset; unsupported types fail closed."""
        name, schema = spec['name'], deepcopy(spec['parameters'])
        if schema.get('type') != 'object':
            raise ValueError('tool_parameters_must_be_object')
        self.register(name, {}, capability, handler, version=version)
        self.json_schemas[name] = schema

    def prepare(self, call_id, name, arguments, *, rewrite=None):
        if name not in self.tools:
            raise ValueError('unknown_tool')
        schema, capability, _, version = self.tools[name]
        args = deepcopy(arguments)
        if rewrite:
            args = rewrite(args)
        if name in self.json_schemas:
            validate_json(self.json_schemas[name], args)
        else:
            if not isinstance(args, dict) or set(args) != set(schema):
                raise ValueError('argument_keys_mismatch')
            if any(type(args[key]) is not expected for key, expected in schema.items()):
                raise ValueError('argument_type_mismatch')
        return Action(call_id, name, canonical(args), capability, version)

    def authorize(self, action, allowed):
        if action.capability not in allowed:
            raise PermissionError('capability_not_granted')
        return Permit(action.call_id, action.digest)

    def dispatch(self, action, permit, *, cancelled=False, post_check=None):
        if cancelled:
            raise ValueError('cancelled_before_admission')
        if (permit.call_id, permit.digest) != (action.call_id, action.digest):
            raise PermissionError('permit_action_mismatch')
        if action.call_id in self.ledger:
            previous = self.ledger[action.call_id]
            if previous['digest'] != action.digest:
                raise ValueError('call_id_reused_for_different_action')
            return deepcopy(previous)
        schema, capability, handler, version = self.tools[action.name]
        if (version, capability) != (action.version, action.capability):
            raise ValueError('tool_contract_changed')
        record = dict(digest=action.digest, execution='accepted', observation='pending')
        self.ledger[action.call_id] = record
        self.handler_entries += 1
        try:
            record['result'] = handler(**json.loads(action.arguments_json))
            record['execution'] = 'succeeded'
        except Exception as exc:
            record.update(execution='unknown', error=type(exc).__name__ + ': ' + str(exc))
        record['observation'] = ('rejected' if post_check and not post_check(deepcopy(record))
                                 else 'accepted')
        return deepcopy(record)


def validate_json(schema, value, depth=0):
    """Bounded subset for shipped tools, not a general JSON Schema engine."""
    if depth > 16:
        raise ValueError('argument_nesting_limit')
    kind = schema.get('type')
    if isinstance(kind, list):
        for candidate in kind:
            try:
                validate_json({**schema, 'type': candidate}, value, depth + 1)
                return
            except ValueError:
                pass
        raise ValueError('argument_type_mismatch')
    types = {'string': str, 'integer': int, 'number': (int, float), 'boolean': bool,
             'array': list, 'object': dict, 'null': type(None)}
    if kind is not None:
        if kind not in types:
            raise ValueError('unsupported_schema_type')
        if not isinstance(value, types[kind]) or (kind in {'integer', 'number'} and isinstance(value, bool)):
            raise ValueError('argument_type_mismatch')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError('argument_enum_mismatch')
    if isinstance(value, str) and len(value) > min(schema.get('maxLength', 24000), 24000):
        raise ValueError('argument_string_limit')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema.get('minimum', float('-inf')) or value > schema.get('maximum', float('inf')):
            raise ValueError('argument_number_range')
    if isinstance(value, list):
        if len(value) > min(schema.get('maxItems', 256), 256):
            raise ValueError('argument_array_limit')
        for item in value:
            validate_json(schema.get('items', {}), item, depth + 1)
    if isinstance(value, dict):
        properties = schema.get('properties', {})
        if not set(schema.get('required', ())) <= set(value):
            raise ValueError('missing_required_argument')
        if schema.get('additionalProperties') is False and set(value) - set(properties):
            raise ValueError('unknown_argument')
        for name, item in value.items():
            validate_json(properties.get(name, {}), item, depth + 1)
