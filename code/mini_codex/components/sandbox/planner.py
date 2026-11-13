"""Pure policy planning. No OS enforcement, spawning, probing, or network I/O.

Backend capabilities below are supplied facts for controlled examples; they are
not a live capability report. A 'planned' result never means a sandbox ran.
"""
from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True)
class Backend:
    name: str
    mount_rules: bool
    network_namespace: bool
    managed_egress: bool = False


@dataclass(frozen=True)
class Spec:
    revision: str
    read_roots: tuple[str, ...]
    write_roots: tuple[str, ...]
    deny_roots: tuple[str, ...] = ()
    network: str = 'isolated'
    domains: tuple[str, ...] = ()


def valid_root(root):
    p = PurePosixPath(root)
    return p.is_absolute() and '..' not in p.parts and str(p) == root


def compile_plan(spec, backend, supplied_environment=None):
    """Validate representability; represent narrow rules after their parents."""
    roots = spec.read_roots + spec.write_roots + spec.deny_roots
    if any(not valid_root(root) for root in roots):
        return dict(status='blocked', reason='noncanonical_root')
    if spec.network not in {'isolated', 'managed'}:
        return dict(status='blocked', reason='unsupported_network_mode')
    if not backend.mount_rules:
        return dict(status='blocked', reason='filesystem_backend_unavailable')
    if not backend.network_namespace:
        return dict(status='blocked', reason='network_boundary_unavailable')
    if spec.network == 'managed' and (not backend.managed_egress or not spec.domains):
        return dict(status='blocked', reason='managed_egress_unavailable')
    rules = ([(p, 'read') for p in spec.read_roots] +
             [(p, 'write') for p in spec.write_roots] +
             [(p, 'deny') for p in spec.deny_roots])
    # More specific path first wins in decision(); plan order exposes deny last.
    rules.sort(key=lambda rule: (len(PurePosixPath(rule[0]).parts), {'read': 0, 'write': 1, 'deny': 2}[rule[1]]))
    supplied = supplied_environment or {}
    env = {key: supplied[key] for key in ('LANG', 'TERM') if key in supplied}
    return dict(status='planned', enforced=False, backend=backend.name,
                policy_revision=spec.revision, rules=rules, network=spec.network,
                domains=list(spec.domains), environment=env)


def decision(plan, path, access):
    """A lexical rule explanation only. Does not resolve symlinks or open files."""
    if plan['status'] != 'planned' or not valid_root(path) or access not in {'read', 'write'}:
        return 'deny'
    candidate = PurePosixPath(path)
    if any(mode == 'deny' and candidate.is_relative_to(PurePosixPath(root))
           for root, mode in plan['rules']):
        return 'deny'
    for root, mode in reversed(plan['rules']):
        if candidate.is_relative_to(PurePosixPath(root)):
            return 'allow' if mode == 'write' or mode == access else 'deny'
    return 'deny'


@dataclass
class Preparation:
    plan: dict
    cancelled: bool = False
    consumed: bool = False

    def cancel(self):
        self.cancelled = True

    def ready_for_executor(self, current_revision):
        if self.cancelled:
            return 'cancelled'
        if self.consumed:
            return 'already_consumed'
        if self.plan['status'] != 'planned':
            return 'blocked'
        if current_revision != self.plan['policy_revision']:
            return 'stale_policy'
        self.consumed = True
        return 'ready_for_real_executor'
