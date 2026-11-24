import os
import unittest
from unittest.mock import Mock, patch

from samples.consume_gateway import main


class ConsumerGatewayTests(unittest.TestCase):
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
