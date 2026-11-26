import unittest
import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from demos.apim_litellm.operator.apim_litellm import verify, completed_a2a_text, completed_response_text, main


DEPLOYMENT = {"apim": {"gateway_url": "https://gateway.example.com", "caller_audience": "api://gateway"}}


def response(value, status=200):
    return Mock(status_code=status, headers={"Content-Type": "application/json"}, json=Mock(return_value=value))


class AcceptanceTests(unittest.TestCase):
    def test_agent_responses_require_success_and_actual_assistant_output(self):
        response = {
            "status": "completed", "error": None,
            "output": [{"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "PRIVATE_RESPONSES_OK"},
            ]}],
        }
        self.assertEqual(completed_response_text(response), "PRIVATE_RESPONSES_OK")
        for status, error in [("failed", None), ("in_progress", None), ("completed", {"code": "failed"})]:
            with self.subTest(status=status, error=error), self.assertRaisesRegex(RuntimeError, "did not complete"):
                completed_response_text({**response, "status": status, "error": error})
        response["output"][0]["role"] = "user"
        with self.assertRaisesRegex(RuntimeError, "no assistant message"):
            completed_response_text(response)

    def test_user_consent_configuration_does_not_replace_an_actual_user_login(self):
        identity = {"consent_mode": "user", "user_consent_configured": True, "admin_consent_requested": False}
        with patch("demos.apim_litellm.operator.apim_litellm.credential"), \
             patch("demos.apim_litellm.operator.apim_litellm.read_key") as key, \
             patch("demos.apim_litellm.operator.apim_litellm.requests.post") as post:
            with self.assertRaisesRegex(ValueError, "actual user login"):
                verify(DEPLOYMENT, scenario="user-obo", identity_receipt=identity)
        key.assert_not_called()
        post.assert_not_called()

    def test_user_consent_acceptance_uses_the_selected_user_cache_not_operator_identity(self):
        identity = {"consent_mode": "user", "user_consent_configured": True, "admin_consent_requested": False,
                    "gateway": {"scope": "api://gateway/access_as_user"}}
        with patch("demos.apim_litellm.operator.apim_litellm.credential") as operator, \
             patch("demos.apim_litellm.operator.entra_setup.cached_user_token", return_value="delegated-user") as cached, \
             patch("demos.apim_litellm.operator.apim_litellm.read_key", return_value="sk-test"), \
             patch("demos.apim_litellm.operator.apim_litellm.requests.post", side_effect=RuntimeError("stop after user selection")):
            with self.assertRaisesRegex(RuntimeError, "stop after user selection"):
                verify(DEPLOYMENT, scenario="user-obo", identity_receipt=identity, user_token_cache="encrypted")
        cached.assert_called_once_with(identity, "encrypted")
        operator.return_value.get_token.assert_not_called()

    def test_failed_live_rerun_replaces_stale_success_and_restores_kubeconfig(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deployment = root / "deployment.json"
            deployment.write_text(json.dumps(DEPLOYMENT), encoding="utf-8")
            (root / "kubeconfig").write_text("operator-context", encoding="utf-8")
            receipt = root / "acceptance.json"
            receipt.write_text('{"status":"passed"}', encoding="utf-8")
            with patch.dict(os.environ, {"KUBECONFIG": "unrelated-original-context"}), \
                 patch("sys.argv", ["apim_litellm", "verify", "--deployment", str(deployment), "--output", str(receipt)]), \
                 patch("demos.apim_litellm.operator.apim_litellm.verify", side_effect=RuntimeError("explicit live failure")):
                with self.assertRaisesRegex(RuntimeError, "explicit live failure"):
                    main()
                self.assertEqual(os.environ["KUBECONFIG"], "unrelated-original-context")
            self.assertEqual(json.loads(receipt.read_text(encoding="utf-8"))["status"], "failed")

    def test_declarative_output_cannot_pass_by_echoing_the_prompt_in_history(self):
        task = {"status": {"state": "TASK_STATE_COMPLETED"},
                "history": [{"role": "ROLE_USER", "parts": [{"text": "DECLARATIVE_OK"}]}],
                "artifacts": [{"parts": [{"text": "WRONG_OUTPUT"}]}]}
        self.assertEqual(completed_a2a_text({"task": task}), "WRONG_OUTPUT")
        task["status"]["state"] = "TASK_STATE_FAILED"
        with self.assertRaisesRegex(RuntimeError, "did not complete"):
            completed_a2a_text({"task": task})
        with self.assertRaisesRegex(RuntimeError, "agent message"):
            completed_a2a_text({"message": {"role": "ROLE_USER", "parts": [{"text": "DECLARATIVE_OK"}]}})

    def run_acceptance(self, task_result, saved_result=None, tool_error=False, permissions=False, mcp_denial=None,
                       playground_content="MCP_WORKLOAD_IDENTITY_OK"):
        task = {
            "id": "task-1",
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [{"artifactId": "artifact-1", "parts": [{"text": "Echo: hello"}]}],
        }
        replies = [response({}, 401) for _ in range(3)]
        if permissions:
            replies += [response({}, 403), response(mcp_denial), response({}, 403)]
        replies += [
            response({"choices": [{"message": {"content": "GATEWAY_OK"}}], "usage": {"total_tokens": 10}}),
            response({}, 403),
            response({"choices": [{"message": {"content": playground_content}}]}),
            response({"jsonrpc": "2.0", "id": 101, **(task_result or {"result": {"task": task}})}),
            response({"jsonrpc": "2.0", "id": 102, "result": saved_result or {"task": task}}),
        ]
        with patch("demos.apim_litellm.operator.apim_litellm.credential") as identity, \
             patch("demos.apim_litellm.operator.apim_litellm.read_key", return_value="sk-test-virtual"), \
             patch("demos.apim_litellm.operator.apim_litellm.requests.get", return_value=response({}, 401)), \
             patch("demos.apim_litellm.operator.apim_litellm.requests.post", side_effect=replies), \
             patch("demos.apim_litellm.operator.apim_litellm.MCPClient") as mcp:
            identity.return_value.get_token.return_value.token = "test-token"
            mcp.return_value.headers = {}
            mcp.return_value.call.side_effect = [
                {"tools": [{"name": "blob-read_blob"}]},
                {"isError": tool_error, "content": [{"text": "MCP_WORKLOAD_IDENTITY_OK"}]},
            ]
            return verify(DEPLOYMENT, validate_permissions=permissions)

    def test_native_mcp_permission_denial_inside_http_200_is_verified_not_ignored(self):
        denial = {"jsonrpc": "2.0", "id": 50, "error": {
            "code": -32600, "message": "The key is not allowed to access the requested MCP servers: blob",
        }}
        result = self.run_acceptance(None, permissions=True, mcp_denial=denial)
        self.assertTrue(result["checks"]["models_only_key_backend_denials"])
        with self.assertRaisesRegex(RuntimeError, "unexpectedly received MCP access"):
            self.run_acceptance(None, permissions=True, mcp_denial={"jsonrpc": "2.0", "id": 50, "result": {"tools": []}})

    def test_completed_native_task_and_saved_artifacts_are_required(self):
        result = self.run_acceptance(None)
        self.assertEqual(result["checks"]["a2a"]["artifact"], "Echo: hello")
        self.assertTrue(result["checks"]["a2a"]["saved_task_retrieved"])
        with self.assertRaisesRegex(RuntimeError, "saved Task differs"):
            self.run_acceptance(None, {"task": {"id": "different", "artifacts": []}})

    def test_http_success_with_a2a_protocol_error_fails_acceptance(self):
        with self.assertRaisesRegex(RuntimeError, "MCP error"):
            self.run_acceptance({"error": {"code": -32603, "message": "Backend task failed"}})

    def test_saved_artifacts_allow_only_omitted_plain_text_media_type(self):
        saved = {
            "id": "task-1",
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [{
                "artifactId": "artifact-1",
                "parts": [{"text": "Echo: hello", "mediaType": "text/plain"}],
            }],
        }
        result = self.run_acceptance(None, {"task": saved})
        self.assertTrue(result["checks"]["a2a"]["saved_task_retrieved"])
        for field, value in [
            ("mediaType", "text/html"), ("artifactId", "different"), ("text", "different"),
        ]:
            with self.subTest(changed_field=field):
                changed = deepcopy(saved)
                artifact = changed["artifacts"][0]
                target = artifact if field == "artifactId" else artifact["parts"][0]
                target[field] = value
                with self.assertRaisesRegex(RuntimeError, "saved Task differs"):
                    self.run_acceptance(None, {"task": changed})

    def test_completed_legacy_task_without_native_envelope_fails_acceptance(self):
        legacy_task = {
            "id": "task-1",
            "status": {"state": "completed"},
            "artifacts": [{"parts": [{"text": "Echo: hello"}]}],
        }
        with self.assertRaisesRegex(RuntimeError, "native A2A 1.0 SendMessage task envelope"):
            self.run_acceptance({"result": legacy_task})

    def test_mcp_error_cannot_pass_by_containing_marker(self):
        with self.assertRaisesRegex(RuntimeError, "MCP did not read"):
            self.run_acceptance(None, tool_error=True)

    def test_playground_must_return_blob_contents_not_merely_claim_tool_access(self):
        result = self.run_acceptance(None)
        self.assertTrue(result["checks"]["mcp_playground"]["actual_blob_marker"])
        for content in [None, "I can use the blob MCP tool"]:
            with self.subTest(content=content), self.assertRaisesRegex(RuntimeError, "actual Blob marker"):
                self.run_acceptance(None, playground_content=content)
