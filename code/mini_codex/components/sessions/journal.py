"""Teaching storage prototype only. No model, dispatcher, tool replay, or Codex runtime."""
from __future__ import annotations
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


class Conflict(ValueError):
    pass


class Journal:
    """One logical producer; SQLite serializes commits and head revisions reject stale writers."""
    def __init__(self, path: Path, *, check_same_thread=True):
        # Runtime History serializes all access under its RLock when false.
        self.db = sqlite3.connect(path, isolation_level=None, timeout=0.1,
                                  check_same_thread=check_same_thread)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA journal_mode=DELETE")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            raise ValueError("unsupported journal schema")
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS events (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
              body TEXT NOT NULL, checksum TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS heads (
              branch TEXT PRIMARY KEY, revision INTEGER NOT NULL, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS versions (
              branch TEXT NOT NULL, revision INTEGER NOT NULL, state TEXT NOT NULL,
              PRIMARY KEY (branch,revision));
            CREATE TABLE IF NOT EXISTS requests (
              request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, receipt TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS meta (singleton INTEGER PRIMARY KEY, generation INTEGER NOT NULL);
            INSERT OR IGNORE INTO meta VALUES (1,0);
            PRAGMA user_version=1;
        ''')
        state = encoded(dict(visible=[], source=None, archived=False, deleted=False))
        with self.transaction():
            self.db.execute("INSERT OR IGNORE INTO heads VALUES ('main',0,?)", (state,))
            self.db.execute("INSERT OR IGNORE INTO versions VALUES ('main',0,?)", (state,))
        self.pending = []
        self.pending_branch = None
        self.pending_revision = None

    def transaction(self):
        class Transaction:
            def __enter__(inner):
                self.db.execute("BEGIN IMMEDIATE")
            def __exit__(inner, kind, value, traceback):
                if kind:
                    self.db.execute("ROLLBACK")
                else:
                    try:
                        self.db.execute("COMMIT")
                    except BaseException:
                        if self.db.in_transaction:
                            self.db.execute("ROLLBACK")
                        raise
        return Transaction()

    def head(self, branch):
        row = self.db.execute("SELECT revision,state FROM heads WHERE branch=?", (branch,)).fetchone()
        if row is None:
            raise KeyError(branch)
        return row[0], json.loads(row[1])

    def _publish(self, branch, revision, state):
        value = encoded(state)
        self.db.execute("INSERT INTO versions VALUES (?,?,?)", (branch, revision, value))
        self.db.execute("INSERT INTO heads VALUES (?,?,?) ON CONFLICT(branch) DO UPDATE SET revision=excluded.revision,state=excluded.state", (branch, revision, value))
        self.db.execute("UPDATE meta SET generation=generation+1 WHERE singleton=1")

    def append(self, kind, payload, items=(), *, branch="main"):
        revision, state = self.head(branch)
        if state["deleted"] or state["archived"]:
            raise Conflict("branch is not writable")
        if self.pending and (branch != self.pending_branch or revision != self.pending_revision):
            raise Conflict("pending batch no longer matches branch head")
        self.pending_branch, self.pending_revision = branch, revision
        event_id = str(uuid.uuid4())
        body = dict(event_id=event_id, kind=kind, payload=payload, items=list(items))
        # Canonical serialization detaches caller-owned dictionaries now, before any I/O.
        self.pending.append((event_id, encoded(body), digest(body)))
        return event_id

    def flush(self, *, fail_at=None):
        if not self.pending:
            return self.watermark()
        with self.transaction():
            revision, state = self.head(self.pending_branch)
            if revision != self.pending_revision:
                raise Conflict("stale head: retry requires explicit reconciliation")
            if fail_at == "write":
                raise OSError("injected record-write failure")
            for row in self.pending:
                self.db.execute("INSERT INTO events(event_id,body,checksum) VALUES (?,?,?)", row)
            state["visible"].extend(row[0] for row in self.pending)
            self._publish(self.pending_branch, revision + 1, state)
            if fail_at == "before_commit":
                raise OSError("injected pre-COMMIT failure")
        self.pending.clear()
        self.pending_branch = self.pending_revision = None
        return self.watermark()

    def watermark(self):
        return dict(commit_generation=self.db.execute("SELECT generation FROM meta").fetchone()[0],
                    committed_seq=self.db.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0])

    def replay(self, branch="main"):
        revision, state = self.head(branch)
        if state["deleted"]:
            raise Conflict("deleted branch cannot resume")
        events = []
        for event_id in state["visible"]:
            row = self.db.execute("SELECT body,checksum FROM events WHERE event_id=?", (event_id,)).fetchone()
            if row is None:
                raise ValueError("visible event missing")
            body = json.loads(row[0])
            if digest(body) != row[1] or body["event_id"] != event_id:
                raise ValueError("canonical event checksum mismatch")
            events.append(body)
        return events

    def snapshot(self, branch="main"):
        revision, state = self.head(branch)
        events = self.replay(branch)
        items = [item for event in events for item in event["items"]]
        return dict(branch=branch, revision=revision, visible=state["visible"],
                    snapshot_id=digest(items), raw_items=items, **self.watermark())

    def timeline(self, request_id, action, branch, expected_revision, *, child=None, cutoff=None):
        """Atomic metadata mutation and receipt. Does not touch workspace files."""
        if self.pending:
            raise Conflict("commit or discard pending events before a timeline mutation")
        fingerprint = digest([action, branch, expected_revision, child, cutoff])
        with self.transaction():
            previous = self.db.execute("SELECT fingerprint,receipt FROM requests WHERE request_id=?", (request_id,)).fetchone()
            if previous:
                if previous[0] != fingerprint:
                    raise Conflict("request ID reused for different operation")
                return json.loads(previous[1])
            revision, state = self.head(branch)
            if revision != expected_revision or state["deleted"]:
                raise Conflict("stale or deleted branch")
            if action == "fork":
                if not child or self.db.execute("SELECT 1 FROM heads WHERE branch=?", (child,)).fetchone():
                    raise Conflict("child must be new")
                target, next_revision = child, 0
                state["source"] = dict(branch=branch, revision=revision)
                state["archived"] = False
            elif action == "revert":
                if not isinstance(cutoff, int) or not 0 <= cutoff <= len(state["visible"]):
                    raise ValueError("invalid cutoff")
                target, next_revision = branch, revision + 1
                state["visible"] = state["visible"][:cutoff]
            elif action in ("archive", "delete"):
                target, next_revision = branch, revision + 1
                state["archived" if action == "archive" else "deleted"] = True
            else:
                raise ValueError(action)
            self._publish(target, next_revision, state)
            receipt = dict(request_id=request_id, action=action, branch=target, revision=next_revision)
            self.db.execute("INSERT INTO requests VALUES (?,?,?)", (request_id, fingerprint, encoded(receipt)))
        return receipt

    def unknown_side_effects(self):
        # Operation evidence survives chat visibility changes; all branches share this workspace.
        events = []
        for body, checksum in self.db.execute("SELECT body,checksum FROM events ORDER BY seq"):
            event = json.loads(body)
            if digest(event) != checksum:
                raise ValueError("canonical event checksum mismatch")
            events.append(event)
        completed = {e["payload"]["call_id"] for e in events if e["kind"] == "tool_result"}
        return [dict(call_id=e["payload"]["call_id"], status="unknown") for e in events
                if e["kind"] == "tool_intent" and e["payload"]["call_id"] not in completed]

    def close(self):
        self.db.close()
