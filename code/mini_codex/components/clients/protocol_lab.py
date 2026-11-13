"""Teaching transport/projection algorithms, not an Agent or a Codex SDK clone."""
from concurrent.futures import Future
from dataclasses import dataclass, field
import json


class ProtocolError(Exception):
    pass


class JsonlDecoder:
    """One decoder per invocation. A newline commits a complete UTF-8 frame."""
    def __init__(self, max_frame_bytes=1 << 20):
        self.buffer = bytearray()
        self.max_frame_bytes = max_frame_bytes

    def feed(self, chunk):
        self.buffer.extend(chunk)
        while b"\n" in self.buffer:
            boundary = self.buffer.index(b"\n")
            if boundary > self.max_frame_bytes:
                raise ProtocolError("frame_too_large")
            line = bytes(self.buffer[:boundary])
            del self.buffer[:boundary + 1]
            try:
                value = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ProtocolError("invalid_frame") from error
            if not isinstance(value, dict):
                raise ProtocolError("frame_must_be_object")
            yield value
        if len(self.buffer) > self.max_frame_bytes:
            raise ProtocolError("frame_too_large")

    def finish(self):
        if self.buffer:
            raise ProtocolError("truncated_frame")


class PendingRouter:
    """Called by one connection event loop; keys carry a local connection epoch.

    This prototype does not write RPC messages, run callbacks, or cancel server turns.
    The reader must retain the epoch captured when its transport was created.
    """
    def __init__(self, epoch):
        self.epoch = epoch
        self.pending = {}
        self.closed = False

    def register(self, request_id):
        if self.closed:
            raise ConnectionError("connection_closed")
        if request_id in self.pending:
            raise ProtocolError("duplicate_id")
        future = Future()
        self.pending[request_id] = future
        return future

    def response(self, reader_epoch, request_id, result):
        if reader_epoch != self.epoch or self.closed:
            return False
        future = self.pending.pop(request_id, None)
        if future is None:
            return False
        future.set_result(result)
        return True

    def cancel_wait(self, request_id):
        future = self.pending.pop(request_id, None)
        return future.cancel() if future else False

    def disconnect(self):
        self.closed = True
        pending, self.pending = self.pending, {}
        for future in pending.values():
            future.set_exception(ConnectionError("connection_closed; remote_status_unknown"))


@dataclass
class Projection:
    """Read-only projection of teaching events; has no scheduler reference."""
    selected_thread: str
    lines: list[str] = field(default_factory=list)
    last_seq: int = 0

    def accept(self, event):
        if event["thread_id"] != self.selected_thread or event["seq"] <= self.last_seq:
            return False
        self.last_seq = event["seq"]
        self.lines.append(f'{event["kind"]}: {event.get("text", "")}')
        return True
