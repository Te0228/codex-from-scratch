"""Native Python evidence for chapter 1; no Docker or external model calls."""

from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from mini_codex.minimal import Agent, DeepSeekModel, Executor, History, ScriptedModel, Tools, Verifier


BUGGY_SOURCE = '''import json
import sys


def filter_tasks(tasks, status=None):
    if status is None:
        return tasks
    return [task for task in tasks if task.get("state") == status]


if __name__ == "__main__":
    request = json.load(sys.stdin)
    print(json.dumps(filter_tasks(request["tasks"], request.get("status"))))
'''
TASKS = [{"id": 1, "status": "todo"}, {"id": 2, "status": "done"}]
CASES = [
    {"name": "done", "input": {"tasks": TASKS, "status": "done"}, "expected": [TASKS[1]]},
    {"name": "todo", "input": {"tasks": TASKS, "status": "todo"}, "expected": [TASKS[0]]},
    {"name": "all", "input": {"tasks": TASKS}, "expected": TASKS},
    {"name": "missing", "input": {"tasks": TASKS, "status": "missing"}, "expected": []},
    {"name": "empty", "input": {"tasks": [], "status": "done"}, "expected": []},
]


def call(call_id, name, **arguments):
    return {"type": "function_call", "call_id": call_id, "name": name,
            "arguments": json.dumps(arguments)}


def message(text="修复完成。"):
    return {"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": text}]}


def repair_script():
    return [
        [call("read-1", "read_file", path="task_list.py")],
        [call("test-1", "run_tests")],
        [call("patch-1", "replace_text", path="task_list.py",
              old='task.get("state")', new='task.get("status")')],
        [call("test-2", "run_tests")],
        [message()],
    ]


class MinimalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chapter01-minimal-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.source = self.workspace / "task_list.py"
        self.source.write_text(BUGGY_SOURCE, encoding="utf-8")
        self.executor = Executor()
        self.verifier = Verifier(self.workspace, self.executor, CASES)
        self.tools = Tools(self.workspace, self.verifier)

    def agent(self, script=None):
        model = ScriptedModel(repair_script() if script is None else script)
        return Agent(model, self.tools, self.verifier, "修复任务状态筛选。"), model

    def test_five_native_cases_start_with_exactly_two_failures(self):
        result = self.verifier.check()
        self.assertFalse(result["passed"])
        self.assertEqual([row["passed"] for row in result["cases"]],
                         [False, False, True, True, True])
        self.assertTrue(all(row["exit_code"] == 0 for row in result["cases"]))

    def test_five_round_repair_and_call_id_pairing(self):
        agent, model = self.agent()
        with patch.object(self.verifier, "check", wraps=self.verifier.check) as check:
            result = agent.run("修复 status 筛选")
        self.assertEqual((result["status"], result["steps"]), ("passed", 5))
        self.assertEqual(check.call_count, 3, "two tool checks plus independent completion check")
        self.assertTrue(all(row["passed"] for row in result["verification"]["cases"]))
        self.assertEqual(self.source.read_text(), BUGGY_SOURCE.replace('"state"', '"status"'))
        calls = [item for item in result["history"] if item.get("type") == "function_call"]
        outputs = [item for item in result["history"] if item.get("type") == "function_call_output"]
        self.assertEqual([i["call_id"] for i in calls], [i["call_id"] for i in outputs])
        self.assertEqual([i["name"] for i in calls],
                         ["read_file", "run_tests", "replace_text", "run_tests"])
        self.assertEqual(json.loads(outputs[1]["output"])["passed"], False)
        self.assertEqual(json.loads(outputs[3]["output"])["passed"], True)
        self.assertIn(outputs[0], model.requests[1]["input"])
        self.assertEqual(len(model.requests), 5)

    def test_false_completion_is_rejected_and_feedback_reaches_model(self):
        agent, model = self.agent([[message("全部通过！")]] + repair_script())
        result = agent.run("修复", max_steps=6)
        self.assertEqual((result["status"], result["steps"]), ("passed", 6))
        feedback = model.requests[1]["input"][-1]
        self.assertEqual(feedback["role"], "user")
        self.assertIn("控制器重跑测试仍失败", feedback["content"])
        self.assertIn('"passed": false', feedback["content"])

    def test_false_completion_without_patch_cannot_pass(self):
        agent, _ = self.agent([[message("全部通过！")]])
        result = agent.run("修复", max_steps=2)
        self.assertEqual(result["status"], "step_limit")
        self.assertFalse(result["verification"]["passed"])
        self.assertEqual(self.source.read_text(), BUGGY_SOURCE)

    def test_budgets_zero_two_four_five_are_distinct_from_test_status(self):
        for budget, status, passed in [(0, "step_limit", False), (2, "step_limit", False),
                                       (4, "step_limit", True), (5, "passed", True)]:
            with self.subTest(budget=budget):
                self.source.write_text(BUGGY_SOURCE)
                agent, model = self.agent()
                result = agent.run("修复", max_steps=budget)
                self.assertEqual((result["status"], result["steps"]), (status, budget))
                self.assertEqual(result["verification"]["passed"], passed)
                self.assertEqual(len(model.requests), budget)

    def test_invalid_budgets_are_rejected_before_model_call(self):
        agent, model = self.agent()
        for budget in (-1, 1.5, True):
            with self.assertRaises(ValueError):
                agent.run("修复", max_steps=budget)
        self.assertEqual(model.requests, [])

    def test_unknown_tool_error_is_returned_to_model(self):
        agent, model = self.agent([[call("unknown-1", "shell", command="do not execute")],
                                   [message()]])
        result = agent.run("修复", max_steps=2)
        output = model.requests[1]["input"][-1]
        self.assertEqual(output["call_id"], "unknown-1")
        self.assertIn("未知工具", json.loads(output["output"])["error"])
        self.assertEqual(result["status"], "step_limit")
        self.assertEqual(self.source.read_text(), BUGGY_SOURCE)

    def test_history_preserves_complete_output_and_defensive_copies(self):
        history = History("task")
        output = [{"type": "reasoning", "encrypted_content": "opaque", "summary": []},
                  call("read-1", "read_file", path="task_list.py")]
        history.add_output(output)
        output[0]["summary"].append("external mutation")
        history.add_tool_result("read-1", {"content": "file"})
        request = history.request("instructions", self.tools.specs())
        request["input"][1]["summary"].append("request mutation")
        request["tools"][0]["name"] = "changed"
        snapshot = history.snapshot()
        self.assertEqual(snapshot[1]["summary"], [])
        self.assertEqual(snapshot[-1]["call_id"], "read-1")
        self.assertEqual(json.loads(snapshot[-1]["output"]), {"content": "file"})
        self.assertEqual(self.tools.specs()[0]["name"], "read_file")

    def test_scripted_model_copies_inputs_outputs_and_has_no_hidden_patch(self):
        script = [[message()]]
        model = ScriptedModel(script)
        script[0][0]["content"][0]["text"] = "changed"
        request = {"input": [{"content": "original"}]}
        output = model.sample(request)
        request["input"][0]["content"] = "changed"
        output[0]["content"][0]["text"] = "changed"
        self.assertEqual(model.requests[0]["input"][0]["content"], "original")
        self.assertEqual(model.script[0][0]["content"][0]["text"], "修复完成。")
        self.assertEqual(model.sample({})[0]["type"], "message")
        self.assertEqual(self.source.read_text(), BUGGY_SOURCE)

    def test_each_agent_run_starts_a_new_history(self):
        agent, model = self.agent([[message("first")], [message("second")]])
        first = agent.run("first task", max_steps=1)
        second = agent.run("second task", max_steps=1)
        self.assertEqual(model.requests[1]["input"], [{"role": "user", "content": "second task"}])
        self.assertEqual(first["history"][0]["content"], "first task")
        self.assertEqual(second["history"][0]["content"], "second task")

    def test_two_workspaces_do_not_share_files_or_verification(self):
        other = self.workspace / "other"
        other.mkdir()
        (other / "task_list.py").write_text(BUGGY_SOURCE)
        verifier = Verifier(other, Executor(), CASES)
        other_agent = Agent(ScriptedModel([[message()]]), Tools(other, verifier), verifier, "fix")
        first, _ = self.agent()
        self.assertEqual(first.run("fix")["status"], "passed")
        second = other_agent.run("fix", max_steps=1)
        self.assertFalse(second["verification"]["passed"])
        self.assertEqual((other / "task_list.py").read_text(), BUGGY_SOURCE)

    def test_fixed_cases_are_copied_and_empty_suite_is_rejected(self):
        cases = deepcopy(CASES)
        verifier = Verifier(self.workspace, self.executor, cases)
        cases[0]["expected"] = []
        result = verifier.check()
        self.assertFalse(result["cases"][0]["passed"])
        result["cases"][0]["expected"].clear()
        self.assertEqual(verifier.check()["cases"][0]["expected"], [TASKS[1]])
        with self.assertRaises(ValueError):
            Verifier(self.workspace, self.executor, [])

    def test_json_types_prevent_boolean_from_passing_as_integer(self):
        self.source.write_text('print("true")\n')
        verifier = Verifier(self.workspace, self.executor,
                            [{"name": "strict", "input": {}, "expected": 1}])
        result = verifier.check()
        self.assertIs(result["cases"][0]["actual"], True)
        self.assertFalse(result["passed"])

    def test_fake_pass_invalid_json_and_nonzero_exit_fail(self):
        for source in ('print("all passed")\n', 'raise SystemExit(0)\n',
                       'print("[]")\nraise SystemExit(1)\n'):
            with self.subTest(source=source):
                self.source.write_text(source)
                result = self.verifier.check()
                self.assertFalse(result["passed"])
                self.assertEqual(len(result["cases"]), 5)

    def test_precise_replacement_rejects_empty_missing_and_duplicate_text(self):
        for old in ("", "not present", "import"):
            result = self.tools.execute(call("patch", "replace_text", path="task_list.py",
                                             old=old, new="replacement"))
            self.assertIn("error", result)
            self.assertEqual(self.source.read_text(), BUGGY_SOURCE)

    def test_tools_and_executor_reject_outside_and_absolute_paths(self):
        outside = self.workspace.parent / "outside.py"
        for path in ("../outside.py", str(self.source)):
            self.assertIn("error", self.tools.execute(call("read", "read_file", path=path)))
            with self.assertRaises(ValueError):
                self.executor.run_python(self.workspace, path, {})
        (self.workspace / "link.py").symlink_to(outside)
        self.assertIn("error", self.tools.execute(call("read", "read_file", path="link.py")))
        with self.assertRaises(ValueError):
            self.executor.run_python(self.workspace, "link.py", {})

    def test_bad_tool_arguments_return_error(self):
        for arguments in ("not json", "[]", '{"unexpected":true}'):
            result = self.tools.execute({"name": "run_tests", "arguments": arguments})
            self.assertIn("error", result)

    def test_missing_call_id_and_empty_model_output_are_rejected(self):
        for script in ([[]], [[{"type": "function_call", "name": "run_tests", "arguments": "{}"}]]):
            agent, _ = self.agent(script)
            with self.assertRaises(ValueError):
                agent.run("fix")

    def test_executor_uses_real_python_isolated_mode_and_empty_environment(self):
        path = self.workspace / "environment.py"
        path.write_text('import json, os, sys\nprint(json.dumps({"key": os.getenv("CH01_SECRET"), '
                        '"isolated": sys.flags.isolated, "cwd": os.getcwd()}))\n')
        with patch.dict("os.environ", {"CH01_SECRET": "not inherited"}):
            result = self.executor.run_python(self.workspace, path.name, {})
        actual = json.loads(result["stdout"])
        self.assertEqual(result["exit_code"], 0)
        self.assertIsNone(actual["key"])
        self.assertEqual(actual["isolated"], 1)
        self.assertEqual(Path(actual["cwd"]).resolve(), self.workspace.resolve())

    def test_real_timeout_is_reported_and_verifier_cannot_pass_it(self):
        self.source.write_text('import time\nprint("started", flush=True)\ntime.sleep(10)\n')
        result = self.executor.run_python(self.workspace, "task_list.py", {}, timeout=0.1)
        self.assertIsNone(result["exit_code"])
        self.assertTrue(result["timed_out"])
        self.assertIn("started", result["stdout"])
        verifier = Verifier(self.workspace, self.executor,
                            [{"name": "timeout", "input": {}, "expected": []}])
        verified = verifier.check()
        self.assertFalse(verified["passed"])
        self.assertTrue(all(row["timed_out"] for row in verified["cases"]))

    def test_deepseek_http_adapter_is_mocked_and_preserves_full_output(self):
        expected = [{"type": "reasoning", "summary": [], "encrypted_content": "opaque"}, message()]
        body = io.BytesIO(json.dumps({"status": "completed", "output": expected}).encode())
        with patch("mini_codex.minimal.model.urllib.request.urlopen", return_value=body) as open_url:
            output = DeepSeekModel("test-key", "test-model").sample({"input": [], "tools": []})
        self.assertEqual(output, expected)
        request = open_url.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.deepseek.com/responses")
        self.assertEqual(payload["reasoning"], {"effort": "none"})
        self.assertEqual(payload["max_output_tokens"], 2048)
        self.assertIs(payload["stream"], False)

    def test_deepseek_errors_do_not_include_credentials_or_response_body(self):
        response_body = io.BytesIO(b"test-key private response body")
        error = urllib.error.HTTPError("https://api.deepseek.com/responses", 401,
                                       "test-key secret response", {}, response_body)
        self.addCleanup(error.close)
        with patch("mini_codex.minimal.model.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                DeepSeekModel("test-key", "test-model").sample({})
        self.assertEqual(str(caught.exception), "DeepSeek 请求失败，HTTP 401")
        self.assertTrue(response_body.closed, "HTTP error responses must be closed before raising")
        body = io.BytesIO(b'{"status":"incomplete","output":[]}')
        with patch("mini_codex.minimal.model.urllib.request.urlopen", return_value=body):
            with self.assertRaisesRegex(RuntimeError, "未完成"):
                DeepSeekModel("test-key", "test-model").sample({})

    def test_synthetic_configuration_requires_exact_deepseek_base(self):
        path = self.workspace / "synthetic-settings.txt"
        prefix = 'OPENAI_API_KEY="test-key"\nOPENAI_MODEL=\'test-model\'\n'
        path.write_text(prefix + "OPENAI_BASE_URL=https://api.deepseek.com\n")
        model = DeepSeekModel.from_env_file(path)
        self.assertEqual(model.model, "test-model")
        for base in ("https://api.deepseek.com/", "https://other.invalid"):
            path.write_text(prefix + "OPENAI_BASE_URL=" + base)
            with self.assertRaises(ValueError):
                DeepSeekModel.from_env_file(path)


if __name__ == "__main__":
    unittest.main()
