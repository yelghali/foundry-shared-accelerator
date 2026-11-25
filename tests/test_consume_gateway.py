import os
import io
import json
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import Mock, patch

from samples.consume_gateway import main


class ConsumerGatewayTests(unittest.TestCase):
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
