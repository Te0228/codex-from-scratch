"""Standalone teaching kernel, not an MCP client or a host renderer.

The provider boundary supplies raw resource bytes. This module owns projection,
origin checks, authority epochs, cancellation state and acceptance of the reply.
It does not import or replace the book's Loop or Dispatcher.
"""
import asyncio
import copy
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import unquote, urlsplit


class BridgeError(ValueError):
    pass


UI_EXTENSION = "io.modelcontextprotocol/ui"
UI_MIME = "text/html;profile=mcp-app"


def project_extensions(host_extensions: dict) -> dict:
    """Teaching subset: one known namespace; unknown namespaces are not forwarded."""
    value = host_extensions.get(UI_EXTENSION)
    return {UI_EXTENSION: copy.deepcopy(value)} if isinstance(value, dict) else {}


def valid_ui_uri(uri: str) -> bool:
    """Intentionally stricter than the Codex metadata extractor: no network fetch."""
    if not isinstance(uri, str) or any(c.isspace() for c in uri):
        return False
    try:
        parsed = urlsplit(uri)
        segments = unquote(parsed.path).split("/")
        return (parsed.scheme == "ui" and bool(parsed.netloc)
                and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment
                and ".." not in segments and "\\" not in unquote(uri))
    except ValueError:
        return False


@dataclass(frozen=True)
class UiProjection:
    uri: str | None
    display_mode: str | None


def project_descriptor(descriptor: dict) -> UiProjection:
    """Only tools/list descriptors enter here. Tool results are never input."""
    meta = descriptor.get("_meta")
    if not isinstance(meta, dict):
        return UiProjection(None, None)
    ui = meta.get("ui")
    candidates = [ui.get("resourceUri") if isinstance(ui, dict) else None,
                  meta.get("ui/resourceUri"), meta.get("openai/outputTemplate")]
    uri = next((v for v in candidates if isinstance(v, str)), None)
    if uri is None:
        return UiProjection(None, None)
    if not valid_ui_uri(uri):
        raise BridgeError("invalid_ui_uri")
    preference = meta.get("openai/ui")
    mode = preference.get("preferredModelDisplayMode") if isinstance(preference, dict) else None
    return UiProjection(uri, mode if mode in ("inline", "fullscreen") else None)


@dataclass(frozen=True)
class Binding:
    server: str
    tool: str
    account: str | None
    connection_epoch: int
    catalog_version: int


@dataclass(frozen=True)
class Origin:
    call_id: str
    binding: Binding
    projection: UiProjection


@dataclass(frozen=True)
class Resource:
    server: str
    uri: str
    mime: str
    body: bytes


class OriginLedger:
    def __init__(self):
        self._prepared: dict[str, Origin] = {}
        self._completed: dict[str, Origin] = {}

    def prepare(self, call_id: str, binding: Binding, descriptor: dict) -> Origin:
        if not call_id or call_id in self._prepared:
            raise BridgeError("duplicate_or_empty_call_id")
        origin = Origin(call_id, binding, project_descriptor(descriptor))
        self._prepared[call_id] = origin
        return origin

    def complete(self, call_id: str, *, succeeded: bool) -> None:
        origin = self._prepared[call_id]
        if succeeded and origin.projection.uri is not None:
            self._completed[call_id] = origin

    def lookup(self, call_id: str) -> Origin:
        try:
            return self._completed[call_id]
        except KeyError:
            raise BridgeError("no_successful_origin") from None


class Authority:
    """Controlled current-authority port, not an OAuth implementation."""
    def __init__(self, binding: Binding):
        self.binding = binding
        self.revision = 1

    def replace(self, binding: Binding) -> None:
        self.binding = binding
        self.revision += 1

    def authorize(self, origin: Origin) -> tuple[Binding, int]:
        current, recorded = self.binding, origin.binding
        if (current.server, current.tool, current.account) != (
                recorded.server, recorded.tool, recorded.account):
            raise BridgeError("authority_mismatch")
        # A recovered descriptor is data. A new read uses the current connection.
        return current, self.revision


@dataclass
class ReadWaiter:
    task: asyncio.Task
    cancelled: bool = False


class ScopedResourceReader:
    def __init__(self, ledger: OriginLedger, authority: Authority,
                 fetch: Callable[[Binding, str], Awaitable[Resource]]):
        self.ledger, self.authority, self.fetch = ledger, authority, fetch
        self.pending: dict[str, ReadWaiter] = {}
        self.retiring: set[asyncio.Task] = set()

    def _retire(self, task: asyncio.Task) -> None:
        self.retiring.add(task)
        def done(finished):
            self.retiring.discard(finished)
            if not finished.cancelled():
                finished.exception()  # observe provider errors after local cancellation
        task.add_done_callback(done)

    async def read(self, read_id: str, call_id: str, server: str, uri: str) -> Resource:
        if read_id in self.pending:
            raise BridgeError("duplicate_read_id")
        origin = self.ledger.lookup(call_id)
        if (server, uri) != (origin.binding.server, origin.projection.uri):
            raise BridgeError("origin_mismatch")
        binding, revision = self.authority.authorize(origin)
        waiter = ReadWaiter(asyncio.create_task(self.fetch(binding, uri)))
        self.pending[read_id] = waiter
        try:
            result = await asyncio.shield(waiter.task)
            # Some providers cannot stop already-sent work; reject their late reply.
            if waiter.cancelled:
                raise asyncio.CancelledError
            if revision != self.authority.revision:
                raise BridgeError("stale_authority")
            self.authority.authorize(origin)
            if (result.server, result.uri) != (server, uri):
                raise BridgeError("response_origin_mismatch")
            if result.mime != UI_MIME:
                raise BridgeError("unsupported_media_type")
            return result
        except asyncio.CancelledError:
            waiter.cancelled = True
            waiter.task.cancel()
            self._retire(waiter.task)
            raise
        finally:
            self.pending.pop(read_id, None)

    def cancel(self, read_id: str) -> bool:
        waiter = self.pending.get(read_id)
        if waiter is None:
            return False
        waiter.cancelled = True
        waiter.task.cancel()
        return True


class PendingBook:
    """Correlation kernel only: no transport, notifications or durable replay."""
    def __init__(self, server: str):
        self.server, self.epoch, self.next_id = server, 1, 0
        self.pending: dict[tuple, str] = {}

    def begin(self, call_id: str) -> tuple:
        self.next_id += 1
        key = (self.server, self.epoch, "client_to_server", self.next_id)
        self.pending[key] = call_id
        return key

    def resolve(self, key: tuple) -> str | None:
        return self.pending.pop(key, None)

    def disconnect(self) -> tuple[str, ...]:
        unknown_calls = tuple(self.pending.values())
        self.pending.clear()
        self.epoch += 1
        self.next_id = 0
        return unknown_calls
