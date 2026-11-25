import os
import io
import json
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import Mock, patch

from samples.consume_gateway import main


class ConsumerGatewayTests(unittest.TestCase):
    def test_verbose_responses_reports_server_identity_timing_and_safe_headers(self):
        payload = {
            "status": "completed",
            "agent_reference": {"name": "gateway-hosted-dual", "version": "5"},
            "agent_session_id": "session-123",
            "output": [{"type": "message", "role": "assistant", "content": [],
                        "agent_reference": {"name": "gateway-hosted-dual", "version": "5"}}],
        }
        with patch.dict(os.environ, {"LITELLM_VIRTUAL_KEY": "sk-do-not-log", "GATEWAY_ACCESS_TOKEN": "user-do-not-log"}), \
             patch("sys.argv", ["consume_gateway", "--endpoint", "https://gateway.example/ai",
                                "--kind", "hosted-agent-responses"]), \
             patch("samples.consume_gateway.requests.post") as post, \
             patch("samples.consume_gateway.time.perf_counter", side_effect=[10.0, 12.25]), \
             redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as diagnostic:
            post.return_value = Mock(
                status_code=200,
                headers={
                    "x-litellm-model-api-base": "https://user:backend-password@demo.services.ai.azure.com/agents/hosted/openai?api-key=query-secret#fragment-secret",
                    "request-id": "request-123",
                    "Authorization": "Bearer response-secret",
                    "x-ratelimit-key": "private-key-hash",
                },
                json=Mock(return_value=payload),
            )
            main()
        text = diagnostic.getvalue()
        self.assertIn("client round trip: 2.250s", text)
        self.assertEqual(text.count("Server agent_reference: name=gateway-hosted-dual; version=5"), 1)
        self.assertIn("Hosted session ID (server): session-123", text)
        self.assertIn("https://demo.services.ai.azure.com/agents/hosted/openai", text)
        self.assertIn("request-id: request-123", text)
        for secret in ("backend-password", "query-secret", "fragment-secret", "response-secret",
                       "private-key-hash", "sk-do-not-log", "user-do-not-log"):
            self.assertNotIn(secret, text)
        self.assertEqual(json.loads(output.getvalue()), payload)

    def test_verbose_a2a_reports_task_but_never_invents_a_server_reference(self):
        result = {"task": {
            "id": "task-123", "contextId": "context-123",
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [{"artifactId": "artifact-123", "parts": [{"text": "HELLO"}]}],
            "history": [{"role": "ROLE_USER", "agent_reference": {"name": "caller-supplied-name"}}],
        }}
        with patch.dict(os.environ, {"LITELLM_VIRTUAL_KEY": "sk-consumer", "GATEWAY_ACCESS_TOKEN": "user-assertion"}), \
             patch("sys.argv", ["consume_gateway", "--endpoint", "https://gateway.example/ai",
                                "--kind", "hosted-agent", "--agent-name", "hosted"]), \
             patch("samples.consume_gateway.requests.post") as post, \
             redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as diagnostic:
            post.return_value = Mock(
                status_code=200, headers={"Content-Type": "application/json"},
                json=Mock(return_value={"jsonrpc": "2.0", "id": 1, "result": result}),
            )
            main()
        text = diagnostic.getvalue()
        self.assertIn("A2A-Version: 1.0", text)
        self.assertIn("method: SendMessage", text)
        self.assertIn("client-side convention, not server attestation", text)
        self.assertIn("Server agent_reference: not returned.", text)
        self.assertNotIn("Server agent_reference: name=", text)
        self.assertIn("A2A Task ID (server): task-123", text)
        self.assertIn("TASK_STATE_COMPLETED", text)
        self.assertIn("artifact-123", text)
        self.assertEqual(json.loads(output.getvalue()), result)

    def test_request_urls_are_reported_without_credentials_or_corrupting_json_results(self):
        for kind, path, payload, extra in [
            ("hosted-agent", "/a2a/hosted", {"jsonrpc": "2.0", "id": 1, "result": {"task": {}}},
             ["--agent-name", "hosted"]),
            ("hosted-agent-responses", "/v1/responses", {"output": []}, []),
        ]:
            with self.subTest(kind=kind), \
                 patch.dict(os.environ, {"LITELLM_VIRTUAL_KEY": "sk-do-not-log", "GATEWAY_ACCESS_TOKEN": "user-do-not-log"}), \
                 patch("sys.argv", ["consume_gateway", "--endpoint", "https://gateway.example/ai",
                                    "--kind", kind, *extra]), \
                 patch("samples.consume_gateway.requests.post") as post, \
                 redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as diagnostic:
                post.return_value = Mock(
                    status_code=200, headers={"Content-Type": "application/json"},
                    json=Mock(return_value=payload),
                )
                main()
                url = "https://gateway.example/ai" + path
                self.assertIn("POST " + url, diagnostic.getvalue())
                self.assertEqual(post.call_args.args[0], url)
                self.assertNotIn("sk-do-not-log", diagnostic.getvalue())
                self.assertNotIn("user-do-not-log", diagnostic.getvalue())
                self.assertIsInstance(json.loads(output.getvalue()), dict)

    @patch("samples.consume_gateway.requests.post")
    def test_private_agent_responses_uses_fixed_alias_and_responses_route(self, post):
        post.return_value = Mock(json=Mock(return_value={"output": []}))
        with patch.dict(os.environ, {"LITELLM_VIRTUAL_KEY": "sk-consumer", "GATEWAY_ACCESS_TOKEN": "user-assertion"}), \
             patch("sys.argv", ["consume_gateway", "--endpoint", "https://gateway.example/ai",
                                "--kind", "hosted-agent-responses", "--prompt", "hello"]), \
             patch("builtins.print"):
            main()
        self.assertEqual(post.call_args.args[0], "https://gateway.example/ai/v1/responses")
        self.assertEqual(post.call_args.kwargs["json"], {"model": "hosted-agent-responses", "input": "hello"})

    @patch("samples.consume_gateway.requests.post")
    def test_application_key_and_entra_token_remain_separate(self, post):
        post.return_value = Mock(json=Mock(return_value={"choices": [{"message": {"content": "HELLO"}}]}))
        with patch.dict(os.environ, {"LITELLM_VIRTUAL_KEY": "sk-consumer", "GATEWAY_ACCESS_TOKEN": "user-assertion"}), \
             patch("sys.argv", ["consume_gateway", "--endpoint", "https://gateway.example/ai", "--kind", "model"]), \
             patch("builtins.print"):
            main()
        self.assertEqual(post.call_args.kwargs["headers"], {
            "Authorization": "Bearer user-assertion", "x-litellm-api-key": "Bearer sk-consumer",
        })

    @patch("samples.consume_gateway.requests.post")
    @patch("samples.consume_gateway.load_json", return_value={"gateway": {"app_id": "test"}})
    @patch("samples.consume_gateway.cached_user_token", side_effect=RuntimeError("Real user login required"))
    def test_cache_authentication_failure_never_falls_back_to_another_identity(self, token, identity, post):
        with patch.dict(os.environ, {"LITELLM_VIRTUAL_KEY": "sk-consumer", "GATEWAY_ACCESS_TOKEN": "different-user"}), \
             patch("sys.argv", ["consume_gateway", "--endpoint", "https://gateway.example/ai", "--kind", "mcp",
                                "--identity", "identity.json", "--user-token-cache", "encrypted-cache"]):
            with self.assertRaisesRegex(RuntimeError, "Real user login required"):
                main()
        post.assert_not_called()
