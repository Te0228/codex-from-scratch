"""Teaching prototype: raw Responses usage -> observations -> partial cost projection.

No network, credentials, agent loop, recovery journal, or exporter is implemented.
Only allowlisted usage measurements enter observations. Input events may be synthetic.
"""
from dataclasses import asdict, dataclass
import copy
from decimal import Decimal
import hashlib
import json


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class Measurement:
    state: str
    value: int | None
    pointer: str


def inspect_field(raw, path):
    """Missing, null, invalid and numeric zero are distinct, including parent nodes."""
    node = raw
    traversed = []
    for index, key in enumerate(path):
        pointer = "/" + "/".join(traversed)
        if node is None:
            return Measurement("parent_null", None, pointer)
        if not isinstance(node, dict):
            return Measurement("parent_invalid", None, pointer)
        traversed.append(key)
        pointer = "/" + "/".join(traversed)
        if key not in node:
            state = "missing" if index == len(path) - 1 else "parent_missing"
            return Measurement(state, None, pointer)
        node = node[key]
    if node is None:
        return Measurement("null", None, pointer)
    if type(node) is not int or node < 0:
        return Measurement("invalid", None, pointer)
    return Measurement("known", node, pointer)


PATHS = {
    "input": ("usage", "input_tokens"),
    "output": ("usage", "output_tokens"),
    "total": ("usage", "total_tokens"),
    "cached": ("usage", "input_tokens_details", "cached_tokens"),
}


@dataclass(frozen=True)
class Observation:
    event_id: str
    task_id: str
    thread_id: str
    parent_thread_id: str | None
    root_turn_id: str
    attempt_id: str
    provider_scope: str
    model: str
    response_id: str | None
    purpose: str
    terminal_kind: str
    incomplete_reason: str | None
    usage_presence: str
    evidence_ref: str
    fields: dict[str, Measurement]
    evidence_kind: str = "fixture"
    phase: str = "measure"
    input_semantics: str = "responses_inclusive_v1"

    def usage_key(self):
        # provider_scope is an opaque provider/account/environment namespace, never a key.
        identity = ("response", self.response_id) if self.response_id else ("attempt", self.attempt_id)
        return (self.provider_scope, *identity)

    def fact_content(self):
        # Envelope ID and evidence location may change on redelivery; the usage fact may not.
        content = asdict(self)
        for name in ("event_id", "evidence_ref", "attempt_id"):
            content.pop(name)
        return content


def observe_raw(raw_event, **identity):
    """Actual parser for Responses-shaped terminal events; no classification fixture."""
    kind = raw_event.get("type")
    if kind not in {"response.completed", "response.incomplete", "response.failed", "transport.failed"}:
        raise ValueError("unsupported_event_kind")
    response = raw_event.get("response", {})
    if not isinstance(response, dict):
        raise ValueError("invalid_response_object")
    usage_presence = ("missing" if "usage" not in response else
                      "null" if response["usage"] is None else
                      "object" if isinstance(response["usage"], dict) else "invalid")
    response_id = response.get("id")
    if response_id is not None and (not isinstance(response_id, str) or not response_id):
        raise ValueError("invalid_response_id")
    details = response.get("incomplete_details")
    reason = details.get("reason") if isinstance(details, dict) else None
    if reason is not None and not isinstance(reason, str):
        raise ValueError("invalid_incomplete_reason")
    fields = {name: inspect_field(response, path) for name, path in PATHS.items()}
    inp, cached = fields["input"], fields["cached"]
    if inp.state == cached.state == "known" and cached.value > inp.value:
        fields["cached"] = Measurement("inconsistent", None, cached.pointer)
    return Observation(response_id=response_id, terminal_kind=kind,
                       incomplete_reason=reason, usage_presence=usage_presence,
                       fields=fields, **identity)


def observe_numeric_only(*, response_id, **identity):
    """Historical numeric totals cannot recreate the provider's raw field presence.

    This conservative sidecar reports unobservable evidence. Historical numeric
    UI totals can remain in a separate projection; they are not raw evidence.
    """
    return Observation(response_id=response_id, terminal_kind="normalized_only",
                       incomplete_reason=None, usage_presence="presence_not_observable",
                       fields={name: Measurement("presence_not_observable", None,
                                                 "/" + "/".join(path))
                               for name, path in PATHS.items()}, **identity)


class Conflict(ValueError):
    pass


class Ledger:
    def __init__(self):
        self.events = {}
        self.observations = {}
        self.conflicts = set()

    def record(self, observation):
        event_key = (observation.provider_scope, observation.event_id)
        digest = hashlib.sha256(canonical(asdict(observation)).encode()).hexdigest()
        old = self.events.get(event_key)
        if old is not None:
            if old[0] != digest:
                self.conflicts.add(observation.usage_key())
                self.conflicts.add(old[1])
                raise Conflict("same_event_id_changed_content")
            return "duplicate_event"
        key = observation.usage_key()
        prior = self.observations.get(key)
        if prior and prior.fact_content() != observation.fact_content():
            self.conflicts.add(key)
            raise Conflict("same_response_changed_fact")
        self.events[event_key] = (digest, key)
        if prior:
            return "duplicate_response"
        self.observations[key] = copy.deepcopy(observation)
        return "recorded"

    def query(self, task_id, rates, *, inventory_complete=False):
        """rates: model -> Decimal price per one million tokens, explicit version.

        inventory_complete is supplied by the runtime/session owner only after
        all task/child attempts are enumerated. Observations alone cannot prove it.
        """
        rows = [o for o in self.observations.values() if o.task_id == task_id]
        aggregates = {name: {"known_sum": 0, "known_count": 0, "unknown_count": 0}
                      for name in PATHS}
        known_cost = Decimal(0)
        unpriced = []
        breakdown = []
        for row in rows:
            conflicted = row.usage_key() in self.conflicts
            for name, measurement in row.fields.items():
                dest = aggregates[name]
                if measurement.state == "known" and not conflicted:
                    dest["known_sum"] += measurement.value
                    dest["known_count"] += 1
                else:
                    dest["unknown_count"] += 1
            rate = rates.get(row.model)
            parts = []
            if not rate or conflicted:
                unpriced.append({"attempt_id": row.attempt_id, "component": "all",
                                 "reason": "conflicting_evidence" if conflicted else "missing_rate"})
                continue
            output = row.fields["output"]
            if output.state == "known":
                parts.append(("output", Decimal(output.value) * rate["output"]))
            else:
                unpriced.append({"attempt_id": row.attempt_id, "component": "output",
                                 "reason": output.state})
            inp, cached = row.fields["input"], row.fields["cached"]
            if inp.state == cached.state == "known" and row.input_semantics == "responses_inclusive_v1":
                parts.extend([("input_uncached", Decimal(inp.value - cached.value) * rate["input"]),
                              ("input_cached", Decimal(cached.value) * rate["cached"])])
            else:
                unpriced.append({"attempt_id": row.attempt_id, "component": "input",
                                 "reason": ("input_semantics_unknown"
                                            if row.input_semantics != "responses_inclusive_v1"
                                            else "cache_split_or_input_unknown")})
            for component, amount in parts:
                amount /= Decimal(1_000_000)
                known_cost += amount
                breakdown.append({"attempt_id": row.attempt_id, "thread_id": row.thread_id,
                                  "purpose": row.purpose, "terminal_kind": row.terminal_kind,
                                  "component": component, "estimated_amount": str(amount),
                                  "price_version": rate["version"]})
        complete = bool(rows) and inventory_complete and not unpriced
        return {"task_id": task_id, "unique_observations": len(rows),
                "inventory_complete": inventory_complete, "tokens": aggregates,
                "estimated_known_subtotal": str(known_cost), "currency": "TEST_UNIT",
                "coverage": "complete_for_declared_inventory" if complete else "partial",
                "unpriced": unpriced, "breakdown": breakdown,
                "billing_claim": "not_an_invoice"}
