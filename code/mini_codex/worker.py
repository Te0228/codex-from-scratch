"""Fixed Linux sandbox. No unsupported-platform or namespace-failure fallback.

The controller is outside the PID/network namespace. Each action has a fresh
namespace; exiting namespace PID 1 kills its remaining descendants. We also kill
the outer process group on cancellation/timeout and check its live members.
"""
import json
import math
import os
from pathlib import Path
import platform
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

from .contracts import (AuthorizedAction, CancelToken, ExecutionLimits,
                        ExecutionObservation)
from .history import digest

CHECKPOINT = Path(__file__).resolve().parent.parent
RESOURCE_ROOT = Path(__file__).resolve().parent / "assets"


class SandboxUnavailable(RuntimeError):
    pass


def live_group_members(group):
    """Linux /proc evidence. Zombies have no running code or writable handles."""
    members = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) == group and fields[0] != "Z":
                members.append(int(entry.name))
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return members


class LinuxWorker:
    def __init__(self, workspace: Path, read_only=False):
        self.workspace = workspace.resolve()
        self.read_only = read_only
        self.clean = True
        self.executions = 0
        self.last_cleanup = {}

    def command(self):
        if platform.system() != "Linux" or sys.version_info < (3, 12):
            raise SandboxUnavailable("requires Linux and Python 3.12+")
        bwrap = shutil.which("bwrap")
        if not bwrap:
            raise SandboxUnavailable("bubblewrap not installed")
        command = [bwrap, "--unshare-all", "--new-session", "--die-with-parent",
                   "--clearenv", "--cap-drop", "ALL"]
        # No host /home, /root, /run, /runs or /etc tree is exposed.
        for directory in ("/usr", "/bin", "/lib", "/lib64"):
            path = Path(directory)
            if path.is_symlink():
                command += ["--symlink", os.readlink(directory), directory]
            elif path.exists():
                command += ["--ro-bind", directory, directory]
        command += ["--dir", "/etc"]
        if Path("/etc/ld.so.cache").exists():
            command += ["--ro-bind", "/etc/ld.so.cache", "/etc/ld.so.cache"]
        command += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                    "--dir", "/opt", "--ro-bind", str(CHECKPOINT), "/opt/mini-codex",
                    "--ro-bind" if self.read_only else "--bind",
                    str(self.workspace), "/workspace", "--chdir", "/workspace",
                    "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin",
                    "--setenv", "HOME", "/tmp", "--setenv", "LANG", "C.UTF-8",
                    "--", sys.executable, "-I", "/opt/mini-codex/mini_codex/worker_entry.py"]
        return command

    def execute(self, action: AuthorizedAction, limits: ExecutionLimits,
                cancel: CancelToken | None = None):
        if Path(action.workspace_ref).resolve() != self.workspace:
            raise ValueError("workspace identity mismatch")
        if not self.clean:
            raise SandboxUnavailable("previous process cleanup unconfirmed")
        cancel = cancel or CancelToken()
        end = min(limits.deadline, time.monotonic() + limits.timeout)
        if cancel.cancelled or end <= time.monotonic():
            return ExecutionObservation(None, "", False, "not_started", "none",
                                        "confirmed", "cancelled_or_deadline")
        command = self.command()  # failure never selects plain subprocess
        packet = json.dumps({"operation": action.operation,
                             "arguments": action.arguments}).encode() + b"\n"
        if len(packet) > 120000:
            raise ValueError("action too large")
        self.executions += 1
        process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True, env={"PATH": "/usr/bin:/bin"})
        self.clean = False
        output, total, stop = bytearray(), 0, None
        selector = selectors.DefaultSelector()
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)
        # The bounded packet is small; communicate it without blocking on a full pipe.
        os.set_blocking(process.stdin.fileno(), False)
        selector.register(process.stdin, selectors.EVENT_WRITE)
        sent = 0
        try:
            while selector.get_map():
                now = time.monotonic()
                if cancel.cancelled or now >= end:
                    stop = "cancelled" if cancel.cancelled else "timeout"
                    break
                for key, _ in selector.select(min(0.05, end - now)):
                    if key.fileobj is process.stdin:
                        try:
                            sent += os.write(process.stdin.fileno(), packet[sent:])
                        except BrokenPipeError:
                            sent = len(packet)
                        if sent == len(packet):
                            selector.unregister(process.stdin)
                            process.stdin.close()
                    else:
                        chunk = os.read(process.stdout.fileno(), 8192)
                        if not chunk:
                            selector.unregister(process.stdout)
                        else:
                            total += len(chunk)
                            output.extend(chunk[:max(0, limits.output_bytes - len(output))])
            if stop or process.poll() is None:
                if not stop:
                    try:
                        process.wait(timeout=max(0.001, end - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        stop = "timeout"
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                stop = "cleanup_unconfirmed"
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            raise
        finally:
            selector.close()
            for stream in (process.stdin, process.stdout):
                if stream and not stream.closed:
                    stream.close()
            members = live_group_members(process.pid)
            self.clean = process.poll() is not None and not members
            self.last_cleanup = {"outer_pid": process.pid, "returncode": process.poll(),
                                 "live_group_members": members,
                                 "pid_namespace_teardown": process.poll() is not None}
        writable = action.operation in ("apply_patch", "exec")
        return ExecutionObservation(process.returncode, output.decode(errors="replace"),
            total > limits.output_bytes, stop or "exited",
            "unknown" if stop and writable else ("confirmed" if writable else "none"),
            "confirmed" if self.clean else "unknown", stop)

    def probe(self, operation, arguments, *, timeout=5, deadline=None):
        action = AuthorizedAction("probe", "probe", operation, arguments, str(self.workspace))
        return self.execute(action, ExecutionLimits(deadline or time.monotonic() + 10,
                                                   timeout=timeout))

    def preflight(self):
        record = {"platform": platform.platform(), "python": sys.version,
                  "boundary": "Linux controller container -> bubblewrap worker",
                  "workspace": str(self.workspace), "status": "blocked"}
        try:
            record["policy"] = self.command()
            record["policy_hash"] = digest(record["policy"])
            record["bwrap"] = subprocess.check_output([shutil.which("bwrap"), "--version"],
                                                       text=True, timeout=2).strip()
            allow = self.probe("exec", {"command": "printf allowed > .probe; cat .probe"})
            record["workspace_control"] = allow.__dict__
            if allow.exit_code != 0 or allow.output != "allowed":
                raise SandboxUnavailable("same-policy workspace write/read failed")
            with tempfile.TemporaryDirectory(prefix="ch01-host-marker-") as directory:
                marker = Path(directory) / "secret.txt"
                marker.write_text("host-control-present")
                record["host_file_control"] = marker.read_text() == "host-control-present"
                # The same absolute path exists in the controller, is absent in worker mount ns.
                file_probe = self.probe("read", {"path": str(marker)})
                exec_probe = self.probe("exec", {"command": f"cat {marker}"})
                record["file_tool_denial"] = file_probe.__dict__
                record["exec_os_denial"] = exec_probe.__dict__
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                port = listener.getsockname()[1]
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    record["host_network_control"] = True
                command = (f"{sys.executable} -I -c 'import socket; "
                           f"socket.create_connection((\"127.0.0.1\", {port}), timeout=1)' ")
                network_probe = self.probe("exec", {"command": command})
                record["network_os_denial"] = network_probe.__dict__
            (self.workspace / ".probe").unlink(missing_ok=True)
            denies = (file_probe, exec_probe, network_probe)
            if any(p.exit_code == 0 or p.process_status != "exited" or
                   p.cleanup_status != "confirmed" for p in denies):
                raise SandboxUnavailable("paired denial or cleanup was not confirmed")
            record["status"] = "passed"
        except (SandboxUnavailable, OSError, subprocess.SubprocessError) as exc:
            record["error"] = str(exc)[:500]
        return record
