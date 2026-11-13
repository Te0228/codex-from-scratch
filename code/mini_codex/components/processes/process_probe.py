"""Real POSIX mechanism prototype; no sandbox, model, checkpoint or Loop claim.

Only launch the controlled probe commands. The manager proves cleanup of its
process group, not containment of a hostile descendant that calls setsid().
Output is byte-addressed and spooled, so poll does not own the reader's lifetime.
"""
from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path
import pty
import selectors
import signal
import struct
import subprocess
import termios
import threading
import time
import uuid


def group_members(pgid: int) -> list[dict]:
    """Observe PGID and state; zombies remain visible rather than called absent."""
    result = subprocess.run(["/bin/ps", "-axo", "pid=,pgid=,stat="],
                            capture_output=True, text=True, check=True)
    found = []
    for row in result.stdout.splitlines():
        pid, group, state = row.split()
        if int(group) == pgid:
            found.append({"pid": int(pid), "state": state})
    return found


class Session:
    def __init__(self, argv: list[str], directory: Path, *, tty=False,
                 timeout=5.0, spool_limit=4 * 1024 * 1024):
        self.handle = uuid.uuid4().hex
        self.path = directory / (self.handle + ".bin")
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.tty = tty
        self.stored = self.total = 0
        self.spool_limit = spool_limit
        self.deadline = time.monotonic() + timeout
        self.stop_reason = None
        self.output_closed = False
        self.fds_closed = False
        self.closed = False
        self.reader_error = None
        self.cleanup_receipt = None
        self.close_requested = threading.Event()
        # A complete supplied environment: do not inspect/inherit host secrets.
        environment = {"PATH": "/usr/bin:/bin", "LANG": "C", "TERM": "dumb"}
        if tty:
            master, slave = pty.openpty()
            try:
                self.process = subprocess.Popen(argv, stdin=slave, stdout=slave,
                    stderr=slave, start_new_session=True, env=environment)
            except BaseException:
                os.close(master)
                raise
            finally:
                os.close(slave)
            self.read_fd = self.write_fd = master
        else:
            self.process = subprocess.Popen(argv, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                start_new_session=True, env=environment)
            self.read_fd = self.process.stdout.fileno()
            self.write_fd = self.process.stdin.fileno()
        os.set_blocking(self.read_fd, False)
        os.set_blocking(self.write_fd, False)
        self.path.touch(exist_ok=False)
        self.thread = threading.Thread(target=self._read, name="probe-collector")
        self.thread.start()

    def _signal_group(self, sig):
        try:
            os.killpg(self.process.pid, sig)
        except ProcessLookupError:
            pass

    def _read(self):
        selector = selectors.DefaultSelector()
        selector.register(self.read_fd, selectors.EVENT_READ)
        terminate_at = None
        try:
            with self.path.open("ab", buffering=0) as spool:
                while True:
                    now = time.monotonic()
                    if terminate_at is None:
                        if self.close_requested.is_set():
                            self.stop_reason = "cancelled"
                        elif now >= self.deadline:
                            self.stop_reason = "timeout"
                        if self.stop_reason:
                            self._signal_group(signal.SIGTERM)
                            terminate_at = now
                    elif now - terminate_at >= 0.15:
                        self._signal_group(signal.SIGKILL)
                    for _, _ in selector.select(0.02):
                        try:
                            chunk = os.read(self.read_fd, 8192)
                        except BlockingIOError:
                            continue
                        except OSError as exc:
                            if self.tty and exc.errno == errno.EIO:
                                chunk = b""  # POSIX PTY slave closure on this host.
                            else:
                                raise
                        with self.changed:
                            if not chunk:
                                selector.unregister(self.read_fd)
                                self.output_closed = True
                            else:
                                kept = chunk[:max(0, self.spool_limit - self.stored)]
                                written = spool.write(kept)
                                if written != len(kept):
                                    raise OSError("short spool write")
                                self.stored += len(kept)
                                self.total += len(chunk)
                                if len(kept) != len(chunk):
                                    self.stop_reason = "output_quota"
                            self.changed.notify_all()
                    exited = self.process.poll() is not None
                    if self.output_closed and exited:
                        break
                    if terminate_at is not None and now - terminate_at > 2:
                        self.reader_error = "drain_deadline"
                        break
        except BaseException as exc:
            self.reader_error = type(exc).__name__ + ": " + str(exc)
            self._signal_group(signal.SIGKILL)
        finally:
            selector.close()
            # Serialize writes versus close; no FD may be reused between lookup/write.
            with self.changed:
                if self.tty:
                    os.close(self.read_fd)
                else:
                    self.process.stdin.close()
                    self.process.stdout.close()
                self.fds_closed = True
                self.changed.notify_all()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.reader_error = "root_reap_timeout"

    def poll(self, cursor=0, *, budget=1024, wait=0.0, wait_cancel=None):
        if not isinstance(budget, int) or budget <= 0:
            raise ValueError("budget must be positive")
        until = time.monotonic() + wait
        with self.changed:
            if not isinstance(cursor, int) or cursor < 0 or cursor > self.stored:
                raise ValueError("cursor outside retained byte log")
            while self.stored == cursor and not self.fds_closed:
                if wait_cancel is not None and wait_cancel.is_set():
                    break
                left = until - time.monotonic()
                if left <= 0:
                    break
                self.changed.wait(min(left, 0.02))
            available = self.stored
            with self.path.open("rb") as stream:
                stream.seek(cursor)
                data = stream.read(min(budget, available - cursor))
            root_code = self.process.poll()
            return {"handle": self.handle, "cursor": cursor,
                "next_cursor": cursor + len(data), "data": data,
                "available_cursor": available,
                "has_more": cursor + len(data) < available,
                "response_truncated": cursor + len(data) < available,
                "truncation_notice": "response budget reached; continue from next_cursor"
                    if cursor + len(data) < available else None,
                "capture_lost_bytes": self.total - self.stored,
                "capture_complete": self.output_closed and self.total == self.stored
                    and self.reader_error is None,
                "output_ref": str(self.path), "root_exited": root_code is not None,
                "exit_code": root_code, "output_closed": self.output_closed,
                "fds_closed": self.fds_closed, "stop_reason": self.stop_reason,
                "wait_cancelled": bool(wait_cancel and wait_cancel.is_set()),
                "reader_error": self.reader_error}

    def write(self, data: bytes):
        with self.lock:
            if self.closed or self.fds_closed or self.process.poll() is not None:
                raise ValueError("closed_process_handle")
            # Receipt is accepted-by-kernel bytes; not application acknowledgement.
            return os.write(self.write_fd, data)

    def resize(self, rows: int, cols: int):
        with self.lock:
            if self.closed or self.fds_closed:
                raise ValueError("closed_process_handle")
            if not self.tty:
                raise ValueError("not_a_pty")
            if not (1 <= rows <= 1000 and 1 <= cols <= 1000):
                raise ValueError("invalid_terminal_size")
            fcntl.ioctl(self.read_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def close(self):
        with self.lock:
            if self.cleanup_receipt is not None:
                return self.cleanup_receipt
            self.closed = True
        self.close_requested.set()
        # The root may have exited and the collector may have completed while
        # a descendant closed its output FDs but remained alive. Always signal PGID.
        self._signal_group(signal.SIGTERM)
        self.thread.join(timeout=2.5)
        members = group_members(self.process.pid)
        if members:
            self._signal_group(signal.SIGKILL)
        until = time.monotonic() + 2
        while members and time.monotonic() < until:
            time.sleep(0.02)
            members = group_members(self.process.pid)
        self.thread.join(timeout=0.5)
        root_code = self.process.poll()
        self.cleanup_receipt = {"root_exited": root_code is not None, "exit_code": root_code,
            "members": members, "fds_closed": self.fds_closed,
            "collector_joined": not self.thread.is_alive(),
            "confirmed_for_process_group": root_code is not None and not members
                and self.fds_closed and not self.thread.is_alive(),
            "scope": "owned_process_group_only; setsid escape not contained",
            "reader_error": self.reader_error}
        return self.cleanup_receipt


class Manager:
    def __init__(self, directory: Path):
        self.directory = directory
        self.entries = {}

    def start(self, argv, **options):
        session = Session(argv, self.directory, **options)
        self.entries[session.handle] = session
        return session.handle

    def get(self, handle):
        if handle not in self.entries:
            raise ValueError("unknown_process_handle")
        return self.entries[handle]

    def close_all(self):
        return {handle: session.close() for handle, session in self.entries.items()}
