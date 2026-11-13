"""Teaching-only durable memory kernel. No model, credentials, or user memory access."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class Candidate:
    scope: str
    key: str
    text: str
    source: str
    source_version: str
    start: int
    end: int
    quote: str
    applies_at: str


class Rejected(Exception):
    pass


class Ledger:
    """SQLite owns commit, active view, operation identity, and forgotten-key barrier."""

    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, timeout=0, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS sources(
          source TEXT, version TEXT, body TEXT, PRIMARY KEY(source, version));
        CREATE TABLE IF NOT EXISTS heads(source TEXT PRIMARY KEY, version TEXT);
        CREATE TABLE IF NOT EXISTS memories(
          id INTEGER PRIMARY KEY, scope TEXT, key TEXT, text TEXT,
          applies_at TEXT, state TEXT, evidence TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS active_key ON memories(scope,key)
          WHERE state='active';
        CREATE TABLE IF NOT EXISTS forgotten(scope TEXT, key TEXT,
          reason TEXT, PRIMARY KEY(scope,key));
        CREATE TABLE IF NOT EXISTS operations(
          op TEXT PRIMARY KEY, payload TEXT, result TEXT);
        CREATE TABLE IF NOT EXISTS events(
          seq INTEGER PRIMARY KEY, op TEXT, kind TEXT, body TEXT);
        """)

    def close(self):
        self.db.close()

    def put_source(self, source: str, body: str) -> str:
        """Synthetic committed transcript snapshot; old versions remain addressable."""
        version = digest(body)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("INSERT OR IGNORE INTO sources VALUES(?,?,?)",
                            (source, version, body))
            self.db.execute("INSERT OR REPLACE INTO heads VALUES(?,?)", (source, version))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return version

    def _validate(self, c: Candidate):
        if not all((c.scope, c.key, c.text, c.source, c.applies_at)):
            raise Rejected("invalid_structure")
        row = self.db.execute(
            "SELECT s.body,h.version FROM sources s JOIN heads h USING(source) "
            "WHERE s.source=? AND s.version=?", (c.source, c.source_version)).fetchone()
        if row is None or row["version"] != c.source_version:
            raise Rejected("stale_or_missing_source")
        if digest(row["body"]) != c.source_version:
            raise Rejected("source_integrity_failure")
        lines = row["body"].splitlines()
        if not 1 <= c.start <= c.end <= len(lines):
            raise Rejected("invalid_source_range")
        if "\n".join(lines[c.start-1:c.end]) != c.quote:
            raise Rejected("source_quote_mismatch")
        # Provenance is checked. Entailment and current-world truth are NOT checked.

    def _run(self, op: str, payload: dict, change: Callable[[], dict],
             barrier: Callable[[], None] | None = None) -> dict:
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            old = self.db.execute("SELECT payload,result FROM operations WHERE op=?",
                                  (op,)).fetchone()
            if old:
                if old["payload"] != serialized:
                    raise Rejected("operation_id_reused_with_other_input")
                result = json.loads(old["result"])
            else:
                result = change()
                self.db.execute("INSERT INTO events(op,kind,body) VALUES(?,?,?)",
                                (op, result["status"], serialized))
                self.db.execute("INSERT INTO operations VALUES(?,?,?)",
                                (op, serialized, json.dumps(result)))
                if barrier:
                    barrier()  # Failure before COMMIT rolls the whole write back.
            self.db.execute("COMMIT")
            return result
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def merge(self, op: str, c: Candidate, barrier=None) -> dict:
        def change():
            self._validate(c)
            if self.db.execute("SELECT 1 FROM forgotten WHERE scope=? AND key=?",
                               (c.scope, c.key)).fetchone():
                return {"status": "blocked_by_forgetting"}
            old = self.db.execute("SELECT * FROM memories WHERE scope=? AND key=? "
                                  "AND state='active'", (c.scope, c.key)).fetchone()
            ev = asdict(c)
            if old and (old["text"], old["applies_at"]) == (c.text, c.applies_at):
                evidence = json.loads(old["evidence"])
                if ev not in evidence:
                    evidence.append(ev)
                self.db.execute("UPDATE memories SET evidence=? WHERE id=?",
                                (json.dumps(evidence, ensure_ascii=False), old["id"]))
                return {"status": "merged_support", "id": old["id"]}
            state = "conflict" if old else "active"
            cur = self.db.execute("INSERT INTO memories(scope,key,text,applies_at,state,evidence) "
                                  "VALUES(?,?,?,?,?,?)", (c.scope,c.key,c.text,c.applies_at,
                                  state,json.dumps([ev],ensure_ascii=False)))
            return {"status": state, "id": cur.lastrowid}
        return self._run(op, {"merge": asdict(c)}, change, barrier)

    def correct(self, op: str, expected_id: int, c: Candidate, request: str) -> dict:
        """Trusted explicit-control port: caller must establish user authorization."""
        def change():
            self._validate(c)
            if not request.strip():
                raise Rejected("missing_correction_reason")
            if self.db.execute("SELECT 1 FROM forgotten WHERE scope=? AND key=?",
                               (c.scope,c.key)).fetchone():
                raise Rejected("forgotten_requires_explicit_reinstate")
            old = self.db.execute("SELECT id FROM memories WHERE scope=? AND key=? "
                                  "AND state='active'",(c.scope,c.key)).fetchone()
            if not old or old["id"] != expected_id:
                raise Rejected("active_version_conflict")
            self.db.execute("UPDATE memories SET state='superseded' WHERE id=?",(expected_id,))
            cur = self.db.execute("INSERT INTO memories(scope,key,text,applies_at,state,evidence) "
                                  "VALUES(?,?,?,?,'active',?)",(c.scope,c.key,c.text,c.applies_at,
                                  json.dumps([asdict(c)],ensure_ascii=False)))
            return {"status":"corrected","id":cur.lastrowid,"supersedes":expected_id}
        return self._run(op,{"correct":asdict(c),"expected_id":expected_id,"request":request},change)

    def forget(self, op: str, scope: str, key: str, reason: str) -> dict:
        def change():
            if not reason.strip():
                raise Rejected("missing_forget_reason")
            self.db.execute("INSERT OR REPLACE INTO forgotten VALUES(?,?,?)",(scope,key,reason))
            self.db.execute("UPDATE memories SET state='forgotten' WHERE scope=? AND key=? "
                            "AND state IN ('active','conflict')",(scope,key))
            return {"status":"forgotten"}
        return self._run(op,{"forget":[scope,key,reason]},change)

    def project(self, scope: str, applies_at: str, query: str, max_bytes: int) -> dict:
        """Exact scope, lexical retrieval, whole-entry UTF-8 byte budget; no semantic ranker."""
        if max_bytes < 0:
            raise Rejected("negative_budget")
        selected, excluded, fragments, used = [], [], [], 0
        rows = self.db.execute("SELECT * FROM memories WHERE scope=? AND state='active' "
                               "ORDER BY id",(scope,)).fetchall()
        for row in rows:
            if query.casefold() not in row["text"].casefold():
                continue
            if row["applies_at"] != applies_at:
                excluded.append({"id":row["id"],"reason":"needs_revalidation"})
                continue
            fragment = json.dumps({"id":row["id"],"scope":scope,"text":row["text"],
                                  "evidence":json.loads(row["evidence"])},ensure_ascii=False)+"\n"
            size = len(fragment.encode())
            if used+size > max_bytes:
                excluded.append({"id":row["id"],"reason":"budget"})
                continue
            used += size
            selected.append(row["id"])
            fragments.append(fragment)
        return {"ids":selected,"excluded":excluded,"bytes":used,"context":"".join(fragments)}
