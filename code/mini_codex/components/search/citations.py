"""Executable teaching prototype; NOT the Codex or alpha/search wire format.

Only result type teaching_text_v1 has an explicit adapter. Provider output and
unknown result types stay data. This module makes no HTTP or model calls.
"""
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
import json
from urllib.parse import urlsplit


class EvidenceError(ValueError):
    pass


def required_text(value, code):
    if not isinstance(value, str) or not value:
        raise EvidenceError(code)
    return value


def web_url(value):
    required_text(value, "invalid_url")
    try:
        parts = urlsplit(value)
        valid = parts.scheme in {"http", "https"} and parts.hostname and not parts.username and not parts.password
    except ValueError:
        valid = False
    if not valid or any(ord(c) < 33 for c in value):
        raise EvidenceError("invalid_url")
    return value


def timestamp(value):
    required_text(value, "invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise EvidenceError("invalid_timestamp") from error
    if parsed.tzinfo is None:
        raise EvidenceError("timestamp_without_timezone")
    return value


@dataclass(frozen=True)
class SourceKey:
    session_id: str
    request_id: str
    ref_id: str


@dataclass(frozen=True)
class Source:
    key: SourceKey
    url: str
    title: str
    acquired_at: str  # local acquisition time, not publication time
    kind: str        # snippet or page_window; never silently upgraded
    line_start: int
    text: str
    content_digest: str


@dataclass(frozen=True)
class Citation:
    source: SourceKey
    content_digest: str
    char_start: int   # Python Unicode code-point offsets, half-open interval
    char_end: int
    line_start: int
    line_end: int
    quote: str


@dataclass(frozen=True)
class Observation:
    request_id: str
    status: str
    output: str
    result_state: str  # absent, empty, present (transport error is separate)
    sources: tuple[SourceKey, ...]
    opaque_results: tuple[dict, ...]
    error_code: str | None = None


class CitationTable:
    def __init__(self, session_id):
        self.session_id = required_text(session_id, "invalid_session")
        self._pending = set()
        self._seen = set()
        self._sources = {}
        self._observations = {}
        self.closed = False

    def begin(self, request_id):
        required_text(request_id, "invalid_request")
        if self.closed:
            raise EvidenceError("session_closed")
        if request_id in self._seen:
            raise EvidenceError("duplicate_request")
        self._seen.add(request_id)
        self._pending.add(request_id)

    def cancel(self, request_id):
        self._pending.discard(request_id)

    def close(self):
        self.closed = True
        self._pending.clear()

    def ingest(self, request_id, raw_response, acquired_at):
        # Caller owns transport/request identity; page-supplied text never selects it.
        if self.closed or request_id not in self._pending:
            raise EvidenceError("late_or_unknown_request")
        if not isinstance(raw_response, bytes) or len(raw_response) > 1_000_000:
            raise EvidenceError("invalid_response_size")
        acquired_at = timestamp(acquired_at)
        try:
            raw = json.loads(raw_response)
        except (ValueError, UnicodeDecodeError) as error:
            raise EvidenceError("malformed_response") from error
        if not isinstance(raw, dict) or not isinstance(raw.get("output"), str):
            raise EvidenceError("invalid_output")
        rows = raw.get("results")
        if rows is not None and not isinstance(rows, list):
            raise EvidenceError("invalid_results")
        result_state = "absent" if rows is None else "empty" if not rows else "present"
        staged, opaque = {}, []
        for row in rows or []:
            if not isinstance(row, dict):
                raise EvidenceError("invalid_result")
            if row.get("type") != "teaching_text_v1":
                opaque.append(row)
                continue
            ref = required_text(row.get("ref_id"), "missing_ref")
            key = SourceKey(self.session_id, request_id, ref)
            if key in staged:
                raise EvidenceError("duplicate_ref")
            kind = row.get("kind")
            if kind not in {"snippet", "page_window"}:
                raise EvidenceError("unsupported_content_kind")
            line_start = row.get("line_start")
            if type(line_start) is not int or line_start < 1:
                raise EvidenceError("invalid_line_start")
            body = required_text(row.get("text"), "missing_content")
            title = required_text(row.get("title"), "missing_title")
            staged[key] = Source(key, web_url(row.get("url")), title, acquired_at,
                                 kind, line_start, body, sha256(body.encode()).hexdigest())
        observation = Observation(request_id, "ok", raw["output"], result_state,
                                  tuple(staged), tuple(opaque))
        # Validate the complete response before publishing any source.
        self._sources.update(staged)
        self._observations[request_id] = observation
        self._pending.remove(request_id)
        return observation

    def fail(self, request_id, code):
        if self.closed or request_id not in self._pending:
            raise EvidenceError("late_or_unknown_request")
        observation = Observation(request_id, "failed", "", "absent", (), (),
                                  required_text(code, "missing_error"))
        self._observations[request_id] = observation
        self._pending.remove(request_id)
        return observation

    def source(self, key):
        try:
            return self._sources[key]
        except KeyError as error:
            raise EvidenceError("unknown_source") from error

    def find(self, key, pattern):
        source = self.source(key)
        required_text(pattern, "empty_pattern")
        result, offset = [], 0
        while (start := source.text.find(pattern, offset)) >= 0:
            end = start + len(pattern)
            result.append(Citation(key, source.content_digest, start, end,
                source.line_start + source.text.count("\n", 0, start),
                source.line_start + source.text.count("\n", 0, end - 1), pattern))
            offset = start + 1  # overlapping occurrences are not discarded
        return tuple(result)

    def resolve_citation(self, citation, *, require_page=True):
        source = self.source(citation.source)
        if require_page and source.kind != "page_window":
            raise EvidenceError("page_not_opened")
        if citation.content_digest != source.content_digest:
            raise EvidenceError("content_version_mismatch")
        if (type(citation.char_start) is not int or type(citation.char_end) is not int or
                not 0 <= citation.char_start < citation.char_end <= len(source.text)):
            raise EvidenceError("invalid_span")
        start, end = citation.char_start, citation.char_end
        if citation.quote != source.text[start:end]:
            raise EvidenceError("quote_mismatch")
        if (citation.line_start != source.line_start + source.text.count("\n", 0, start) or
                citation.line_end != source.line_start + source.text.count("\n", 0, end - 1)):
            raise EvidenceError("line_mismatch")
        return {"source": asdict(source.key), "url": source.url, "title": source.title,
                "acquired_at": source.acquired_at, "content_kind": source.kind,
                "content_digest": source.content_digest,
                "line_start": citation.line_start, "line_end": citation.line_end,
                "quote": citation.quote, "support_status": "exact_span_only",
                "trust": "external_data"}
