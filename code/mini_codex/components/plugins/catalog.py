"""Local, single-process teaching prototype; not the integrated mini_codex Loop.

Only inert JSON manifests are installed. No module import, subprocess, account,
marketplace, credential or network access occurs. A supplied executor owns calls.
"""
import asyncio
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import uuid


EXPOSURE = {
    "Direct": frozenset(("initial", "nested")),
    "Deferred": frozenset(("search", "nested")),
    "DeferredModelOnly": frozenset(("search",)),
    "DirectModelOnly": frozenset(("initial",)),
    "CodeModeOnly": frozenset(("nested",)),
    "Hidden": frozenset(),
}


class StaleCatalog(ValueError):
    pass


class Denied(PermissionError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class CatalogToken:
    epoch: str
    revision: int
    auth_revision: int


@dataclass(frozen=True)
class ToolRef:
    token: CatalogToken
    plugin_id: str
    version: str
    environment_id: str
    name: str
    content_hash: str


def validate_manifest(value):
    if not isinstance(value, dict):
        raise ValueError("manifest must be an object")
    for key in ("plugin_id", "version", "environment_id"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise ValueError(f"missing {key}")
    names = set()
    if not isinstance(value.get("tools"), list):
        raise ValueError("tools must be an array")
    for tool in value["tools"]:
        name = tool.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*", name):
            raise ValueError("wire name must be namespace.name")
        if name in names:
            raise ValueError("duplicate wire name")
        names.add(name)
        if tool.get("exposure") not in EXPOSURE or not isinstance(tool.get("schema"), dict):
            raise ValueError("invalid exposure or schema")
        if not isinstance(tool.get("description"), str):
            raise ValueError("missing description")
    return deepcopy(value)


class PluginStore:
    """Owns durable installed/enabled state; deliberately installs metadata only.

    One JSON publication is atomic for readers. Power-loss durability and
    multiprocess locking are outside this prototype's contract.
    """
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "installed.json"
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {}
        for record in self.state.values():
            validate_manifest(record["manifest"])
        self.epoch = str(uuid.uuid4())
        self.revision = 0
        self.auth_revision = 0
        self.pending = {}
        self.lock = asyncio.Lock()
        self.listeners = []

    @property
    def token(self):
        return CatalogToken(self.epoch, self.revision, self.auth_revision)

    def _changed(self):
        self.revision += 1
        for listener in self.listeners:
            listener()

    def _publish(self, state):
        temporary = self.root / (".publish-" + str(uuid.uuid4()))
        try:
            temporary.write_text(canonical(state))
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
        self.state = state
        self._changed()

    def request_install(self, source, *, root_request, reason, candidates):
        manifest = validate_manifest(json.loads(Path(source).read_text()))
        if not root_request or not reason.strip() or manifest["plugin_id"] not in candidates:
            raise Denied("invalid install request")
        request_id = str(uuid.uuid4())
        self.pending[request_id] = manifest
        return request_id

    async def confirm_install(self, request_id, *, accepted, enabled=False,
                              before_commit=None, lose_ack=False):
        manifest = self.pending.pop(request_id)
        if not accepted:
            return "declined"
        async with self.lock:
            stage = self.root / (".stage-" + request_id)
            try:
                stage.write_text(canonical(manifest))
                if before_commit:
                    await before_commit()
                state = deepcopy(self.state)
                state[manifest["plugin_id"]] = {
                    "manifest": json.loads(stage.read_text()), "enabled": enabled}
                self._publish(state)
            finally:
                stage.unlink(missing_ok=True)
        if lose_ack:
            raise ConnectionError("committed; response lost")
        return "installed"

    async def enable(self, plugin_id, enabled):
        async with self.lock:
            if self.state[plugin_id]["enabled"] == enabled:
                return
            state = deepcopy(self.state)
            state[plugin_id]["enabled"] = enabled
            self._publish(state)

    async def uninstall(self, plugin_id):
        async with self.lock:
            state = deepcopy(self.state)
            del state[plugin_id]
            self._publish(state)

    def auth_changed(self):
        # Input is an opaque identity revision, never a credential.
        self.auth_revision += 1
        self._changed()


class ToolCatalog:
    def __init__(self, store):
        self.store = store
        self.schema_cache = {}
        self._index_key = None
        self._index = ()
        self.index_builds = 0
        store.listeners.append(self.invalidate)

    def invalidate(self):
        self.schema_cache.clear()
        self._index_key = None

    def entries(self):
        # All contribution conflicts fail closed; never pick a package by name order.
        entries = {}
        for plugin_id, record in sorted(self.store.state.items()):
            if not record["enabled"]:
                continue
            manifest = record["manifest"]
            for tool in manifest["tools"]:
                if tool["name"] in entries:
                    raise Denied("duplicate contribution")
                ref = ToolRef(self.store.token, plugin_id, manifest["version"],
                              manifest["environment_id"], tool["name"], digest(tool))
                entries[tool["name"]] = (ref, deepcopy(tool))
        return entries

    def _current(self, ref):
        if ref.token != self.store.token:
            raise StaleCatalog("catalog generation changed")
        current = self.entries().get(ref.name)
        if current is None or current[0] != ref:
            raise StaleCatalog("tool identity changed")
        return current[1]

    def list_exposed(self, surface):
        return [(ref, tool) for ref, tool in self.entries().values()
                if surface in EXPOSURE[tool["exposure"]]]

    def search(self, query):
        if not query.strip():
            raise ValueError("empty query")
        entries = self.list_exposed("search")
        key = digest([(ref.__dict__ | {"token": ref.token.__dict__}, tool)
                      for ref, tool in entries])
        if key != self._index_key:
            self._index_key = key
            self._index = tuple((ref, (ref.name + " " + tool["description"]).casefold())
                                for ref, tool in entries)
            self.index_builds += 1
        terms = query.casefold().split()
        return [ref for ref, text in self._index if all(term in text for term in terms)]

    async def load_schema(self, ref, *, loader=None):
        tool = self._current(ref)
        if ref in self.schema_cache:
            return deepcopy(self.schema_cache[ref])
        schema = await loader(tool) if loader else deepcopy(tool["schema"])
        # This is the publication fence: every await invalidates an old assumption.
        current = self._current(ref)
        if schema != current["schema"]:
            raise StaleCatalog("returned schema does not match current declaration")
        self.schema_cache[ref] = deepcopy(schema)
        return deepcopy(schema)

    def resolve_call(self, ref, *, surface, authorize):
        tool = self._current(ref)
        allowed = EXPOSURE[tool["exposure"]]
        visible = (surface == "nested" and "nested" in allowed or surface == "model" and
                   ("initial" in allowed or "search" in allowed and ref in self.schema_cache))
        if not visible:
            raise Denied("tool unavailable on this surface")
        if not authorize(ref):
            raise Denied("execution policy denied")
        # Authorization is independent and must not mutate our snapshot unnoticed.
        self._current(ref)
        return ref


def merge_namespaces(definitions):
    result = {}
    for ref, schema in definitions:
        namespace, name = ref.name.split(".", 1)
        if name in result.setdefault(namespace, {}):
            raise Denied("duplicate wire identity")
        result[namespace][name] = deepcopy(schema)
    return result
