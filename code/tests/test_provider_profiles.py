"""Protocol tests use synthetic replies, not evidence of a working model service."""
import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from mini_codex.contracts import CallContext, ModelRequest
from mini_codex.model import ResponsesModel
from mini_codex.model_http import request as http_request


class ProviderProfileTests(unittest.TestCase):
    def test_deepseek_wire_path_payload_and_raw_items_via_real_http_child(self):
        captured = []
        output = [
            {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "synthetic"}]},
            {"type": "function_call", "call_id": "read-1", "name": "read",
             "arguments": '{"path":"task_list.py"}', "status": "completed"},
        ]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                captured.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                body = json.dumps({"id": "response-fixture", "status": "completed",
                                   "output": output, "usage": {"total_tokens": 3}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("x-request-id", "request-fixture")
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(os.environ, {"OPENAI_BASE_URL": "https://api.deepseek.com/",
                                         "OPENAI_API_KEY": "test-key", "OPENAI_MODEL": "fixture"}):
                adapter = ResponsesModel(endpoint=("http", "127.0.0.1", server.server_port))
                context = CallContext("run", "turn", "sample", time.monotonic() + 5)
                result = adapter.sample(ModelRequest(context, "snapshot", "test", tuple(output), (),
                                                     {"reasoning_effort": "none"}))
            self.assertEqual(result.response_status, "completed")
            self.assertEqual(result.output_items, tuple(output))
            self.assertEqual((result.request_id, result.response_id), ("request-fixture", "response-fixture"))
            self.assertEqual(adapter.last_cleanup, "confirmed")
            path, payload = captured[0]
            self.assertEqual(path, "/responses")
            self.assertEqual(payload["input"], output)
            self.assertEqual(payload["reasoning"], {"effort": "none"})
            self.assertEqual(payload["max_output_tokens"], 2048)
            self.assertNotIn("store", payload)
            self.assertNotIn("include", payload)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_provider_destinations_are_exact_and_unknown_urls_fail_closed(self):
        env = {"OPENAI_API_KEY": "test-key", "OPENAI_MODEL": "fixture"}
        for base, endpoint, path in (
            ("https://api.deepseek.com", ("https", "api.deepseek.com", 443), "/responses"),
            ("https://api.openai.com/v1", ("https", "api.openai.com", 443), "/v1/responses"),
        ):
            with patch.dict(os.environ, dict(env, OPENAI_BASE_URL=base)):
                adapter = ResponsesModel()
                self.assertEqual(adapter.endpoint, endpoint)
                self.assertEqual(adapter.api_path, path)
        for bad in ("http://api.deepseek.com", "https://api.deepseek.com.evil.invalid",
                    "https://key@api.deepseek.com", "https://api.deepseek.com/v1", "/", "////"):
            with self.subTest(base=bad), patch.dict(os.environ, dict(env, OPENAI_BASE_URL=bad)):
                with self.assertRaisesRegex(ValueError, "unsupported_OPENAI_BASE_URL"):
                    ResponsesModel()

    def test_http_child_rejects_cross_provider_path_before_network(self):
        with patch("mini_codex.model_http.http.client.HTTPSConnection") as connect:
            for host, path in (("api.deepseek.com", "/v1/responses"),
                               ("api.openai.com", "/responses"),
                               ("unknown.invalid", "/responses")):
                result = http_request({"endpoint": ("https", host, 443), "api_path": path,
                                       "key": "test-key", "timeout": 1, "payload": {}})
                self.assertEqual(result["provider_error"], "invalid_endpoint")
            connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
