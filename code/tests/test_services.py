"""Local tests only: no Docker, credentials, provider calls, or device access."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
import unittest

from mini_codex.contracts import CallContext, ModelRequest, ModelResult, RunResult
from mini_codex.services import ServiceHub
from mini_codex.components.telemetry.meter import Conflict
from mini_codex.components.assembly import system_audit


class ServiceHubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hub = ServiceHub(self.root)
        self.addCleanup(self.hub.close)

    def request(self, sampling="sample-1", mode="fixture", provider="openai"):
        return ModelRequest(CallContext("run-1", "turn-1", sampling, time.monotonic() + 10),
            "snapshot-1", "private prompt", (), (),
            {"mode": mode, "provider": provider, "model": "test-model", "api_key": "DO-NOT-LOG"})

    def result(self, usage=None, presence="not_observable", response_id=None, status="completed"):
        return ModelResult("adapter-id", status, (), usage, response_id=response_id, usage_presence=presence)

    def test_presence_zero_missing_null_and_unknown_survive_sqlite(self):
        rows = [("zero", {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                           "input_tokens_details": {"cached_tokens": 0}}, "object"),
                ("missing", None, "missing"), ("null", None, "null"),
                ("unobservable", None, "not_observable")]
        for name, usage, presence in rows:
            self.hub.on_model(self.request(name), self.result(usage, presence))
        report = self.hub.report()["scopes"]["fixture"]
        self.assertEqual(report["usage_presence"], {"object": 1, "missing": 1, "null": 1, "not_observable": 1})
        self.assertEqual(report["runs"]["run-1"]["tokens"]["input"],
                         {"known_sum": 0, "known_count": 1, "unknown_count": 3})
        self.assertEqual(report["cache"]["eligible_attempts"], 1)
        self.assertIsNone(report["cache"]["cached_token_fraction"])
        payloads = [json.loads(line) for line in self.hub.journal_path.read_text().splitlines()]
        self.assertEqual({p["usage_presence"] for p in payloads}, {"object", "missing", "null", "not_observable"})
        self.assertNotIn("DO-NOT-LOG", self.hub.journal_path.read_text())
        self.assertNotIn("private prompt", self.hub.journal_path.read_text())

    def test_missing_provider_id_uses_real_local_sampling_identity(self):
        first = self.hub.on_model(self.request("sample-a"), self.result())
        self.hub.on_model(self.request("sample-b"), self.result())
        self.assertEqual(first["identity_basis"], "local_sampling_id")
        report = self.hub.report()["scopes"]["fixture"]
        self.assertEqual(report["unique_usage_facts"], 2)
        stored = self.hub._events()
        self.assertTrue(all(p["response_id"] is None for p in stored))
        self.assertTrue(all(p["adapter_request_id"] == "adapter-id" for p in stored))

    def test_restart_preserves_events_and_duplicate_callback_is_not_new_usage(self):
        request, result = self.request(), self.result()
        self.hub.on_model(request, result)
        self.hub.close()
        # Simulate a stale projection after a SQLite commit, not a process crash.
        (self.root / "model-usage.jsonl").write_text("")
        self.hub = ServiceHub(self.root)
        self.addCleanup(self.hub.close)
        self.assertEqual(self.hub.on_model(request, result)["status"], "duplicate_event")
        self.assertEqual(self.hub.report()["sampling_events"], 1)
        self.assertEqual(len(self.hub.journal_path.read_text().splitlines()), 1)

    def test_replay_live_fixture_never_share_cache_cohort(self):
        usage = {"input_tokens": 100, "output_tokens": 4, "input_tokens_details": {"cached_tokens": 60}}
        for mode in ("replay", "fixture", "live"):
            self.hub.on_model(self.request(mode=mode), self.result(usage, "object"))
        scopes = self.hub.report()["scopes"]
        self.assertEqual(set(scopes), {"replay", "fixture", "live"})
        for mode, scope in scopes.items():
            self.assertEqual(scope["cache"]["evidence_kind"], mode)
            self.assertEqual(scope["cache"]["cached_token_fraction"], .6)

    def test_response_redelivery_deduplicates_but_conflict_excludes_usage(self):
        usage = {"input_tokens": 100, "output_tokens": 4, "input_tokens_details": {"cached_tokens": 60}}
        self.hub.on_model(self.request("sample-a"), self.result(usage, "object", "response-1"))
        self.hub.on_model(self.request("sample-b"), self.result(usage, "object", "response-1"))
        report = self.hub.report()["scopes"]["fixture"]
        self.assertEqual(report["sampling_events"], 2)
        self.assertEqual(report["unique_usage_facts"], 1)
        self.assertEqual(report["cache"]["eligible_input_tokens"], 100)
        self.hub.on_model(self.request("sample-c"), self.result({**usage, "input_tokens": 200}, "object", "response-1"))
        conflicted = self.hub.report()["scopes"]["fixture"]
        self.assertIn("same_response_changed_fact", conflicted["conflicts"])
        self.assertEqual(conflicted["cache"]["eligible_attempts"], 0)

    def test_same_sampling_conflict_is_retained_and_invalidates_projection(self):
        self.hub.on_model(self.request(), self.result())
        with self.assertRaisesRegex(Conflict, "same_sampling_changed_fact"):
            self.hub.on_model(self.request(), self.result(presence="missing"))
        self.assertIn("same_sampling_changed_fact", self.hub.report()["scopes"]["fixture"]["conflicts"])

    def test_conflicting_response_identity_invalidates_original_fact_too(self):
        usage = {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 50}}
        self.hub.on_model(self.request(), self.result(usage, "object", "original-response"))
        with self.assertRaises(Conflict):
            self.hub.on_model(self.request(), self.result(usage, "object", "different-response"))
        self.assertEqual(self.hub.report()["scopes"]["fixture"]["cache"]["eligible_attempts"], 0)

    def test_provider_semantics_not_inferred_from_responses_shape(self):
        usage = {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 60}}
        self.hub.on_model(self.request(provider="unknown-provider"), self.result(usage, "object"))
        stats = self.hub.report()["scopes"]["fixture"]["cache"]
        self.assertEqual(stats["eligible_attempts"], 0)
        self.assertEqual(set(stats["excluded"].values()), {"unverified_provider_semantics"})

    def test_failed_request_usage_and_final_inventory_are_retained(self):
        usage = {"input_tokens": 50, "output_tokens": 5, "input_tokens_details": {"cached_tokens": 0}}
        self.hub.on_model(self.request(), self.result(usage, "object", status="failed"))
        self.hub.on_run_result(RunResult("run-1", "failed", "provider_error", None, "confirmed", 1, "fixture"))
        scope = self.hub.report()["scopes"]["fixture"]
        self.assertEqual(scope["cache"]["status_counts"], {"failed": 1})
        self.assertEqual(scope["runs"]["run-1"]["tokens"]["input"]["known_sum"], 50)
        self.assertTrue(scope["runs"]["run-1"]["inventory_complete"])
        self.assertEqual(scope["runs"]["run-1"]["coverage"], "partial")

    def test_doctor_close_and_external_capability_limits(self):
        self.assertEqual(self.hub.doctor()["status"], "passed")
        computer_use = next(c for c in self.hub.capabilities() if c["name"] == "computer_use")
        self.assertEqual(computer_use["status"], "protocol_only")
        self.hub.close()
        self.hub.close()
        with self.assertRaisesRegex(RuntimeError, "service_hub_closed"):
            self.hub.report()

    def test_worker_thread_callbacks_and_concurrent_reports_share_one_owner(self):
        from concurrent.futures import ThreadPoolExecutor
        def work(index):
            self.hub.on_model(self.request(f"sample-{index}"), self.result())
            return self.hub.report()["sampling_events"]
        with ThreadPoolExecutor(max_workers=4) as workers:
            counts = list(workers.map(work, range(20)))
        self.assertEqual(self.hub.report()["sampling_events"], 20)
        self.assertTrue(all(1 <= count <= 20 for count in counts))
        self.assertEqual(len(self.hub.journal_path.read_text().splitlines()), 20)


class FinalEvidenceGateTests(unittest.TestCase):
    def test_packaged_default_contract_rejects_missing_evidence(self):
        with tempfile.TemporaryDirectory() as empty:
            result = system_audit.evaluate_run(Path(empty))
        self.assertEqual(result['status'], 'rejected')
        self.assertIn('missing_evidence:verification', result['reasons'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cases = [{"id": "synthetic-only", "input": {"value": 1}, "expected": 1}]

    def write_synthetic_structure(self):
        """Only validates rejection/check algorithms, never task acceptance."""
        snapshot = self.root / "snapshot"
        snapshot.mkdir()
        (snapshot / "synthetic.txt").write_text("synthetic test payload")
        files = {"synthetic.txt": system_audit.digest((snapshot / "synthetic.txt").read_bytes())}
        candidate = {"hash": system_audit.digest(system_audit.canonical(files).encode()), "files": files}
        result = {"run_id": "synthetic", "mode": "fixture", "task_status": "passed", "cleanup_status": "confirmed"}
        verification = {"passed": True, "case_ids": ["synthetic-only"], "expected_count": 1,
            "executed_count": 1, "assertion_count": 1, "snapshot_unchanged": True, "protection_intact": True,
            "cleanup_status": "confirmed", "candidate": candidate, "frozen_hash": candidate["hash"],
            "cases": [{**self.cases[0], "actual": 1, "passed": True, "error": None,
                "execution": {"exit_code": 0, "process_status": "exited", "cleanup_status": "confirmed", "truncated": False}}]}
        (self.root / "result.json").write_text(json.dumps(result))
        (self.root / "final-verification.json").write_text(json.dumps(verification))
        return verification

    def test_missing_proof_never_passes(self):
        result = system_audit.evaluate_run(self.root, self.cases)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("missing_evidence:verification", result["reasons"])

    def test_changed_snapshot_rejects_previously_matching_structure(self):
        self.write_synthetic_structure()
        self.assertEqual(system_audit.evaluate_run(self.root, self.cases)["status"], "passed")
        (self.root / "snapshot/synthetic.txt").write_text("changed")
        result = system_audit.evaluate_run(self.root, self.cases)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("frozen_snapshot_hash_mismatch", result["reasons"])

    def test_fake_pass_and_self_changed_expectation_rejected(self):
        verification = self.write_synthetic_structure()
        row = verification["cases"][0]
        row.update(expected=999, actual=999)
        row["execution"]["cleanup_status"] = "unknown"
        (self.root / "final-verification.json").write_text(json.dumps(verification))
        result = system_audit.evaluate_run(self.root, self.cases)
        self.assertIn("case_contract_changed:synthetic-only", result["reasons"])
        self.assertIn("case_assertion_failed:synthetic-only", result["reasons"])
        self.assertIn("case_execution_unconfirmed:synthetic-only", result["reasons"])


if __name__ == "__main__":
    unittest.main()
