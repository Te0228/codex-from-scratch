"""A bounded local Skill loader; no model loop, execution, installation or grants.

Teaching subset: one-line name/description frontmatter, direct-child packages,
no symlinks, fresh file reads. Not a Codex-compatible parser or OS sandbox.
"""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Callable
import json
import re


class SkillError(Exception):
    def __init__(self, code: str, detail: str):
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class Entry:
    authority: str
    package: str
    name: str
    description: str
    root: Path

    @property
    def locator(self):
        return f"skill://{self.authority}/{self.package}"


@dataclass(frozen=True)
class Catalog:
    step_id: str
    entries: tuple[Entry, ...]
    warnings: tuple[str, ...]

    def display(self):
        return tuple({"name": e.name, "description": e.description,
                      "package": e.locator} for e in self.entries)


@dataclass(frozen=True)
class Fragment:
    locator: str
    resource: str
    sha256: str
    text: str


@dataclass(frozen=True)
class LoadBundle:
    step_id: str
    fragments: tuple[Fragment, ...]


@dataclass(frozen=True)
class Page:
    resource: str
    contents: str
    next_cursor: str | None

    def wire(self):
        return json.dumps(self.__dict__, ensure_ascii=False, separators=(",", ":"))


def check_cancel(cancelled: Callable[[], bool]):
    if cancelled():
        raise SkillError("cancelled", "no partial bundle committed")


def safe_file(root: Path, relative: str) -> Path:
    # This rejects symlinks even when they point back inside root. It is stricter
    # than lexical containment and still not a hostile-race-resistant filesystem.
    if not relative or "\\" in relative or relative.startswith("/"):
        raise SkillError("invalid_resource", relative)
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise SkillError("invalid_resource", relative)
    target = root
    for part in parts:
        target = target / part
        if target.is_symlink():
            raise SkillError("symlink_rejected", relative)
    try:
        real = target.resolve(strict=True)
        real.relative_to(root.resolve(strict=True))
    except FileNotFoundError as exc:
        raise SkillError("resource_missing", relative) from exc
    except ValueError as exc:
        raise SkillError("resource_escape", relative) from exc
    if not real.is_file():
        raise SkillError("not_a_file", relative)
    return real


class Skills:
    def __init__(self, roots: dict[str, Path], is_allowed: Callable[[str], bool],
                 *, max_bytes=65536, max_header_bytes=4096, max_packages=128):
        # Config owns roots and live policy; method text has no setter for either.
        self.roots = {name: path.resolve(strict=True) for name, path in roots.items()}
        if any(not re.fullmatch(r"[a-z][a-z0-9_-]*", name) for name in roots):
            raise ValueError("authority must be a simple configured identifier")
        self.is_allowed = is_allowed
        self.max_bytes, self.max_header_bytes = max_bytes, max_header_bytes
        self.max_packages = max_packages
        self.events: list[dict] = []

    def _metadata(self, root):
        path = safe_file(root, "SKILL.md")
        fields, consumed = {}, 0
        with path.open("rb") as stream:
            if stream.readline(5).strip() != b"---":
                raise SkillError("metadata_invalid", "missing opening frontmatter")
            while True:
                raw = stream.readline(self.max_header_bytes + 1)
                consumed += len(raw)
                if not raw or consumed > self.max_header_bytes:
                    raise SkillError("metadata_invalid", "unclosed or oversized frontmatter")
                line = raw.decode("utf-8").strip()
                if line == "---":
                    break
                key, sep, value = line.partition(":")
                if not sep or key not in {"name", "description"} or key in fields:
                    raise SkillError("metadata_invalid", "only unique name/description supported")
                fields[key] = value.strip()
        if not fields.get("name") or not fields.get("description"):
            raise SkillError("metadata_invalid", "name and description required")
        return fields

    def discover(self, step_id: str, cancelled=lambda: False) -> Catalog:
        entries, warnings = [], []
        for authority, root in sorted(self.roots.items()):
            check_cancel(cancelled)
            if not self.is_allowed(authority):
                continue
            children = sorted(root.iterdir())
            if len(children) > self.max_packages:
                raise SkillError("catalog_limit", authority)
            for folder in children:
                check_cancel(cancelled)
                if not folder.is_dir() or folder.is_symlink():
                    continue
                if not re.fullmatch(r"[a-zA-Z0-9_-]+", folder.name):
                    warnings.append(f"invalid_package_name:{authority}/{folder.name}")
                    continue
                try:
                    fields = self._metadata(folder)
                    entries.append(Entry(authority, folder.name, fields["name"],
                                         fields["description"], folder))
                except (SkillError, OSError, UnicodeError) as exc:
                    warnings.append(f"{authority}/{folder.name}:{exc}")
        check_cancel(cancelled)
        catalog = Catalog(step_id, tuple(entries), tuple(warnings))
        self.events.append({"event": "catalog_discovered", "step_id": step_id,
                            "count": len(entries), "warnings": len(warnings)})
        return catalog

    def select(self, catalog: Catalog, *, locator=None, name=None) -> Entry:
        if (locator is None) == (name is None):
            raise SkillError("selection_invalid", "provide one locator or name")
        matches = [e for e in catalog.entries
                   if (e.locator == locator if locator is not None else e.name == name)]
        if len(matches) != 1:
            raise SkillError("selection_ambiguous" if matches else "selection_missing",
                             locator or name)
        entry = matches[0]
        if not self.is_allowed(entry.authority):
            raise SkillError("source_denied", entry.authority)
        self.events.append({"event": "skill_selected", "step_id": catalog.step_id,
                            "locator": entry.locator})
        return entry

    def _read(self, entry: Entry, relative: str, cancelled) -> Fragment:
        check_cancel(cancelled)
        configured = self.roots.get(entry.authority)
        if configured is None or not self.is_allowed(entry.authority):
            raise SkillError("source_denied", entry.authority)
        # Reject fabricated Entry roots, not just forged locator strings.
        if entry.root != configured / entry.package or entry.root.is_symlink():
            raise SkillError("source_mismatch", entry.locator)
        target = safe_file(entry.root, relative)
        with target.open("rb") as stream:
            data = stream.read(self.max_bytes + 1)
        check_cancel(cancelled)
        if not self.is_allowed(entry.authority):
            raise SkillError("source_denied", entry.authority)
        if len(data) > self.max_bytes:
            raise SkillError("resource_limit", relative)
        try:
            text = data.decode("utf-8")
        except UnicodeError as exc:
            raise SkillError("invalid_utf8", relative) from exc
        fragment = Fragment(entry.locator, relative, sha256(data).hexdigest(), text)
        self.events.append({"event": "resource_read", "locator": entry.locator,
                            "resource": relative, "sha256": fragment.sha256,
                            "bytes": len(data)})
        return fragment

    def load(self, catalog: Catalog, entry: Entry, resources: tuple[str, ...] = (),
             cancelled=lambda: False) -> LoadBundle:
        if entry not in catalog.entries:
            raise SkillError("selection_missing", "entry does not belong to this step")
        # Staging is local; callers receive no fragments on any failure.
        try:
            staged = tuple(self._read(entry, path, cancelled)
                           for path in dict.fromkeys(("SKILL.md",) + resources))
            check_cancel(cancelled)
        except (SkillError, OSError) as exc:
            self.events.append({"event": "bundle_failed", "locator": entry.locator,
                                "error": getattr(exc, "code", type(exc).__name__)})
            raise
        self.events.append({"event": "bundle_committed", "step_id": catalog.step_id,
                            "locator": entry.locator, "count": len(staged)})
        return LoadBundle(catalog.step_id, staged)

    def read_page(self, entry: Entry, relative: str, byte_budget: int,
                  cursor=None, cancelled=lambda: False) -> Page:
        fragment = self._read(entry, relative, cancelled)
        identity = sha256((entry.locator + "\0" + relative + "\0" +
                           fragment.sha256).encode()).hexdigest()[:20]
        start = 0
        if cursor is not None:
            try:
                fingerprint, offset = cursor.split(":")
                start = int(offset)
                if fingerprint != identity or not 0 <= start <= len(fragment.text):
                    raise ValueError()
            except (ValueError, AttributeError) as exc:
                raise SkillError("invalid_cursor", "resource or contents changed") from exc
        resource = f"{entry.locator}/{relative}"
        # Character offsets avoid slicing a UTF-8 code point. Count serialized
        # JSON bytes, including cursor/escaping, not just the visible text.
        # A final page drops the cursor; check it before binary-searching the
        # monotonic non-final-page size (including numeric cursor width).
        final = Page(resource, fragment.text[start:], None)
        if len(final.wire().encode()) <= byte_budget:
            return final
        best = None
        lo, hi = start + 1, len(fragment.text) - 1
        while lo <= hi:
            end = (lo + hi) // 2
            page = Page(resource, fragment.text[start:end], f"{identity}:{end}")
            if len(page.wire().encode()) <= byte_budget:
                best, lo = page, end + 1
            else:
                hi = end - 1
        if best is None:
            raise SkillError("response_budget", "cannot fit one character and metadata")
        return best
