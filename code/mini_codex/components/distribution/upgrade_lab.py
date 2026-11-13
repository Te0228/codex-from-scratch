"""Chapter 30 storage/activation prototype; deliberately NOT a second Agent Loop.

Uses real files, SQLite, tar archives and child processes in a caller-owned lab
directory. POSIX, one host, cooperating writers; no installer/network/registry.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import uuid
import fcntl


class Refused(RuntimeError):
    pass


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text())


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextlib.contextmanager
def maintenance(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "maintenance.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def connect_ro(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def inspect_db(path):
    with connect_ro(path) as db:
        schema = db.execute("PRAGMA user_version").fetchone()[0]
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok" or schema not in (1, 2):
            raise Refused(f"invalid database: schema={schema}, integrity={integrity}")
        tasks = db.execute("SELECT id,title," + ("done" if schema == 1 else "status") + " FROM tasks ORDER BY id").fetchall()
        canonical_tasks = [(i, title, bool(value) if schema == 1 else value == "done") for i, title, value in tasks]
        return {
            "schema": schema,
            "tasks": canonical_tasks,
            "messages": db.execute("SELECT source_id,thread_id,ordinal,role,text FROM messages ORDER BY source_id,thread_id,ordinal").fetchall(),
            "memory": db.execute("SELECT source_id,id,text FROM memory ORDER BY source_id,id").fetchall(),
            "config": db.execute("SELECT key,value FROM config ORDER BY key").fetchall(),
            "sources": db.execute("SELECT source_id,format_version,content_sha256 FROM sources ORDER BY source_id").fetchall(),
        }


def validate_source(source):
    raw = Path(source).read_bytes()
    value = json.loads(raw)
    if value.get("format_version") != 1:
        raise Refused("unknown source format_version")
    allowed = {"format_version", "source_id", "tasks", "threads", "memory", "config"}
    unsupported = sorted(set(value) - allowed)
    if unsupported:
        raise Refused("unsupported fields: " + ", ".join(unsupported))
    if not isinstance(value.get("source_id"), str) or not value["source_id"]:
        raise Refused("source_id required")
    for name in ("tasks", "threads", "memory"):
        ids = [row["id"] for row in value[name]]
        if len(ids) != len(set(ids)):
            raise Refused(f"duplicate {name} identity")
    return value, hashlib.sha256(raw).hexdigest()


def preview(source, target=None):
    value, digest = validate_source(source)
    action = "import"
    if target is not None and Path(target).exists():
        with connect_ro(target) as db:
            existing = db.execute("SELECT content_sha256 FROM sources WHERE source_id=?", (value["source_id"],)).fetchone()
        if existing and existing[0] != digest:
            raise Refused("source identity already imported with different content")
        if existing:
            action = "already_imported"
    return {"source_id": value["source_id"], "source_sha256": digest, "format_version": 1, "action": action,
            "counts": {"tasks": len(value["tasks"]), "threads": len(value["threads"]),
                       "messages": sum(len(t["messages"]) for t in value["threads"]), "memory": len(value["memory"])}}


def import_source(source, target):
    """Standalone initial import, then idempotent re-import; not a multi-store importer."""
    target = Path(target)
    plan = preview(source, target)
    if plan["action"] == "already_imported":
        return plan
    if target.exists():
        raise Refused("this bounded initial importer requires an empty target")
    value, digest = validate_source(source)
    if digest != plan["source_sha256"]:
        raise Refused("source changed after preview")
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(target.name + ".preparing")
    if staged.exists():
        raise Refused("unfinished import staging requires inspection")
    try:
        with sqlite3.connect(staged) as db:
            db.executescript("""
                PRAGMA journal_mode=DELETE;
                CREATE TABLE sources(source_id TEXT PRIMARY KEY, format_version INTEGER, content_sha256 TEXT);
                CREATE TABLE tasks(id TEXT PRIMARY KEY, title TEXT, done INTEGER CHECK(done IN (0,1)));
                CREATE TABLE messages(source_id TEXT,thread_id TEXT,ordinal INTEGER,role TEXT,text TEXT,
                  PRIMARY KEY(source_id,thread_id,ordinal));
                CREATE TABLE memory(source_id TEXT,id TEXT,text TEXT,PRIMARY KEY(source_id,id));
                CREATE TABLE config(key TEXT PRIMARY KEY,value TEXT);
                PRAGMA user_version=1;
            """)
            db.execute("INSERT INTO sources VALUES(?,?,?)", (value["source_id"],1,digest))
            db.executemany("INSERT INTO tasks VALUES(?,?,?)", [(r["id"],r["title"],int(r["done"])) for r in value["tasks"]])
            db.executemany("INSERT INTO messages VALUES(?,?,?,?,?)", [(value["source_id"],t["id"],i,m["role"],m["text"]) for t in value["threads"] for i,m in enumerate(t["messages"])])
            db.executemany("INSERT INTO memory VALUES(?,?,?)", [(value["source_id"],m["id"],m["text"]) for m in value["memory"]])
            db.executemany("INSERT INTO config VALUES(?,?)", [(k,json.dumps(v,ensure_ascii=False,sort_keys=True)) for k,v in value["config"].items()])
        inspect_db(staged)
        if sha(source) != digest:
            raise Refused("source changed before import publication")
        # Same-filesystem no-clobber publication. Recovery can inspect a leftover
        # staging file, but this initial-import helper is not the full LC gate.
        os.link(staged, target)
        sync_dir(target.parent)
    finally:
        staged.unlink(missing_ok=True)
    return plan


APP = '''import json, pathlib, sqlite3, subprocess, sys
PROGRAM_VERSION = __PROGRAM_VERSION__
SUPPORTED_SCHEMA = __SUPPORTED_SCHEMA__
package = pathlib.Path(__file__).resolve().parents[1]
manifest = json.loads((package / "manifest.json").read_text())
if manifest["version"] != PROGRAM_VERSION:
    raise SystemExit("manifest/program version mismatch")
hello = json.loads(subprocess.check_output([sys.executable, "-I", str(package / "helpers" / "inspect_helper.py")]))
if hello.get("protocol") != manifest["helper_protocol"] or "inspect" not in hello.get("capabilities", []):
    raise SystemExit("helper protocol/capability mismatch")
db = sqlite3.connect(sys.argv[1])
schema = db.execute("PRAGMA user_version").fetchone()[0]
if schema not in SUPPORTED_SCHEMA or not manifest["data_read_min"] <= schema <= manifest["data_read_max"]:
    raise SystemExit("unsupported data schema")
if len(sys.argv) > 2:
    if schema != 2: raise SystemExit("writer requires schema 2")
    db.execute("INSERT INTO tasks VALUES(?,?,?)", ("user-after-upgrade", sys.argv[2], "todo"))
    db.commit()
print(json.dumps({"program_version": PROGRAM_VERSION, "schema": schema,
    "task_count": db.execute("SELECT count(*) FROM tasks").fetchone()[0], "helper_protocol": hello["protocol"]}))
db.close()
'''


def build_package(destination, version, *, helper_protocol=1, omit_helper=False, executable=True, manifest_version=None):
    """A diagnostic payload only; final integration must pack the actual checkpoint."""
    destination = Path(destination)
    app = APP.replace("__PROGRAM_VERSION__",repr(version)).replace("__SUPPORTED_SCHEMA__",repr((1,) if version=="1.0.0" else (2,)))
    files = {"bin/inspect_app.py": app.encode()}
    if not omit_helper:
        files["helpers/inspect_helper.py"] = ("import json\nprint(json.dumps(" + repr({"protocol":helper_protocol,"capabilities":["inspect"]}) + "))\n").encode()
    manifest = {"layout_version":1,"version":manifest_version or version,"target":"posix-python-lab",
                "entrypoint":"bin/inspect_app.py", "required_helper":"helpers/inspect_helper.py",
                "helper_protocol":1,"data_read_min":1 if version=="1.0.0" else 2,
                "data_read_max":1 if version=="1.0.0" else 2,
                "files":{k:hashlib.sha256(v).hexdigest() for k,v in files.items()}}
    files["manifest.json"] = encoded(manifest)
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tarfile.open(destination,"w:gz") as tar:
        for name, data in sorted(files.items()):
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            entry.mode = 0o755 if name=="bin/inspect_app.py" or (executable and name.endswith(".py")) else 0o644
            entry.mtime = 0
            tar.addfile(entry, io.BytesIO(data))
    return sha(destination)


def verify_package(package):
    package = Path(package)
    manifest = read_json(package / "manifest.json")
    if manifest["layout_version"] != 1 or manifest["target"] != "posix-python-lab":
        raise Refused("unknown package layout or target")
    for name in (manifest["entrypoint"],manifest["required_helper"]):
        if name not in manifest["files"]:
            raise Refused("missing declared helper/entrypoint: " + name)
        if not (package/name).is_file() or not (package/name).stat().st_mode & 0o111:
            raise Refused("missing/nonexecutable package dependency: " + name)
    for name, expected in manifest["files"].items():
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or not (package/path).is_file() or sha(package/path) != expected:
            raise Refused("package file integrity failure: " + name)
    return manifest


def unpack(archive, expected_sha, destination):
    if sha(archive) != expected_sha:
        raise Refused("archive checksum mismatch")
    destination = Path(destination)
    destination.mkdir(parents=True,exist_ok=False)
    with tarfile.open(archive,"r:gz") as tar:
        seen=set()
        for member in tar.getmembers():
            path = Path(member.name)
            if not member.isfile() or path.is_absolute() or ".." in path.parts or member.name in seen:
                raise Refused("unsafe archive member")
            seen.add(member.name)
            out=destination/path
            out.parent.mkdir(parents=True,exist_ok=True)
            with tar.extractfile(member) as stream:
                out.write_bytes(stream.read())
            out.chmod(member.mode & 0o777)
    return verify_package(destination)


def smoke(package, database, cwd, write=None):
    manifest=verify_package(package)
    args=[sys.executable,"-I",str(Path(package).resolve()/manifest["entrypoint"]),str(Path(database).resolve())]
    if write is not None:
        args.append(write)
    result=subprocess.run(args,cwd=cwd,capture_output=True,text=True,env={"PATH":os.defpath},timeout=15)
    if result.returncode:
        raise Refused(f"installed child exit {result.returncode}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def bootstrap(root, source, archive, expected_sha):
    root=Path(root)
    with maintenance(root):
        if (root/"active.json").exists():
            raise Refused("bootstrap target already installed")
        package=root/"packages"/"initial"
        manifest=unpack(archive,expected_sha,package)
        db=root/"data"/"initial.sqlite"
        import_source(source,db)
        smoke(package,db,root)
        active={"tx":"initial","package":"packages/initial","database":"data/initial.sqlite","version":manifest["version"],"schema":1}
        atomic_json(root/"active.json",active)
        return active


def migrate_candidate(source_db, target_db):
    before=inspect_db(source_db)
    if before["schema"] != 1:
        raise Refused("only schema 1 to 2 is implemented")
    with connect_ro(source_db) as source, sqlite3.connect(target_db) as candidate:
        source.backup(candidate)
        with candidate:
            candidate.execute("BEGIN IMMEDIATE")
            candidate.execute("ALTER TABLE tasks RENAME TO tasks_v1")
            candidate.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY,title TEXT,status TEXT CHECK(status IN ('todo','done')))")
            candidate.execute("INSERT INTO tasks SELECT id,title,CASE done WHEN 1 THEN 'done' ELSE 'todo' END FROM tasks_v1")
            candidate.execute("DROP TABLE tasks_v1")
            candidate.execute("PRAGMA user_version=2")
    verify_migration(before,target_db)


def verify_migration(before, candidate_db):
    after=inspect_db(candidate_db)
    if after["schema"] != 2 or {k:v for k,v in before.items() if k!="schema"} != {k:v for k,v in after.items() if k!="schema"}:
        raise Refused("candidate data semantic verification failed")
    return after


def checkpoint(name, fault):
    if fault == name:
        # Intentional process death: bypass Python cleanup and release locks by OS.
        os._exit(86)
    if fault == "fail:"+name:
        raise Refused("injected failure: " + name)


def upgrade(root, archive, expected_sha, *, fault=None, cancel=False, corrupt_candidate=False):
    root=Path(root)
    with maintenance(root):
        old=read_json(root/"active.json")
        tx=uuid.uuid4().hex
        staging=root/"staging"/tx
        staging.mkdir(parents=True)
        journal={"tx":tx,"phase":"PREPARING","old":old,"old_sha":sha(root/old["database"]),
                 "package":f"packages/{tx}","database":f"data/{tx}.sqlite","archive_sha256":expected_sha}
        journal_file=root/"transactions"/(tx+".json")
        atomic_json(journal_file,journal)
        manifest=unpack(archive,expected_sha,root/journal["package"])
        before=inspect_db(root/old["database"])
        candidate=staging/"candidate.sqlite"
        migrate_candidate(root/old["database"],candidate)
        if corrupt_candidate:
            with sqlite3.connect(candidate) as db:
                db.execute("DELETE FROM tasks WHERE id=(SELECT min(id) FROM tasks)")
        verify_migration(before,candidate)
        report=smoke(root/journal["package"],candidate,root)
        journal.update(phase="PREPARED",candidate_sha=sha(candidate),version=manifest["version"],schema=report["schema"])
        atomic_json(journal_file,journal)
        checkpoint("prepared",fault)
        if cancel:
            shutil.rmtree(staging)
            journal["phase"]="CANCELLED"
            atomic_json(journal_file,journal)
            return {"status":"cancelled","active":old,"tx":tx}
        # Generation publication and active selection are separate durable steps.
        os.rename(candidate,root/journal["database"])
        sync_dir(root/"data")
        checkpoint("data_published_before_journal",fault)
        journal["phase"]="DATA_READY"
        atomic_json(journal_file,journal)
        checkpoint("before_activation",fault)
        if read_json(root/"active.json") != old or sha(root/old["database"]) != journal["old_sha"]:
            raise Refused("old state changed during maintenance")
        active={"tx":tx,"package":journal["package"],"database":journal["database"],"version":manifest["version"],"schema":report["schema"]}
        atomic_json(root/"active.json",active)
        checkpoint("activated_before_ack",fault)
        journal["phase"]="COMMITTED"
        atomic_json(journal_file,journal)
        return {"status":"updated","active":active,"tx":tx}


def recover(root):
    root=Path(root)
    decisions=[]
    with maintenance(root):
        active=read_json(root/"active.json")
        for path in sorted((root/"transactions").glob("*.json")):
            j=read_json(path)
            if j["phase"] in ("COMMITTED","CANCELLED","ABORTED","ROLLED_BACK"):
                continue
            if active["tx"] == j["tx"]:
                if active["package"]!=j["package"] or active["database"]!=j["database"] or sha(root/j["database"])!=j["candidate_sha"]:
                    raise Refused("activated state changed; requires explicit recovery")
                smoke(root/j["package"],root/j["database"],root)
                j["phase"]="COMMITTED"
            elif active == j["old"] and sha(root/active["database"])==j["old_sha"]:
                # Never infer success from the existence of a candidate database.
                j["phase"]="ABORTED"
            else:
                raise Refused("activation belongs to another state; do not overwrite")
            atomic_json(path,j)
            decisions.append({"tx":j["tx"],"decision":j["phase"]})
    return decisions


def run_installed(root, write=None):
    root=Path(root)
    with maintenance(root):
        active=read_json(root/"active.json")
        journal=root/"transactions"/(active["tx"]+".json")
        if journal.exists() and read_json(journal)["phase"] != "COMMITTED":
            raise Refused("recover activated transaction before admitting user writes")
        report=smoke(root/active["package"],root/active["database"],root,write)
        if report["program_version"]!=active["version"] or report["schema"]!=active["schema"]:
            raise Refused("active record disagrees with observed package/database")
        return report


def rollback(root, tx):
    root=Path(root)
    with maintenance(root):
        jpath=root/"transactions"/(tx+".json")
        j=read_json(jpath)
        active=read_json(root/"active.json")
        if j["phase"]!="COMMITTED" or active["tx"]!=tx:
            raise Refused("rollback does not own current activation")
        if sha(root/active["database"])!=j["candidate_sha"]:
            raise Refused("post-upgrade user writes exist; automatic rollback refused")
        if sha(root/j["old"]["database"])!=j["old_sha"]:
            raise Refused("old data generation changed")
        smoke(root/j["old"]["package"],root/j["old"]["database"],root)
        atomic_json(root/"active.json",j["old"])
        j["phase"]="ROLLED_BACK"
        atomic_json(jpath,j)
        return {"status":"rolled_back","active":j["old"]}


if __name__ == "__main__":
    if len(sys.argv)!=6 or sys.argv[1]!="crash-upgrade":
        raise SystemExit("use run_checks.py, or crash-upgrade ROOT ARCHIVE SHA FAULT")
    upgrade(Path(sys.argv[2]),Path(sys.argv[3]),sys.argv[4],fault=sys.argv[5])
