"""One command-line entry to the same engine used throughout the notebooks."""
import argparse
import io
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import time
import unittest
import uuid

from .contracts import data
from .verify import Verifier, save_json
from .worker import CHECKPOINT, RESOURCE_ROOT, LinuxWorker


def prepare(run_dir):
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace = run_dir / 'workspace'
    workspace.mkdir()
    shutil.copy(RESOURCE_ROOT / 'fixtures/task-list/task_list.py', workspace / 'task_list.py')
    return workspace


def load_env(path):
    if path is None:
        return
    allowed = {'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_MODEL', 'OPENAI_REASONING_EFFORT'}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        name, separator, value = line.removeprefix('export ').partition('=')
        if separator and name.strip() in allowed:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ[name.strip()] = value


def main(argv=None):
    parser = argparse.ArgumentParser(description='A small, source-grounded Coding Agent')
    parser.add_argument('command', choices=('demo', 'run', 'doctor', 'check', 'serve'))
    parser.add_argument('prompt', nargs='?')
    parser.add_argument('--mode', choices=('replay', 'live'), default=None)
    parser.add_argument('--workspace', type=Path)
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--run-dir', type=Path)
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--verify-command')
    parser.add_argument('--max-steps', type=int, default=16)
    parser.add_argument('--deadline-seconds', type=float, default=180)
    parser.add_argument('--context-budget', type=int, default=32000)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args(argv)
    if args.max_steps < 1 or args.deadline_seconds <= 0 or args.context_budget < 1:
        parser.error('Limits must be positive')
    mode = args.mode or ('live' if args.command == 'run' else 'replay')
    default_base = Path.home() / '.mini-codex' if args.command in ('run', 'serve') else Path.cwd()
    run_dir = (args.run_dir or default_base / 'runs' / ('agent-' + uuid.uuid4().hex[:12])).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    state_dir = (args.state_dir or run_dir.parent / 'state').resolve()

    def finish(status, reason):
        result = {'task_status': status, 'stop_reason': reason, 'mode': mode, 'run_dir': str(run_dir)}
        save_json(run_dir / 'result.json', result)
        print(json.dumps(result, ensure_ascii=False))
        return {'passed': 0, 'blocked': 2, 'needs_review': 3, 'cancelled': 130}.get(status, 1)

    try:
        if args.command in ('run', 'serve'):
            if not args.workspace or not args.workspace.is_dir():
                parser.error('run/serve needs --workspace pointing to an existing project')
            if args.command == 'run' and not args.prompt:
                parser.error('run needs a prompt and --workspace pointing to an existing project')
            workspace = args.workspace.resolve()
            if workspace.is_relative_to(CHECKPOINT) or CHECKPOINT.is_relative_to(workspace):
                return finish('blocked', 'workspace_must_not_overlap_trusted_code')
            if state_dir.is_relative_to(workspace) or workspace.is_relative_to(state_dir):
                return finish('blocked', 'state_directory_must_be_separate_from_workspace')
            if run_dir.is_relative_to(workspace):
                return finish('blocked', 'run_directory_must_be_outside_workspace')
        else:
            workspace = prepare(run_dir)
        if args.env_file:
            spelled_path = args.env_file.expanduser().absolute()
            config_path = spelled_path.resolve(strict=True)
            if spelled_path.is_relative_to(CHECKPOINT) or config_path.is_relative_to(CHECKPOINT):
                return finish('blocked', 'credential_file_must_be_outside_trusted_code')
            if config_path.is_relative_to(workspace):
                return finish('blocked', 'credential_file_must_be_outside_workspace')
            load_env(config_path)
        if args.command == 'serve':
            from .server import serve
            return serve(workspace=workspace, state_dir=state_dir,
                         host=args.host, port=args.port, mode=mode)
        preflight = LinuxWorker(workspace).preflight()
        save_json(run_dir / 'preflight.json', preflight)
        if preflight['status'] != 'passed':
            return finish('blocked', 'sandbox_preflight: ' + preflight.get('error', 'failed'))
        if args.command == 'doctor':
            return finish('passed', 'sandbox_preflight_only')
        if args.command == 'check':
            os.environ['MINI_CODEX_TEST_EVIDENCE'] = str(run_dir / 'probes')
            suite = unittest.defaultTestLoader.discover(str(CHECKPOINT / 'tests'))
            stream = io.StringIO()
            tests = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
            (run_dir / 'tests.log').write_text(stream.getvalue())
            save_json(run_dir / 'tests.json', {'tests_run': tests.testsRun,
                'failures': len(tests.failures), 'errors': len(tests.errors),
                'skipped': len(tests.skipped), 'scope': 'final_package', 'mode': 'replay',
                'isolation': 'real LinuxWorker in integration tests; other tests label their ports'})
            print(stream.getvalue(), end='')
            return finish('passed' if tests.wasSuccessful() and not tests.skipped else 'failed', 'test_suite')
        from .engine import AgentConfig, MiniCodex
        verifier = None
        if args.command == 'demo':
            verifier = Verifier()
            baseline = verifier.baseline(workspace, time.monotonic() + args.deadline_seconds)
            save_json(run_dir / 'baseline.json', baseline)
            if not baseline['baseline_valid']:
                return finish('blocked', 'fixed_red_baseline_not_witnessed')
        config = AgentConfig(workspace=workspace, state_dir=state_dir, mode=mode,
            max_steps=args.max_steps, timeout=args.deadline_seconds,
            context_budget=args.context_budget, verify_command=args.verify_command, run_dir=run_dir)
        agent = MiniCodex(config)
        signal.signal(signal.SIGINT, lambda *_: agent.cancel())
        signal.signal(signal.SIGTERM, lambda *_: agent.cancel())
        prompt = args.prompt or '修复 task_list.py 的 status 筛选缺陷；独立基线已确认指定断言为红。'
        if args.command == 'demo' and mode == 'replay':
            from .model import ReplayModel
            replay_responses = ReplayModel().responses
        else:
            replay_responses = None
        try:
            result = agent.run(prompt, verifier=verifier, replay_responses=replay_responses)
        finally:
            agent.close()
        print(json.dumps({**data(result), 'run_dir': str(agent.run_dir)}, ensure_ascii=False))
        return {'passed': 0, 'blocked': 2, 'needs_review': 3, 'cancelled': 130}.get(result.task_status, 1)
    except (OSError, ValueError) as exc:
        return finish('blocked', str(exc)[:300])


if __name__ == '__main__':
    raise SystemExit(main())
