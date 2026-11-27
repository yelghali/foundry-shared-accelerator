import importlib
import json
from unittest.mock import Mock, patch

import pytest

from litellm_demo.control import save_receipt
from litellm_demo.apim_litellm import mcp_authorization_environment
from litellm_demo.scenario_checks import ScenarioChecks


def response(payload):
    return Mock(headers={"Content-Type": "application/json"}, json=Mock(return_value=payload))


def test_model_requires_actual_assistant_marker():
    with patch("litellm_demo.scenario_checks.requests.Session") as session:
        session.return_value.post.return_value = response({"choices": [{"message": {"content": "wrong"}}]})
        with ScenarioChecks("https://gateway.example/ai", "token", "sk-key") as checks:
            with pytest.raises(RuntimeError, match="exact assistant"):
                checks.model("EXPECTED")
            assert checks.results == []


def test_hosted_private_mcp_requires_completed_tool_output_not_an_echo():
    payload = {
        "status": "completed", "error": None,
        "agent_reference": {"name": "gateway-hosted-dual"},
        "output": [{"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "MCP_WORKLOAD_IDENTITY_OK"},
        ]}],
    }
    with ScenarioChecks("https://gateway.example/ai", "token", "sk-key") as checks:
        with patch.object(checks, "post", return_value=response(payload)) as post:
            checks.hosted_private_mcp()
            assert post.call_args.args[1]["input"] == "/mcp-read gateway-check.txt"
        payload["output"][0]["role"] = "user"
        with patch.object(checks, "post", return_value=response(payload)):
            with pytest.raises(RuntimeError, match="no assistant message"):
                checks.hosted_private_mcp()


@pytest.mark.parametrize("delegated", [False, True])
def test_mcp_checks_identity_and_actual_blob_tool_result(delegated):
    claims = {"oid": "expected-user-or-service", "azp": "expected-client",
              "authentication_mode": "user-obo" if delegated else "service", "scp": "tools.read"}
    with patch("litellm_demo.scenario_checks.MCPClient") as factory:
        mcp = factory.return_value
        mcp.headers = {}
        mcp.call.side_effect = [
            {"tools": [{"name": "blob-whoami"}, {"name": "blob-read_blob"}]},
            {"structuredContent": claims},
            {"content": [{"type": "text", "text": "MCP_WORKLOAD_IDENTITY_OK"}]},
        ]
        with ScenarioChecks("https://gateway.example/ai", "token", "sk-key") as checks:
            checks.mcp(expected_client_id="expected-client",
                       expected_principal_ids=["expected-user-or-service"], delegated=delegated)
            assert checks.results[0]["identity_matched"]
        mcp.session.close.assert_called_once()


def test_mcp_wrong_backend_identity_does_not_read_blob_or_report_success():
    with patch("litellm_demo.scenario_checks.MCPClient") as factory:
        mcp = factory.return_value
        mcp.headers = {}
        mcp.call.side_effect = [
            {"tools": [{"name": "whoami"}, {"name": "read_blob"}]},
            {"structuredContent": {"oid": "wrong", "azp": "expected", "authentication_mode": "service"}},
        ]
        with ScenarioChecks("https://gateway.example/ai", "token", "sk-key") as checks:
            with pytest.raises(RuntimeError, match="backend identity"):
                checks.mcp(expected_client_id="expected", expected_principal_ids=["allowed"], delegated=False)
            assert checks.results == []
        assert mcp.call.call_count == 2
        mcp.session.close.assert_called_once()


def test_background_submission_polls_native_v1_and_uses_artifacts_not_history():
    with patch("litellm_demo.scenario_checks.requests.Session") as session:
        session.return_value.post.side_effect = [
            response({"jsonrpc": "2.0", "id": 1, "result": {"task": {
                "id": "actual-task", "status": {"state": "TASK_STATE_SUBMITTED"},
            }}}),
            response({"jsonrpc": "2.0", "id": 2, "result": {
                "id": "actual-task", "status": {"state": "TASK_STATE_COMPLETED"},
                "history": [{"role": "ROLE_USER", "parts": [{"text": "EXPECTED"}]}],
                "artifacts": [{"parts": [{"text": "WRONG"}]}],
            }}),
        ]
        with ScenarioChecks("https://gateway.example/ai", "token", "sk-key") as checks:
            with pytest.raises(RuntimeError, match="artifacts"):
                checks.a2a_job("hosted", "EXPECTED")
            assert checks.results == []
        calls = session.return_value.post.call_args_list
        assert calls[0].kwargs["json"]["params"]["configuration"]["returnImmediately"] is True
        assert calls[1].kwargs["json"]["method"] == "GetTask"
        assert calls[1].kwargs["json"]["params"] == {"id": "actual-task"}
        assert all(call.kwargs["headers"]["A2A-Version"] == "1.0" for call in calls)


@pytest.mark.parametrize("module_name", [
    "samples.test_shared_service_identity",
    "samples.test_dedicated_service_identity",
    "samples.test_delegated_user_identity",
])
def test_named_scripts_replace_stale_pass_receipt_on_failure(module_name, tmp_path, monkeypatch):
    module = importlib.import_module(module_name)
    receipt = tmp_path / "receipt.json"
    receipt.write_text('{"status":"passed"}')
    monkeypatch.setattr("sys.argv", ["test-scenario", "--output", str(receipt)])
    monkeypatch.setattr(module, "load_json", Mock(side_effect=RuntimeError("explicit setup failure")))
    with pytest.raises(RuntimeError, match="explicit setup failure"):
        module.main()
    assert json.loads(receipt.read_text())["status"] == "failed"


def test_portable_receipts_reject_nested_credentials(tmp_path):
    for field in ("Authorization", "client_secret", "access_token"):
        with pytest.raises(ValueError, match="Credential field"):
            save_receipt(tmp_path / "result.json", {"nested": [{"key": {field: "do-not-write"}}]})
    assert not (tmp_path / "result.json").exists()


def test_hosted_mcp_policy_is_explicit_scoped_and_never_added_to_user_obo(tmp_path):
    file = tmp_path / "policy.json"
    policy = {
        "namespace": "gateway",
        "client_ids": ["00000000-0000-0000-0000-000000000001"],
        "principal_ids": ["00000000-0000-0000-0000-000000000002"],
    }
    file.write_text(json.dumps(policy))
    result = mcp_authorization_environment(file, "gateway")
    assert result["MCP_FOUNDRY_CLIENT_IDS"] == policy["client_ids"][0]
    assert result["MCP_FOUNDRY_PRINCIPAL_IDS"] == policy["principal_ids"][0]
    assert mcp_authorization_environment(file, "gateway-user-obo") == {}
    policy["namespace"] = "gateway-user-obo"
    file.write_text(json.dumps(policy))
    with pytest.raises(ValueError, match="service-identity"):
        mcp_authorization_environment(file, "gateway-user-obo")
    policy["namespace"] = "gateway"
    policy["principal_ids"] = []
    file.write_text(json.dumps(policy))
    with pytest.raises(ValueError, match="principal_ids"):
        mcp_authorization_environment(file, "gateway")
