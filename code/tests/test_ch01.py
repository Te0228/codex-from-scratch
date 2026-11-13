import json
import os
from pathlib import Path
import platform
import shutil
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from mini_codex.__main__ import prepare
from mini_codex.contracts import (CallContext, CancelToken, FixedPermit, ModelRequest, ModelResult, RunLimits,
                                  TaskInput, ToolCall)
from mini_codex.dispatcher import Dispatcher
from mini_codex.history import History
from mini_codex.loop import Loop, unavailable_service
from mini_codex.model import ReplayModel, ResponsesModel, function_call, message
from mini_codex.model_http import request as http_request
from mini_codex.verify import Verifier, sha256, save_json
from mini_codex.worker import CHECKPOINT, LinuxWorker


class HistoryTests(unittest.TestCase):
    def test_snapshot_is_defensive_and_preserves_reasoning(self):
        history = History("unit")
        item = {"type": "reasoning", "encrypted_content": "opaque", "summary": []}
        history.append("raw", {}, [item])
        snapshot = history.snapshot()
        snapshot.raw_items[0]["summary"].append("caller mutation")
        self.assertEqual(history.snapshot().raw_items[0], item)

    def test_trace_redacts_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            history = History("unit", path, ("secret-value",))
            history.append("error", {"message": "secret-value"})
            self.assertNotIn("secret-value", path.read_text())


class TransportTests(unittest.TestCase):
    """Real local HTTP transport; synthetic server is not evidence of live model access."""
    def test_trickled_headers_cannot_extend_absolute_deadline(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(2)
        done = threading.Event()
        connected = threading.Event()
        def slow_server():
            try:
                conn, _ = listener.accept()
                with conn:
                    connected.set()
                    conn.recv(65536)
                    for byte in b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n":
                        if done.wait(0.02):
                            return
                        conn.sendall(bytes([byte]))
            except OSError:
                pass
        thread = threading.Thread(target=slow_server, daemon=True)
        thread.start()
        try:
            with patch.dict(os.environ, {"OPENAI_API_KEY": "transport-test-key", "OPENAI_MODEL": "test",
                                         "OPENAI_BASE_URL": ""}):
                model = ResponsesModel(endpoint=("http", "127.0.0.1", listener.getsockname()[1]))
                start = time.monotonic()
                context = CallContext("transport", "turn", "sample", start + 0.2)
                result = model.sample(ModelRequest(context, "snapshot", "test", (), (), {}))
                elapsed = time.monotonic() - start
            self.assertTrue(connected.is_set(), "real HTTP child must reach the slow-header server")
            self.assertEqual(result.provider_error, "model_deadline")
            self.assertEqual(model.last_cleanup, "confirmed")
            self.assertLess(elapsed, 0.6, "idle socket timeouts must not extend the whole request")
        finally:
            done.set()
            listener.close()
            thread.join(timeout=2)

    def test_service_gate_does_not_reclassify_budget_or_invalid_responses(self):
        for error in ("model_deadline", "model_timeout", "response_too_large",
                      "model_response_format", "model_child_response_format", "provider_http_400"):
            self.assertFalse(unavailable_service(error), error)
        for error in ("provider_http_401", "provider_http_503", "provider_unreachable"):
            self.assertTrue(unavailable_service(error), error)

    def test_nonofficial_base_is_rejected_before_any_request(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "other-provider-key", "OPENAI_MODEL": "test",
                                     "OPENAI_BASE_URL": "https://other-provider.invalid/v1"}), \
             patch("mini_codex.model.subprocess.Popen") as launch:
            with self.assertRaisesRegex(ValueError, "unsupported_OPENAI_BASE_URL"):
                ResponsesModel()
            launch.assert_not_called()
        for url in ("", "https://api.openai.com/v1", "https://api.openai.com/v1/"):
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "OPENAI_MODEL": "test",
                                         "OPENAI_BASE_URL": url}):
                ResponsesModel()

    def test_http_request_id_and_response_id_stay_distinct_without_network(self):
        response = MagicMock(status=200)
        response.getheader.return_value = "req-from-header"
        response.read.return_value = json.dumps({"id": "resp-from-body", "status": "completed",
                                                "output": [], "usage": {"total_tokens": 1}}).encode()
        connection = MagicMock()
        connection.getresponse.return_value = response
        with patch("mini_codex.model_http.http.client.HTTPSConnection", return_value=connection):
            result = http_request({"endpoint": ["https", "api.openai.com", 443],
                                   "key": "test-key", "timeout": 1, "payload": {}})
        self.assertEqual(result["request_id"], "req-from-header")
        self.assertEqual(result["response_id"], "resp-from-body")
        child = MagicMock()
        child.communicate.return_value = (json.dumps(result).encode(), b"")
        child.poll.return_value = 0
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "OPENAI_MODEL": "test",
                                     "OPENAI_BASE_URL": ""}), \
             patch("mini_codex.model.subprocess.Popen", return_value=child):
            context = CallContext("ids", "turn", "sample", time.monotonic() + 1)
            adapted = ResponsesModel().sample(ModelRequest(context, "snapshot", "test", (), (), {}))
        self.assertEqual(adapted.request_id, "req-from-header")
        self.assertEqual(adapted.response_id, "resp-from-body")
        # Appending the optional field preserves the previous positional constructor.
        self.assertIsNone(ModelResult("req", "completed", (), {}, None).response_id)


@unittest.skipUnless(platform.system() == "Linux" and shutil.which("bwrap"),
                     "real isolation tests require Linux+bwrap; no fake fallback")
class RealLinuxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environment = tempfile.TemporaryDirectory()
        root = Path(cls.environment.name)
        workspace = prepare(root)
        preflight = LinuxWorker(workspace).preflight()
        if preflight["status"] != "passed":
            raise RuntimeError("sandbox preflight failed: " + json.dumps(preflight))

    @classmethod
    def tearDownClass(cls):
        cls.environment.cleanup()

    def setUp(self):
        evidence = os.environ.get("MINI_CODEX_TEST_EVIDENCE")
        if evidence:
            self.run_dir = Path(evidence) / self._testMethodName
        else:
            self.tmp = tempfile.TemporaryDirectory()
            self.addCleanup(self.tmp.cleanup)
            self.run_dir = Path(self.tmp.name)
        self.workspace = prepare(self.run_dir)
        self.worker = LinuxWorker(self.workspace)
        self.verifier = Verifier()
        self.history = History("test-run", self.run_dir / "events.jsonl")
        self.cancel = CancelToken()

    def tearDown(self):
        save_json(self.run_dir / "last-cleanup.json", self.worker.last_cleanup)

    def record(self, name, value):
        save_json(self.run_dir / (name + ".json"), value)
        return value

    def make_loop(self, responses=None, *, lose=None, after=None):
        self.model = ReplayModel(responses)
        self.dispatcher = Dispatcher(self.worker, self.history, lose_after_call=lose)
        self.loop = Loop(self.model, self.history, self.dispatcher, self.verifier,
                         self.run_dir, self.cancel, after_observation=after)
        return self.loop

    def run_loop(self, steps=10, seconds=30):
        return self.loop.run(TaskInput("test", "修复status", str(self.workspace)),
                             RunLimits(steps, time.monotonic() + seconds))

    def test_paired_isolation_and_environment_secret(self):
        self.assertEqual(self.record("paired-controls", self.worker.preflight())["status"], "passed")
        os.environ["CH01_TEST_SECRET"] = "must-not-reach-worker"
        self.addCleanup(os.environ.pop, "CH01_TEST_SECRET", None)
        result = self.worker.probe("exec", {"command": "env"})
        self.assertNotIn("must-not-reach-worker", result.output)
        self.assertEqual(result.exit_code, 0)

    def test_fixed_red_then_real_replay_green_and_history_pairing(self):
        baseline = self.record("baseline", self.verifier.baseline(self.workspace, time.monotonic() + 20))
        self.assertTrue(baseline["baseline_valid"])
        self.make_loop()
        result = self.run_loop()
        self.assertEqual(result.task_status, "passed")
        final = json.loads((self.run_dir / "final-verification.json").read_text())
        self.assertEqual(final["assertion_count"], 5)
        self.assertEqual(final["frozen_hash"], final["candidate"]["hash"])
        inputs = self.model.requests[1].messages
        self.assertTrue(any(i.get("type") == "function_call_output" and
                            i.get("call_id") == "search-1" for i in inputs))
        self.assertEqual(sum(e["kind"] == "run_stopped" for e in self.history.events), 1)
        self.assertFalse(self.loop.stop("late_second_stop"))

    def test_false_completion_returns_failure_to_model_then_can_fix(self):
        self.make_loop([[message("all passed")]] + ReplayModel().responses)
        result = self.run_loop()
        self.assertEqual(result.task_status, "passed")
        self.assertTrue(any("独立检查未通过" in i.get("content", "")
                            for i in self.model.requests[1].messages if isinstance(i.get("content"), str)))

    def test_fake_pass_and_exit_zero_cannot_pass(self):
        for index, source in enumerate(('print("all passed: 999")\n', 'raise SystemExit(0)\n')):
            (self.workspace / "task_list.py").write_text(source)
            result = self.record(f"forged-{index}", self.verifier.check(self.workspace, time.monotonic() + 20))
            self.assertFalse(result["passed"])
            self.assertEqual(result["executed_count"], 5)
            self.assertEqual(result["assertion_count"], 0)

    def test_writable_test_configuration_cannot_hide_fixed_cases(self):
        (self.workspace / "pytest.ini").write_text("[pytest]\npython_files = nothing.py\n")
        (self.workspace / "json.py").write_text('raise RuntimeError("shadowed json")')
        (self.workspace / "conftest.py").write_text("def pytest_collection_modifyitems(items): items.clear()\n")
        result = self.record("fixed-collection", self.verifier.baseline(self.workspace, time.monotonic() + 20))
        self.assertTrue(result["baseline_valid"])
        self.assertEqual(result["assertion_count"], 5)

    def test_os_protects_verifier_and_launcher(self):
        before = self.verifier.protection()
        for name in ("mini_codex/assets/checks/cases.json", "mini_codex/verify.py", "mini_codex/worker_entry.py"):
            result = self.worker.probe("exec", {"command": f"printf forged > /opt/mini-codex/{name}"})
            self.record("denial-" + Path(name).name, result.__dict__)
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("Read-only", result.output)
        self.assertEqual(self.verifier.protection(), before)

    def test_final_snapshot_is_os_readonly(self):
        result = LinuxWorker(self.workspace, read_only=True).probe("exec", {
            "command": "printf changed > task_list.py"})
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(sha256(self.workspace / "task_list.py"), self.verifier.manifest["baseline_sha256"])

    def test_duplicate_call_not_reentered_and_unknown_tool_rejected(self):
        dispatcher = Dispatcher(self.worker, self.history)
        context = CallContext("test-run", "turn1", "sample1", time.monotonic() + 20, "same")
        permit = FixedPermit("test-run", str(self.workspace))
        call = ToolCall("same", "exec", {"command": "printf x >> writes"})
        self.assertEqual(dispatcher.dispatch(call, context, permit).outcome, "ok")
        self.assertEqual(dispatcher.dispatch(call, context, permit).error_code, "duplicate_call")
        self.assertEqual((self.workspace / "writes").read_text(), "x")
        bad = ToolCall("new", "network", {})
        bad_context = CallContext("test-run", "turn1", "sample1", time.monotonic() + 20, "new")
        self.assertEqual(dispatcher.dispatch(bad, bad_context, permit).error_code, "unknown_or_denied_tool")
        self.assertEqual(self.worker.executions, 1)

    def test_write_then_lost_confirmation_is_unknown_and_not_retried(self):
        self.make_loop([[function_call("lost", "exec", command="printf x >> side-effect")]], lose="lost")
        result = self.run_loop()
        self.assertEqual(result.task_status, "unknown")
        self.assertEqual(self.model.index, 1)
        self.assertEqual((self.workspace / "side-effect").read_text(), "x")
        self.assertEqual(self.worker.executions, 1)
        self.assertTrue(self.worker.clean)
        self.record("independent-local-inspection", {"file": "side-effect", "text":
                    (self.workspace / "side-effect").read_text(), "cleanup": self.worker.last_cleanup,
                    "scope": "write observed once; lost action confirmation remains unknown"})
        observation = next(e["payload"] for e in self.history.events if e["kind"] == "tool_observation")
        self.assertEqual(observation["side_effect_status"], "unknown")

    def test_empty_and_invalid_response_consume_steps(self):
        self.make_loop([[], [{"type": "function_call", "call_id": "bad", "name": "exec",
                              "arguments": "{", "status": "completed"}], []])
        result = self.run_loop(steps=3)
        self.assertEqual(result.stop_reason, "step_limit")
        self.assertEqual(result.steps, 3)
        self.assertEqual(self.worker.executions, 0)

    def test_absolute_deadline_is_not_extended(self):
        self.make_loop([])
        result = self.run_loop(steps=100000, seconds=0.02)
        self.assertEqual(result.stop_reason, "deadline")
        self.assertNotEqual(result.task_status, "passed")

    def test_first_provider_failure_is_blocked_without_tool_execution(self):
        self.make_loop([])
        self.model.sample = lambda request: ModelResult(None, "failed", (), provider_error="provider_http_401")
        result = self.run_loop()
        self.assertEqual(result.task_status, "blocked")
        self.assertEqual(self.worker.executions, 0)
        gate = json.loads((self.run_dir / "model-gate.json").read_text())
        self.assertEqual(gate["status"], "blocked")

    def test_candidate_symlink_rejection_keeps_a_terminal_result(self):
        (self.workspace / "bad-link").symlink_to("/outside")
        self.make_loop([[message("done")]])
        result = self.run_loop(steps=1)
        self.assertEqual(result.task_status, "failed")
        self.assertTrue((self.run_dir / "result.json").exists())
        kinds = [e["kind"] for e in self.history.events]
        self.assertIn("candidate_check", kinds)
        self.assertIn("final_verification_error", kinds)

    def test_cancel_after_observation_preserves_result_and_stops_new_work(self):
        self.make_loop([[function_call("read-once", "read", path="task_list.py")],
                        [function_call("must-not-write", "exec", command="touch forbidden")]],
                       after=lambda _: setattr(self.cancel, "cancelled", True))
        result = self.run_loop()
        self.assertEqual(result.task_status, "cancelled")
        self.assertEqual(self.model.index, 1)
        self.assertFalse((self.workspace / "forbidden").exists())
        self.assertTrue(any(e["kind"] == "tool_observation" for e in self.history.events))

    def test_timeout_kills_child_and_output_is_bounded(self):
        result = self.worker.probe("exec", {"command": "(sleep 1; touch survived) & wait"}, timeout=0.2)
        self.record("timeout", {**result.__dict__, "cleanup": self.worker.last_cleanup})
        self.assertEqual(result.process_status, "timeout")
        self.assertEqual(result.side_effect_status, "unknown")
        self.assertEqual(result.cleanup_status, "confirmed")
        self.assertEqual(self.worker.last_cleanup["live_group_members"], [])
        # Positive control confirms the same delayed command can write when allowed to finish.
        positive = self.worker.probe("exec", {"command": "(sleep 1.2; touch control) & wait"}, timeout=3)
        self.assertEqual(positive.exit_code, 0)
        self.assertTrue((self.workspace / "control").exists())
        self.assertFalse((self.workspace / "survived").exists())
        self.record("delayed-child-control", {"positive_exit_code": positive.exit_code,
                    "positive_file_exists": (self.workspace / "control").exists(),
                    "timed_out_child_file_exists": (self.workspace / "survived").exists()})
        large = self.worker.probe("exec", {"command": "python3 -I -c 'print(\"x\" * 100000)'"})
        self.assertTrue(large.truncated)
        self.assertLessEqual(len(large.output.encode()), 32768)


if __name__ == "__main__":
    unittest.main()
