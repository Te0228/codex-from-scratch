#!/usr/bin/env python3
"""Run Mini Codex in an immutable Linux image and retain actual evidence."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent
MODEL_ENV = ('OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_MODEL', 'OPENAI_REASONING_EFFORT')
INSPECT_FORMAT = ('{"id":{{json .Id}},"image":{{json .Image}},"state":{{json .State}},'
                  '"run_id":{{json (index .Config.Labels "codex.book.run_id")}}}')


def source_hash(folder):
    files = {}
    for path in sorted(folder.rglob('*')):
        if any(x in ('__pycache__', '.tmp', '.venv', 'build', 'dist') or x.endswith('.egg-info')
               for x in path.parts):
            continue
        if path.name.startswith('.env') or not path.is_file():
            continue
        if path.is_symlink():
            raise ValueError('checkpoint source must not contain symlinks')
        files[str(path.relative_to(folder))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def load_environment(path):
    """Read assignments as data; never source a shell file or expand commands."""
    env = os.environ.copy()
    if path is not None:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            key, separator, value = line.removeprefix('export ').partition('=')
            if not separator or key.strip() not in MODEL_ENV:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            env[key.strip()] = value
    return env


def validate_environment_path(path):
    """Reject credentials anywhere in COPY's checkpoint tree before reading/building.

    Check both the spelled path and its resolved target: an outside alias into the
    tree and an inside symlink out of the tree must both be rejected.
    """
    if path is None:
        return None
    candidate = path.expanduser().absolute()
    resolved = candidate.resolve(strict=True)
    tree = ROOT
    if candidate.is_relative_to(tree.absolute()) or resolved.is_relative_to(tree.resolve()):
        raise ValueError('--env-file must be outside the entire code tree, including symlinks')
    if not resolved.is_file():
        raise ValueError('--env-file must name a regular file')
    return resolved


def observe_container(call, target):
    observed = call(['docker', 'inspect', target, '--format', INSPECT_FORMAT], timeout=10)
    value = json.loads(observed.stdout or 'null')
    if (observed.returncode or not isinstance(value, dict) or not isinstance(value.get('state'), dict)
            or not isinstance(value.get('id'), str) or not value['id']):
        raise RuntimeError('cannot observe the run container')
    return value


def finish_container(call, record, run_dir):
    """One finalizer for normal completion, start/inspect errors, timeout and cancel.

    A successful create response identifies our container even when inspect is
    unavailable. An uncertain create requires a matching run label before kill.
    Never remove evidence resources while the process state is unknown.
    """
    target = record.get('container_id') or record['container']
    record['cleanup_attempted'] = True
    record['cleanup_status'] = 'unknown'
    record['termination_status'] = 'unknown'
    record['resources_retained'] = [record['container'], record['volume']]
    observed = None
    try:
        observed = observe_container(call, target)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        record['cleanup_initial_observation_error'] = str(exc)
    owned = bool(record.get('container_id'))
    if observed is not None:
        record['cleanup_initial_observation'] = observed
        owned = (observed.get('run_id') == record['run_id'] and
                 (not record.get('container_id') or observed.get('id') == record['container_id']))
    if not owned:
        record['cleanup_error'] = 'run container ownership not confirmed; no kill or removal attempted'
        return
    if observed is not None:
        target = observed['id']
        record.setdefault('container_id', target)
    if observed is None or observed['state'].get('Running') or observed['state'].get('Status') in ('paused', 'restarting'):
        try:
            stopped = call(['docker', 'kill', target], timeout=15)
            record['kill_exit_code'] = stopped.returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            record['kill_error'] = str(exc)
        # A kill CLI result alone never proves that the daemon stopped the process.
        try:
            observed = observe_container(call, target)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            record['cleanup_error'] = 'termination observation unavailable: ' + str(exc)
            return
    state = observed['state']
    record['container_state'] = state
    if (observed.get('run_id') != record['run_id'] or
            (record.get('container_id') and observed.get('id') != record['container_id']) or
            state.get('Running') is not False or state.get('Status') not in ('exited', 'created', 'dead')):
        record['cleanup_error'] = 'run container termination not confirmed; resources retained'
        return
    record['termination_status'] = 'confirmed'
    record['container_image_id'] = observed.get('image')
    try:
        logs = call(['docker', 'logs', target], timeout=30)
        (run_dir / 'container.log').write_text(logs.stdout + logs.stderr)
        record['logs_exit_code'] = logs.returncode
        exported = call(['docker', 'cp', target + ':/runs/.', str(run_dir)], timeout=60)
        record['artifact_export_exit_code'] = exported.returncode
        if exported.returncode:
            record['cleanup_status'] = 'retained'
            record['cleanup_error'] = 'artifact export failed; stopped container and volume retained'
            return
        removed = call(['docker', 'rm', target], timeout=30)
        record['container_removed'] = removed.returncode == 0
        if removed.returncode:
            record['cleanup_status'] = 'retained'
            record['cleanup_error'] = 'container removal unconfirmed; result volume retained'
            return
        record['resources_retained'] = [record['volume']]
        volume_removed = call(['docker', 'volume', 'rm', record['volume']], timeout=30)
        record['volume_removed'] = volume_removed.returncode == 0
        if volume_removed.returncode:
            record['cleanup_status'] = 'retained'
            record['cleanup_error'] = 'result volume removal unconfirmed'
            return
        record['resources_retained'] = []
        record['cleanup_status'] = 'confirmed'
    except (OSError, subprocess.TimeoutExpired) as exc:
        record['cleanup_status'] = 'retained'
        record['cleanup_error'] = 'artifact export or resource removal interrupted: ' + str(exc)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('doctor', 'replay', 'live', 'test'))
    parser.add_argument('--workspace', type=Path)
    parser.add_argument('--prompt')
    parser.add_argument('--verify-command')
    parser.add_argument('--env-file', type=Path, help='optional local configuration; never printed')
    parser.add_argument('--timeout', type=float, default=300, help='outer container deadline in seconds')
    args = parser.parse_args()
    checkpoint = ROOT
    if not (checkpoint / 'mini_codex/__main__.py').is_file():
        parser.error('final package entry point is unavailable')
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    run_id = f'agent-{args.action}-{uuid.uuid4().hex[:10]}'
    run_dir = ROOT.parent / 'runs' / run_id
    run_dir.mkdir(parents=True)
    record = {'run_id': run_id, 'package': 'mini_codex', 'action': args.action,
              'status': 'started', 'run_dir': str(run_dir), 'live_model_requested': args.action == 'live'}
    container, volume = 'codex-book-' + run_id, 'codex-book-' + run_id + '-results'
    record.update(container=container, volume=volume, cleanup_status='not_needed')
    container_attempted = False
    volume_attempted = False

    def save():
        (run_dir / 'runner.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')

    def call(command, *, timeout=30, env=None):
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=env)

    try:
        env_path = validate_environment_path(args.env_file)
        env = load_environment(env_path)
        if not shutil.which('docker'):
            raise RuntimeError('Docker CLI is unavailable')
        ready = call(['docker', 'version', '--format', '{{json .Server}}'], timeout=10)
        server = json.loads(ready.stdout or 'null')
        if ready.returncode or not server or not server.get('Version'):
            raise RuntimeError('Docker engine is unavailable')
        record['docker_server'] = {k: server.get(k) for k in ('Version', 'Os', 'Arch', 'KernelVersion')}
        record['model_configuration_present'] = {key: bool(env.get(key)) for key in MODEL_ENV}
        if args.action == 'live' and not all(env.get(k) for k in ('OPENAI_API_KEY', 'OPENAI_MODEL')):
            raise RuntimeError('live requires OPENAI_API_KEY and OPENAI_MODEL; use a local --env-file')
        record['checkpoint_source_sha256'] = source_hash(checkpoint)
        image = 'codex-from-scratch:runtime'
        print(f'Building Mini Codex; evidence: {run_dir}', flush=True)
        with (run_dir / 'build.log').open('w') as log:
            build = subprocess.run(['docker', 'build', '--progress=plain', '--tag', image, str(ROOT)],
                                   stdout=log, stderr=subprocess.STDOUT, timeout=600)
        record['build_exit_code'] = build.returncode
        if build.returncode:
            raise RuntimeError('image build failed; see build.log')
        if source_hash(checkpoint) != record['checkpoint_source_sha256']:
            raise RuntimeError('code changed during build; rerun after edits finish')
        identity = call(['docker', 'image', 'inspect', image, '--format', '{{.Id}}'])
        if identity.returncode or not identity.stdout.strip().startswith('sha256:'):
            raise RuntimeError('cannot identify the built image')
        record['image_id'] = identity.stdout.strip()
        volume_attempted = True
        made = call(['docker', 'volume', 'create', '--label', 'codex.book.run_id=' + run_id, volume])
        if made.returncode:
            raise RuntimeError('cannot create the run result volume')
        command = ['docker', 'create', '--name', container, '--label', 'codex.book.run_id=' + run_id,
                   '--cap-add', 'SYS_ADMIN',
                   '--security-opt', 'seccomp=unconfined',
                   # Docker's masked /proc paths prevent mounting proc in the
                   # nested PID namespace. This option is for the trusted outer
                   # controller; the tool worker still creates its own namespaces.
                   '--security-opt', 'systempaths=unconfined', '--read-only',
                   '--tmpfs', '/tmp:rw,nosuid,nodev,size=64m', '--pids-limit', '128',
                   '--memory', '512m', '--cpus', '2', '--mount',
                   f'type=volume,source={volume},target=/runs', '--workdir', '/opt/mini-codex']
        if args.action != 'live':
            command += ['--network', 'none']
        else:
            for name in MODEL_ENV:
                if env.get(name):
                    command += ['--env', name]  # Values never appear in command arguments.
        if args.workspace is not None:
            workspace = args.workspace.expanduser().resolve(strict=True)
            if (not workspace.is_dir() or workspace == ROOT or ROOT.is_relative_to(workspace)
                    or workspace.is_relative_to(ROOT)):
                raise ValueError('workspace must be a project outside the trusted code tree')
            if not args.prompt or args.action not in ('live', 'replay'):
                raise ValueError('a custom workspace needs --prompt and live/replay action')
            if env_path is not None and env_path.is_relative_to(workspace):
                raise ValueError('credential file must stay outside the workspace')
            command += ['--mount', f'type=bind,source={workspace},target=/project']
        command += [record['image_id'], 'python', '-m', 'mini_codex']
        if args.workspace is not None:
            command += ['run', args.prompt, '--workspace', '/project', '--mode', args.action]
            if args.verify_command:
                command += ['--verify-command', args.verify_command]
        elif args.action == 'doctor':
            command += ['doctor']
        elif args.action == 'test':
            command += ['check', '--mode', 'replay']
        else:
            command += ['demo', '--mode', args.action]
        command += ['--run-dir', '/runs/artifacts', '--state-dir', '/runs/state']
        record['command'] = command
        container_attempted = True
        created = call(command, env=env)
        record['create_exit_code'] = created.returncode
        if created.returncode:
            raise RuntimeError('container creation failed')
        if not created.stdout.strip():
            raise RuntimeError('container creation response has no identity')
        record['container_id'] = created.stdout.strip()
        save()
        print(f'Running {args.action}; container: {container}', flush=True)
        end = time.monotonic() + args.timeout
        started = call(['docker', 'start', record['container_id']], timeout=min(30, args.timeout))
        record['start_exit_code'] = started.returncode
        if started.returncode:
            (run_dir / 'docker-error.log').write_text(started.stderr)
            raise RuntimeError('Docker could not start the container; see docker-error.log')
        while True:
            observed = observe_container(call, record['container_id'])
            if observed.get('run_id') != run_id or observed.get('id') != record['container_id']:
                raise RuntimeError('run container ownership mismatch')
            if observed.get('image') != record['image_id']:
                raise RuntimeError('container image differs from recorded immutable image')
            state = observed['state']
            if state.get('Running') is False:
                break
            if time.monotonic() >= end:
                raise subprocess.TimeoutExpired(['docker', 'wait', record['container_id']], args.timeout)
            time.sleep(0.25)
        actually_started = not state.get('StartedAt', '0001').startswith('0001')
        record['container_exit_code'] = state['ExitCode'] if actually_started else None
        exit_code = state['ExitCode']
        if not actually_started or state.get('Status') != 'exited':
            record.update(status='blocked', error='container did not complete an actual process run')
            exit_code = 2
        else:
            record['status'] = 'command_completed' if exit_code == 0 else 'command_failed'
        record['exit_code'] = exit_code
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        if container_attempted:
            interruption = 'timeout' if isinstance(exc, subprocess.TimeoutExpired) else 'cancelled'
            record.update(status='interrupted', error=str(exc), interruption=interruption,
                          exit_code=124 if interruption == 'timeout' else 130)
        else:
            record.update(status='blocked', error=str(exc), exit_code=2)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        record.update(status='blocked', error=str(exc), exit_code=2)
    finally:
        if container_attempted:
            try:
                finish_container(call, record, run_dir)
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
                record.update(cleanup_status='unknown', cleanup_error='cleanup interrupted: ' + str(exc))
                record.setdefault('termination_status', 'unknown')
                record.setdefault('resources_retained', [container, volume])
            if record['termination_status'] != 'confirmed':
                record.update(original_status=record['status'], status='unknown', exit_code=3)
            elif record['cleanup_status'] != 'confirmed' and record['status'] == 'command_completed':
                record.update(original_status=record['status'], status='blocked', exit_code=2)
        elif volume_attempted:
            # No create was attempted, so no run container can be active. Preserve
            # an uncertain volume rather than deleting an unverified resource.
            record.update(cleanup_status='retained', resources_retained=[volume])
        save()
    print(json.dumps({'status': record['status'], 'exit_code': record['exit_code'],
                      'cleanup_status': record['cleanup_status'], 'reason': record.get('error'),
                      'evidence': str(run_dir)}, ensure_ascii=False))
    return record['exit_code']


if __name__ == '__main__':
    sys.exit(main())
