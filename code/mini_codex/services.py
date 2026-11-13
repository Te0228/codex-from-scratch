"""Services used by the same Engine: persistent model evidence and local gates.

SQLite owns accepted observations. JSONL is a regenerated inspection projection,
not a second state owner. No prompts, headers, model options containing credentials,
or output text are persisted here. Raw usage and its observed presence are retained.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, is_dataclass
from functools import wraps
import hashlib
import json
from pathlib import Path
import sqlite3
import threading

from .components.telemetry import meter
from .components.cache import cache_stats
from .components.assembly import system_audit


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value):
    return hashlib.sha256(_encoded(value).encode()).hexdigest()


def _synchronized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


class ServiceHub:
    """Single-controller local service owner; no implicit external connections."""

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / "services.sqlite"
        self.journal_path = self.state_dir / "model-usage.jsonl"
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS model_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT UNIQUE NOT NULL, payload_sha256 TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS model_conflicts (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL, payload TEXT NOT NULL, reason TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_results (
                run_id TEXT PRIMARY KEY, payload TEXT NOT NULL
            );
        """)
        self.db.commit()
        self.closed = False
        self._write_projection()

    def _check_open(self):
        if self.closed:
            raise RuntimeError("service_hub_closed")

    def _events(self):
        self._check_open()
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT payload FROM model_events ORDER BY sequence")]

    def _write_projection(self):
        # If interrupted after SQLite commit, reopen regenerates the entire view.
        rows = self.db.execute("SELECT payload FROM model_events ORDER BY sequence").fetchall()
        temporary = self.journal_path.with_suffix(".jsonl.next")
        temporary.write_text("".join(row[0] + "\n" for row in rows), encoding="utf-8")
        temporary.replace(self.journal_path)

    @staticmethod
    def _payload(request, result):
        ctx, options = request.call_context, request.model_options
        mode = options.get("mode", "unknown")
        if mode not in {"live", "replay", "fixture"}:
            mode = "unknown"
        provider = str(options.get("provider", "unknown"))
        model = str(options.get("model", "unknown"))
        usage = result.usage
        presence = getattr(result, "usage_presence", "not_observable")
        if presence in {None, "not_observable", "presence_not_observable"}:
            # An actual object still establishes presence; None alone cannot
            # distinguish omitted usage from explicit null in the upstream body.
            presence = "object" if isinstance(usage, dict) else "not_observable"
        if presence not in {"missing", "null", "object", "invalid", "not_observable"}:
            raise ValueError("invalid_usage_presence")
        if ((presence in {"missing", "null"} and usage is not None) or
                (presence == "object" and not isinstance(usage, dict)) or
                (presence == "invalid" and (usage is None or isinstance(usage, dict)))):
            raise ValueError("usage_presence_conflicts_with_payload")
        response_id = getattr(result, "response_id", None)
        if response_id is not None and (not isinstance(response_id, str) or not response_id):
            raise ValueError("invalid_provider_response_id")
        event_key = _encoded([mode, provider, ctx.run_id, ctx.turn_id, ctx.sampling_id])
        semantics_known = provider == "openai"
        return {
            "schema": "mini-codex-model-usage-v1", "event_key": event_key,
            "mode": mode, "provider": provider, "model": model,
            "run_id": ctx.run_id, "turn_id": ctx.turn_id, "sampling_id": ctx.sampling_id,
            "attempt_id": _encoded([ctx.run_id, ctx.turn_id, ctx.sampling_id]),
            "history_snapshot_id": request.history_snapshot_id,
            "adapter_request_id": result.request_id,
            "request_id_kind": "http_header" if mode == "live" else "adapter_local_or_unknown",
            "response_id": response_id,
            "identity_basis": "provider_response_id" if response_id else "local_sampling_id",
            "response_status": result.response_status, "provider_error": result.provider_error,
            "usage_presence": presence, "raw_usage": usage,
            "input_semantics": "responses_inclusive_v1" if semantics_known else "unverified_provider_semantics",
            "evidence_kind": mode,
        }

    @staticmethod
    def _observation(payload):
        status = payload["response_status"]
        terminal = {"completed": "response.completed", "incomplete": "response.incomplete",
                    "failed": "response.failed"}.get(status, "transport.failed")
        identity = dict(event_id=payload["event_key"], task_id=payload["run_id"],
            thread_id=payload["run_id"], parent_thread_id=None, root_turn_id=payload["turn_id"],
            attempt_id=payload["attempt_id"], provider_scope=_encoded([payload["mode"], payload["provider"]]),
            model=payload["model"], purpose="agent_sampling", evidence_ref="model-usage.jsonl",
            evidence_kind=payload["mode"], input_semantics=payload["input_semantics"])
        if payload["usage_presence"] == "not_observable":
            return meter.observe_numeric_only(response_id=payload["response_id"], **identity)
        response = {}
        if payload["response_id"] is not None:
            response["id"] = payload["response_id"]
        if payload["usage_presence"] != "missing":
            response["usage"] = payload["raw_usage"]
        return meter.observe_raw({"type": terminal, "response": response}, **identity)

    @_synchronized
    def on_model(self, request, result):
        """Called for every actual sampling result, including failures and replay."""
        self._check_open()
        payload = self._payload(request, result)
        # Validate the parser before persisting; never store arbitrary model options.
        self._observation(payload)
        encoded, digest = _encoded(payload), _sha(payload)
        old = self.db.execute("SELECT payload_sha256 FROM model_events WHERE event_key=?",
                              (payload["event_key"],)).fetchone()
        if old:
            if old[0] == digest:
                return {"status": "duplicate_event", "identity_basis": payload["identity_basis"]}
            with self.db:
                self.db.execute("INSERT INTO model_conflicts(event_key,payload,reason) VALUES(?,?,?)",
                    (payload["event_key"], encoded, "same_sampling_changed_fact"))
            raise meter.Conflict("same_sampling_changed_fact")
        with self.db:
            self.db.execute("INSERT INTO model_events(event_key,payload_sha256,payload) VALUES(?,?,?)",
                            (payload["event_key"], digest, encoded))
        self._write_projection()
        return {"status": "recorded", "identity_basis": payload["identity_basis"],
                "usage_presence": payload["usage_presence"]}

    @_synchronized
    def on_run_result(self, result, run_dir=None):
        self._check_open()
        payload = asdict(result) if is_dataclass(result) else dict(result)
        if not payload.get("run_id"):
            raise ValueError("run_id_required")
        if run_dir is not None:
            payload["run_dir"] = str(Path(run_dir).resolve())
        with self.db:
            self.db.execute("INSERT INTO run_results(run_id,payload) VALUES(?,?) "
                            "ON CONFLICT(run_id) DO UPDATE SET payload=excluded.payload",
                            (payload["run_id"], _encoded(payload)))

    @_synchronized
    def report(self):
        """Observed counters; no inferred prices, fabricated usage or cache hits."""
        self._check_open()
        events = self._events()
        conflicts = [dict(event_key=row[0], payload=json.loads(row[1]), reason=row[2])
                     for row in self.db.execute("SELECT event_key,payload,reason FROM model_conflicts ORDER BY sequence")]
        runs = {row[0]: json.loads(row[1]) for row in self.db.execute("SELECT run_id,payload FROM run_results")}
        scopes = {}
        for mode in sorted({event["mode"] for event in events}):
            selected = [event for event in events if event["mode"] == mode]
            ledger = meter.Ledger()
            conflict_reasons = []
            for event in selected:
                try:
                    ledger.record(self._observation(event))
                except meter.Conflict as error:
                    conflict_reasons.append(str(error))
            for conflict in conflicts:
                if conflict["payload"]["mode"] == mode:
                    observation = self._observation(conflict["payload"])
                    ledger.conflicts.add(observation.usage_key())
                    original = next((event for event in selected
                                     if event["event_key"] == conflict["event_key"]), None)
                    if original is not None:
                        ledger.conflicts.add(self._observation(original).usage_key())
                    conflict_reasons.append(conflict["reason"])
            task_reports = {}
            for run_id in sorted({event["run_id"] for event in selected}):
                terminal = runs.get(run_id)
                complete = bool(terminal and terminal.get("task_status") in
                                {"passed", "failed", "blocked", "cancelled", "unknown"})
                task_reports[run_id] = ledger.query(run_id, {}, inventory_complete=complete)
            # Repeated delivery of one provider response is one fact. Distinct
            # sampling IDs without a provider response ID remain distinct attempts.
            unique = {}
            for event in selected:
                observation = self._observation(event)
                unique.setdefault(observation.usage_key(), event)
            attempts = []
            for key, event in unique.items():
                usage = event["raw_usage"] if event["usage_presence"] == "object" else None
                if key in ledger.conflicts:
                    usage = None
                status = "completed" if event["response_status"] == "completed" else (
                    "cancelled" if event["response_status"] == "cancelled" else "failed")
                attempts.append(cache_stats.Attempt(_encoded(key), event["run_id"], "measure", status,
                    usage, evidence_kind=mode, semantics=("responses_input_includes_cached_v1"
                        if event["input_semantics"] == "responses_inclusive_v1" else "unverified_provider_semantics")))
            scopes[mode] = {"sampling_events": len(selected), "unique_usage_facts": len(unique),
                "usage_presence": dict(Counter(event["usage_presence"] for event in selected)),
                "identity_basis": dict(Counter(event["identity_basis"] for event in selected)),
                "conflicts": conflict_reasons, "runs": task_reports,
                "cache": cache_stats.summarize(attempts)}
        return {"schema": "mini-codex-services-report-v1", "state": "observed",
                "sampling_events": len(events), "scopes": scopes,
                "run_results": runs, "sqlite_path": str(self.path), "journal_path": str(self.journal_path),
                "cost_status": "unpriced_no_rates_configured", "billing_claim": "not_an_invoice",
                "limitations": ["Replay usage is not provider billing evidence.",
                    "Missing and unobservable usage remain unknown, never zero.",
                    "No cache speedup or external service acceptance is inferred."]}

    @_synchronized
    def capture_artifact(self, path):
        self._check_open()
        return system_audit.capture_artifact(Path(path))

    @_synchronized
    def evaluate(self, run_dir, expected_cases=None):
        self._check_open()
        return system_audit.evaluate_run(Path(run_dir), expected_cases=expected_cases)

    @_synchronized
    def doctor(self):
        self._check_open()
        integrity = self.db.execute("PRAGMA integrity_check").fetchone()[0]
        return {"status": "passed" if integrity == "ok" else "failed", "sqlite_integrity": integrity,
                "scope": "local_service_state", "capabilities": self.capabilities()}

    @_synchronized
    def capabilities(self):
        return [
            {"name": "telemetry", "status": "runtime_connected", "scope": "model request/result -> SQLite -> Ledger"},
            {"name": "cache_statistics", "status": "runtime_connected", "scope": "observed usage only; no cache implementation"},
            {"name": "artifact_capture", "status": "available_local", "scope": "file bytes and SHA-256"},
            {"name": "evaluation", "status": "available_local", "scope": "final demo evidence gate"},
            {"name": "hosts", "status": "local_mechanism", "scope": "in-process journal; no daemon or remote transport"},
            {"name": "cloud_artifacts", "status": "local_mechanism", "scope": "Git artifact gate; cloud service not configured"},
            {"name": "media", "status": "local_mechanism", "scope": "attachment bytes and jobs; image provider not configured"},
            {"name": "realtime", "status": "protocol_only", "scope": "event/queue logic; audio devices and service not configured"},
            {"name": "search", "status": "protocol_only", "scope": "citation table; search transport not configured"},
            {"name": "computer_use", "status": "protocol_only", "scope": "observation/action ledger; browser/device not configured"},
            {"name": "distribution", "status": "local_mechanism", "scope": "diagnostic upgrade lab; not an Agent installer"},
        ]

    @_synchronized
    def close(self):
        if not self.closed:
            self.db.close()
            self.closed = True

