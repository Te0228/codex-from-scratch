"""Bounded JSON-RPC MCP stdio client for explicitly configured local commands.

No command is accepted from model tool arguments. The host chooses the command
and allowed tools. A timeout after dispatch is not proof that a remote write did
not happen; there is deliberately no automatic tool-call retry.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from typing import Sequence


class MCPError(RuntimeError):
    def __init__(self, code, *, outcome='failed', side_effect_status='none'):
        super().__init__(code)
        self.code, self.outcome, self.side_effect_status = code, outcome, side_effect_status


class StdioMCPClient:
    def __init__(self, command: Sequence[str], *, cwd=None, allowed_tools=(), read_only_tools=(), timeout=5.0):
        if not command or not all(isinstance(x, str) and x for x in command):
            raise ValueError('command must be a nonempty argv sequence')
        self.command, self.cwd = tuple(command), cwd
        self.allowed_tools, self.read_only_tools = frozenset(allowed_tools), frozenset(read_only_tools)
        self.timeout = max(0.01, min(float(timeout), 60))
        self.process = None
        self.reader = None
        self.pending = {}
        self.notifications = []
        self.lock = threading.RLock()
        self.pending_lock = threading.Lock()
        self.next_id = 0
        self.closed = False

    def _reader(self):
        try:
            while True:
                raw = self.process.stdout.readline(1_048_578)
                if not raw:
                    raise EOFError('mcp_transport_closed')
                if len(raw) > 1_048_576 or not raw.endswith(b'\n'):
                    raise ValueError('mcp_frame_limit')
                frame = json.loads(raw)
                if not isinstance(frame, dict):
                    raise ValueError('mcp_invalid_frame')
                if 'method' in frame:
                    # Reverse requests are not implicitly approved by this minimal host.
                    if 'id' in frame:
                        self._write({'jsonrpc':'2.0', 'id':frame['id'],
                            'error':{'code':-32601, 'message':'reverse request unsupported'}})
                    elif len(self.notifications) < 128:
                        self.notifications.append(frame)
                    continue
                with self.pending_lock:
                    waiter = self.pending.get(frame.get('id'))
                if waiter:
                    waiter.put(frame)
        except Exception as exc:
            with self.pending_lock:
                waiters = tuple(self.pending.values())
            for waiter in waiters:
                waiter.put(exc)

    def _write(self, value):
        # Requests are serialized by self.lock. Only the reader writes reverse errors.
        payload = json.dumps(value, ensure_ascii=False).encode() + b'\n'
        with self.write_lock:
            self.process.stdin.write(payload)
            self.process.stdin.flush()

    def start(self, *, deadline=None, cancel=None):
        with self.lock:
            if self.closed:
                raise MCPError('mcp_client_closed')
            if self.process:
                return
            env = {key:value for key,value in os.environ.items()
                   if key in {'PATH','LANG','LC_ALL','TMPDIR','SYSTEMROOT'}}
            self.process = subprocess.Popen(self.command, cwd=self.cwd, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.write_lock = threading.Lock()
            self.reader = threading.Thread(target=self._reader, daemon=True, name='mini-codex-mcp-reader')
            self.reader.start()
            try:
                result = self._request('initialize', {'protocolVersion':'2024-11-05',
                    'capabilities':{}, 'clientInfo':{'name':'mini-codex','version':'1'}},
                    deadline=min(deadline or float('inf'),time.monotonic()+self.timeout), cancel=cancel)
                if not isinstance(result, dict) or 'protocolVersion' not in result:
                    raise MCPError('mcp_invalid_initialize')
                self._write({'jsonrpc':'2.0','method':'notifications/initialized'})
            except BaseException:
                self.close()
                raise

    def _request(self, method, params, *, deadline, cancel=None, side_effect_possible=False):
        with self.lock:
            self.next_id += 1
            request_id = self.next_id
            waiter = queue.Queue()
            with self.pending_lock:
                self.pending[request_id] = waiter
            sent = False
            try:
                if cancel is not None and cancel.is_set():
                    raise MCPError('mcp_cancelled_before_dispatch', outcome='cancelled')
                if deadline <= time.monotonic():
                    raise MCPError('mcp_deadline_before_dispatch')
                sent = True  # A failed flush can still leave a dispatched request.
                self._write({'jsonrpc':'2.0','id':request_id,'method':method,'params':params})
                while True:
                    if cancel is not None and cancel.is_set():
                        raise TimeoutError('mcp_cancelled_after_dispatch')
                    remaining = deadline-time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('mcp_timeout_after_dispatch')
                    try:
                        frame = waiter.get(timeout=min(remaining,0.02))
                        break
                    except queue.Empty:
                        continue
                if isinstance(frame, Exception):
                    raise ConnectionError('mcp_transport_lost') from frame
                if 'error' in frame:
                    raise MCPError('mcp_remote_error', side_effect_status='unknown' if side_effect_possible else 'none')
                if 'result' not in frame:
                    raise ValueError('mcp_missing_result')
                return frame['result']
            except MCPError:
                raise
            except (OSError, ValueError, TimeoutError) as exc:
                # Stop this owned transport; never replay an uncertain tool call.
                self.close()
                raise MCPError(type(exc).__name__ + ':' + str(exc),
                    outcome='unknown' if sent else 'failed',
                    side_effect_status='unknown' if sent and side_effect_possible else 'none') from exc
            finally:
                with self.pending_lock:
                    self.pending.pop(request_id, None)

    def list_tools(self, *, deadline=None, cancel=None):
        self.start(deadline=deadline,cancel=cancel)
        result = self._request('tools/list', {}, deadline=deadline or time.monotonic()+self.timeout, cancel=cancel)
        if not isinstance(result, dict) or not isinstance(result.get('tools'), list):
            raise MCPError('mcp_invalid_tool_list')
        return [tool for tool in result['tools'] if tool.get('name') in self.allowed_tools]

    def call(self, name, arguments, *, deadline=None, cancel=None):
        if name not in self.allowed_tools:
            raise MCPError('mcp_tool_not_allowlisted')
        self.start(deadline=deadline,cancel=cancel)
        result = self._request('tools/call', {'name':name,'arguments':arguments},
            deadline=deadline or time.monotonic()+self.timeout, cancel=cancel,
            side_effect_possible=name not in self.read_only_tools)
        if not isinstance(result, dict):
            raise MCPError('mcp_invalid_tool_result', outcome='unknown',
                side_effect_status='none' if name in self.read_only_tools else 'unknown')
        return {'outcome':'failed' if result.get('isError') else 'succeeded', 'result':result,
                'error':'mcp_tool_error' if result.get('isError') else None,
                'side_effect_status':'none' if name in self.read_only_tools else 'unknown',
                'cleanup_status':'complete'}

    def close(self):
        with self.lock:
            self.closed = True
            if self.process:
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=2)
                for stream in (self.process.stdin,self.process.stdout):
                    if stream:
                        stream.close()
            if self.reader and self.reader is not threading.current_thread():
                self.reader.join(timeout=0.5)
