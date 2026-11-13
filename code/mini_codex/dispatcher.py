"""Sole dispatch owner: schema, captured action, permit, journal, and execution."""
import json
from pathlib import Path
from threading import RLock
import time

from .contracts import AuthorizedAction, ExecutionLimits, ToolObservation, VERSION, data
from .components.tools.toolbox import Registry
from .components.permissions.policy import Action, Scope, Policy, Requirement, Source, issue, check
from .components.sandbox.planner import Spec, Backend, compile_plan, Preparation

FIELDS = {
    'read': {'path': 'Workspace-relative UTF-8 file path.'},
    'search': {'query': 'Literal text to find in Python files under the workspace.'},
    'apply_patch': {'path': 'Workspace-relative existing file path.',
                    'old': 'Exact nonempty old text; it must occur exactly once.', 'new': 'Replacement text.'},
    'exec': {'command': 'Short /bin/sh command, cwd=/workspace, no network.'},
}
TOOL_SPECS = tuple({'type': 'function', 'name': name, 'description': {
    'read': 'Read a workspace file.', 'search': 'Search Python source with literal text.',
    'apply_patch': 'Atomically replace one exact text occurrence.',
    'exec': 'Run a bounded command in the isolated workspace.'}[name], 'strict': True,
    'parameters': {'type': 'object', 'properties': {key: {'type': 'string', 'description': desc}
    for key, desc in fields.items()}, 'required': list(fields), 'additionalProperties': False}}
    for name, fields in FIELDS.items())


class Dispatcher:
    def __init__(self, worker, history, *, lose_after_call=None, extensions=None, policy=None):
        self.worker, self.history, self.extensions = worker, history, extensions
        self.accepted, self.closed = {}, False
        self.lose_after_call = lose_after_call
        self.registry = Registry()
        self.policy = policy or Policy('fixed-workspace-v1')
        self._lock = RLock()
        self._current = None
        extra = tuple(extensions.tool_specs()) if extensions else ()
        self.tool_specs = TOOL_SPECS + extra
        self.allowed_names = tuple(spec['name'] for spec in self.tool_specs)
        for spec in self.tool_specs:
            name = spec['name']
            capability = ('workspace.write' if name in ('exec', 'apply_patch') else
                          'workspace.read' if name in FIELDS else 'extension.' + name)
            self.registry.register_spec(spec, capability,
                self._handler(name), version=VERSION)
        self.last_sandbox_plan = None

    def _handler(self, name):
        def invoke(**arguments):
            return self._execute(name, arguments)
        return invoke

    def _execute(self, name, args):
        context, action, cancel = self._current
        if name not in FIELDS:
            return {'extension': self.extensions.dispatch(name, args, context)}
        try:
            result = self.worker.execute(action, ExecutionLimits(context.deadline), cancel)
        except Exception as error:
            from .worker import SandboxUnavailable
            if isinstance(error, SandboxUnavailable):
                return {'unavailable': str(error)}
            raise
        return {'worker': data(result)}

    def dispatch(self, call, context, permit, cancel=None):
        with self._lock:
            return self._dispatch(call, context, permit, cancel)

    def _dispatch(self, call, context, permit, cancel):
        def reject(code):
            return ToolObservation(call.call_id, 'rejected', {}, 'none', 'confirmed', code)
        if self.closed:
            return reject('run_closed')
        if context.contract_version != VERSION or permit.contract_version != VERSION:
            return reject('contract_version')
        if (context.run_id != permit.run_id or context.run_id != self.history.run_id or
                context.call_id != call.call_id or not call.call_id):
            return reject('identity_mismatch')
        if Path(permit.workspace_ref).resolve() != self.worker.workspace:
            return reject('workspace_identity_mismatch')
        if call.call_id in self.accepted:
            self.history.append('duplicate_dispatch', {'call_id': call.call_id})
            return reject('duplicate_call')
        if time.monotonic() >= context.deadline:
            return reject('deadline')
        if cancel and cancel.cancelled:
            return reject('cancelled')
        if call.tool_name not in permit.allowed or call.tool_name not in self.allowed_names:
            return reject('unknown_or_denied_tool')
        try:
            captured = self.registry.prepare(call.call_id, call.tool_name, call.arguments)
        except (ValueError, TypeError):
            return reject('invalid_arguments')
        workspace = str(self.worker.workspace)
        resource = 'workspace' if call.tool_name in FIELDS else 'extension:' + call.tool_name
        writable = call.tool_name in ('exec', 'apply_patch') or call.tool_name not in FIELDS
        scope = Scope(write=frozenset({resource})) if writable else Scope(read=frozenset({resource}))
        permission_action = Action(context.run_id, call.call_id, workspace, call.tool_name,
                                   captured.arguments_json, scope)
        decision = issue(permission_action, self.policy, Requirement.ALLOW, Source.POLICY, 'fixed-local-policy')
        supported = Scope(read=frozenset({resource}), write=frozenset({resource}))
        if check(decision, permission_action, self.policy, supported,
                 cancelled=bool(cancel and cancel.cancelled)) != 'matched':
            return reject('permit_mismatch')
        registry_permit = self.registry.authorize(captured, {captured.capability})
        # Policy translation for the fixed backend, not live capability evidence.
        if call.tool_name in FIELDS:
            plan = compile_plan(Spec(self.policy.version, ('/workspace',), ('/workspace',), network='isolated'),
                                Backend('required-linux-bwrap', True, True))
            plan['capability_evidence'] = 'required_backend_contract; actual worker checks and enforces'
            if Preparation(plan).ready_for_executor(self.policy.version) != 'ready_for_real_executor':
                return reject('sandbox_plan_blocked')
            self.last_sandbox_plan = plan
        action = AuthorizedAction(f'{context.run_id}:{call.call_id}:a1', call.call_id,
                                  call.tool_name, json.loads(captured.arguments_json), workspace)
        operation_id = f'{context.run_id}:{call.call_id}'
        self.history.append('tool_intent', {'operation_id': operation_id, 'call_id': call.call_id,
            'context': data(context), 'action': data(action), 'action_digest': captured.digest,
            'policy_version': self.policy.version})
        self.accepted[call.call_id] = 'accepted'
        self.history.append('call_accepted', {'context': data(context), 'action': data(action), 'attempt': 1})
        self._current = (context, action, cancel)
        try:
            record = self.registry.dispatch(captured, registry_permit,
                                             cancelled=bool(cancel and cancel.cancelled))
        except ValueError as error:
            observation = ToolObservation(call.call_id, 'rejected', {}, 'none', 'confirmed', str(error))
        else:
            result = record.get('result', {})
            if record['execution'] == 'unknown':
                observation = ToolObservation(call.call_id, 'unknown', {'error': record.get('error')},
                                              'unknown', 'unknown', 'handler_exception')
            elif 'unavailable' in result:
                observation = ToolObservation(call.call_id, 'failed', result, 'none', 'confirmed', 'sandbox_unavailable')
            elif 'extension' in result:
                ext = result['extension']
                cleanup = 'confirmed' if ext.get('cleanup_status') in ('complete', 'confirmed') else 'unknown'
                effect = {'committed': 'confirmed'}.get(ext.get('side_effect_status'), ext.get('side_effect_status', 'unknown'))
                outcome = {'succeeded': 'ok', 'cancelled': 'failed'}.get(ext.get('outcome'), ext.get('outcome', 'unknown'))
                if cleanup != 'confirmed' or effect == 'unknown':
                    outcome = 'unknown'
                observation = ToolObservation(call.call_id, outcome, ext, effect, cleanup, ext.get('error'))
            else:
                execution = result['worker']
                if call.call_id == self.lose_after_call:
                    observation = ToolObservation(call.call_id, 'unknown',
                        {'last_known_phase': 'handler_entered', 'injected': 'reply_lost_after_execution'},
                        'unknown', execution['cleanup_status'], 'observation_lost')
                else:
                    outcome = ('unknown' if execution['side_effect_status'] == 'unknown' or execution['cleanup_status'] != 'confirmed'
                               else 'ok' if execution['exit_code'] == 0 else 'failed')
                    observation = ToolObservation(call.call_id, outcome, execution, execution['side_effect_status'],
                                                  execution['cleanup_status'], execution['error_code'])
        finally:
            self._current = None
        self.accepted[call.call_id] = observation.outcome
        if observation.outcome != 'unknown' and observation.cleanup_status == 'confirmed':
            self.history.append('tool_result', {'operation_id': operation_id, 'call_id': call.call_id,
                                                'observation': data(observation)})
        return observation

    def close(self):
        with self._lock:
            self.closed = True
