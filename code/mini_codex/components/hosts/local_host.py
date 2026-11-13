"""Local teaching host: real file journal, in-process state, no daemon/RPC/network.

Single writer only. The journal flushes to a file; this is not an OS crash-durable
service implementation, authenticated transport, or task executor.
"""
import json
from pathlib import Path
from uuid import uuid4


class Refused(ValueError):
    pass


class LocalHost:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "journal.jsonl"
        self.state = "ready"
        self.runs, self.keys, self.trust, self.sessions, self.events = {}, {}, {}, {}, []
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                event = json.loads(line)
                self.events.append(event)
                self._apply(event)
            # A new object has no proof that earlier running actions have stopped.
            for run_id, run in list(self.runs.items()):
                if run["state"] == "running":
                    self._record("run_state", run_id=run_id, state="needs_reconciliation")
        self.sessions = {}  # Connections are deliberately not restored.

    def _apply(self, event):
        kind, data = event["kind"], event["data"]
        if kind == "trust":
            self.trust[data["controller"]] = data["generation"]
        elif kind == "submitted":
            self.keys[data["key"]] = data["run_id"]
            self.runs[data["run_id"]] = {"state": "queued", "instruction": data["instruction"]}
        elif kind == "run_state":
            self.runs[data["run_id"]]["state"] = data["state"]

    def _record(self, kind, **data):
        event = {"cursor": len(self.events) + 1, "kind": kind, "data": data}
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            stream.flush()
        self.events.append(event)
        self._apply(event)
        return event

    def pair(self, controller):
        generation = self.trust.get(controller, 0) + 1
        self._record("trust", controller=controller, generation=generation)
        return generation

    def connect(self, controller, generation):
        if self.state != "ready" or self.trust.get(controller) != generation:
            raise Refused("host_unready_or_stale_trust")
        session = uuid4().hex
        self.sessions[session] = (controller, generation)
        return session

    def authorize(self, session, allowed=True):
        if session not in self.sessions:
            raise Refused("connection_closed")
        controller, generation = self.sessions[session]
        if self.trust.get(controller) != generation:
            raise Refused("trust_revoked")
        if not allowed:
            raise Refused("action_policy_denied")

    def submit(self, session, key, instruction):
        self.authorize(session)
        if self.state != "ready":
            raise Refused("host_draining")
        if key in self.keys:
            run_id = self.keys[key]
            if self.runs[run_id]["instruction"] != instruction:
                raise Refused("idempotency_key_conflict")
            return run_id
        run_id = uuid4().hex
        self._record("submitted", run_id=run_id, key=key, instruction=instruction)
        return run_id

    def start(self, run_id):
        if self.state != "ready" or self.runs[run_id]["state"] != "queued":
            raise Refused("cannot_start")
        self._record("run_state", run_id=run_id, state="running")

    def disconnect(self, session):
        self.sessions.pop(session, None)

    def revoke(self, controller):
        self._record("trust", controller=controller, generation=self.trust[controller] + 1)

    def read_after(self, session, cursor):
        self.authorize(session)
        return [event for event in self.events if event["cursor"] > cursor]

    def cancel(self, session, run_id):
        self.authorize(session)
        if self.runs[run_id]["state"] not in {"completed", "cancelled"}:
            # No worker exists here: cancellation is requested, not confirmed.
            self._record("run_state", run_id=run_id, state="cancel_requested")

    def acknowledge_cancel(self, run_id, *, cleanup_confirmed):
        if self.runs[run_id]["state"] != "cancel_requested" or not cleanup_confirmed:
            raise Refused("cleanup_not_confirmed")
        self._record("run_state", run_id=run_id, state="cancelled")

    def drain(self):
        self.state = "draining"
        return {"queued": sum(r["state"] == "queued" for r in self.runs.values()),
                "unsettled": [k for k, r in self.runs.items()
                              if r["state"] not in {"queued", "completed", "cancelled"}]}
