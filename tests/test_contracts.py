import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from accelerator.agent import validate_project
from accelerator.control import AzureRequestError, ControlPlane, save_receipt
from accelerator.gateway import agent_openapi, agent_registration_names, configure_models, grant_agent_consumer, prepare_team, register_agent
from accelerator.knowledge import verify_ingestion, verify_project_agent
from accelerator.mcp import agent_arguments, completed_agent_output, parse_rpc_response
from accelerator.usage import exported_metrics, project_showback


PROJECT = {
    "id": "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/test/providers/Microsoft.CognitiveServices/accounts/fsa-agents/projects/team-alpha",
    "name": "team-alpha",
    "endpoint": "https://fsa-agents.services.ai.azure.com/api/projects/team-alpha",
}


class ContractTests(unittest.TestCase):
    @patch("accelerator.knowledge.AIProjectClient")
    def test_knowledge_agent_uses_team_connection(self, project_client):
        client = project_client.return_value.__enter__.return_value
        client.agents.create_version.return_value = Mock(status="Succeeded", version="1")
        client.get_openai_client.return_value.responses.create.return_value.output_text = (
            "TEAM-ALPHA KNOWLEDGE INGESTION VERIFIED"
        )
        project = {
            "name": "team-alpha", "endpoint": PROJECT["endpoint"],
            "model_reference": "platform-models-team-alpha/approved-chat",
            "knowledge": {"knowledge_bases": {
                "team-alpha-kb": {
                    "mcp_endpoint": "https://team.search.windows.net/knowledgebases/team-alpha-kb/mcp",
                    "connection_id": PROJECT["id"] + "/connections/team-alpha-kb-iq",
                },
            }},
        }
        result = verify_project_agent(project, Mock())
        self.assertTrue(result["project_identity_knowledge_retrieval"])
        definition = client.agents.create_version.call_args.kwargs["definition"]
        self.assertEqual(definition.tools[0].project_connection_id, "team-alpha-kb-iq")
        self.assertEqual(definition.tools[0].allowed_tools, ["knowledge_base_retrieve"])

    @patch("accelerator.knowledge.time.sleep")
    @patch("accelerator.knowledge.requests.get")
    @patch("accelerator.knowledge.requests.post")
    @patch("accelerator.knowledge.requests.put")
    def test_knowledge_verification_uses_blob_source_and_minimal_intents(self, put, post, get, sleep):
        identity = Mock()
        identity.get_token.return_value.token = "test-token"
        deployment = {
            "shared_storage": {"blob_endpoint": "https://shared.blob.core.windows.net/"},
            "search": {"endpoint": "https://test.search.windows.net"},
        }
        project = {
            "name": "team-alpha",
            "containers": {"team-alpha-docs": "/container"},
            "knowledge": {"knowledge_bases": {"team-alpha-kb": {}}},
        }
        put.return_value.status_code = 201
        post.side_effect = [
            Mock(status_code=202),
            Mock(ok=True, json=Mock(return_value={"response": [{"content": "TEAM-ALPHA KNOWLEDGE INGESTION VERIFIED"}]})),
        ]
        get.return_value = Mock(ok=True, json=Mock(return_value={"lastResult": {"status": "success", "itemsProcessed": 1}}))
        result = verify_ingestion(deployment, project, identity)
        self.assertTrue(result["grounded_marker_found"])
        self.assertEqual(post.call_args.kwargs["json"]["knowledgeSourceParams"][0]["kind"], "azureBlob")
        self.assertIn("intents", post.call_args.kwargs["json"])
        self.assertNotIn("messages", post.call_args.kwargs["json"])
        put.assert_called_once()
        sleep.assert_called_once_with(10)

    def test_project_identity_matches_endpoint(self):
        validate_project(PROJECT)

    def test_foreign_project_endpoint_rejected(self):
        for endpoint in (
            "https://fsa-agents.services.ai.azure.com/api/projects/team-beta",
            "https://example.com/api/projects/team-alpha",
            "http://fsa-agents.services.ai.azure.com/api/projects/team-alpha",
            PROJECT["endpoint"] + "?different=target",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                validate_project({**PROJECT, "endpoint": endpoint})

    def test_receipt_rejects_nested_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            with self.assertRaises(ValueError):
                save_receipt(path, {"result": [{"primaryKey": "never-write"}]})
            self.assertFalse(path.exists())

    def test_receipt_allows_nonsecret_key_resource_id(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            save_receipt(path, {"consumer_key_id": "/resource/apiKeys/consumer"})
            self.assertEqual(json.loads(path.read_text())["consumer_key_id"], "/resource/apiKeys/consumer")

    def test_openapi_exposes_exact_agent_not_project_catch_all(self):
        spec = agent_openapi(PROJECT, {"name": "assistant"})
        self.assertEqual(len(spec["paths"]), 1)
        path = next(iter(spec["paths"]))
        self.assertTrue(path.endswith("/agents/assistant/endpoint/protocols/openai/responses"))
        body = spec["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(body["properties"]["store"]["enum"], [False])
        self.assertEqual(spec["paths"][path]["post"]["parameters"][0]["schema"]["default"], "v1")

    def test_foreign_agent_registration_rejected_before_writes(self):
        control = Mock(spec=ControlPlane)
        with self.assertRaises(ValueError):
            register_agent(control, PROJECT, {"project_id": PROJECT["id"] + "-foreign", "name": "assistant"})
        control.grant.assert_not_called()

    def test_consumer_grant_is_limited_to_active_project_agent_asset(self):
        control = Mock(spec=ControlPlane)
        control.request.return_value = {"properties": {"accessState": "active", "type": "openApi"}}
        gateway = "/subscriptions/test/resourceGroups/test/providers/Microsoft.ApiManagement/aigateways/gateway"
        server = gateway + "/workspaces/default/toolServers/team-alpha-assistant"
        project = {**PROJECT, "gateway": {"id": gateway}, "agents": {
            "assistant": {"agent": {"project_id": PROJECT["id"]}, "tool_server_id": server},
        }}
        principal = "c6982fed-3a65-46cb-a80f-d5093b79e3e4"
        receipt = grant_agent_consumer(control, project, "assistant", principal)
        self.assertEqual(receipt["tool_server_id"], server)
        control.grant.assert_called_once()
        self.assertEqual(control.grant.call_args.args[1:3], (server, "dce03f8d-857f-4e40-a90d-67770fa6ecd2"))
        control.reset_mock()
        with self.assertRaises(ValueError):
            grant_agent_consumer(control, {**project, "agents": {"assistant": {
                "agent": {"project_id": PROJECT["id"]}, "tool_server_id": gateway + "-other/workspaces/default/toolServers/foreign",
            }}}, "assistant", principal)
        control.grant.assert_not_called()
        control.put.assert_not_called()

    def test_multi_agent_names_are_unique_and_bounded(self):
        one = agent_registration_names(PROJECT, {"name": "assistant"})
        two = agent_registration_names(PROJECT, {"name": "welcome"})
        self.assertNotEqual(one, two)
        self.assertEqual(one, agent_registration_names(PROJECT, {"name": "assistant"}))
        long = agent_registration_names({**PROJECT, "name": "p" * 40}, {"name": "a" * 40})
        self.assertLessEqual(len(long[0]), 63)
        self.assertLessEqual(len(long[2] + "_ask_agent"), 64)

    def test_existing_published_namespace_is_preserved(self):
        project = {**PROJECT, "agent": {"name": "assistant"}, "agent_tool_server_id": "/toolServers/legacy-name"}
        self.assertEqual(agent_registration_names(project, {"name": "assistant"}), ("legacy-name", "agent", "team-alpha"))

    def test_model_connections_are_keyless_and_project_unique(self):
        control = Mock(spec=ControlPlane)
        deployment = {"gateway": {"id": "/gateway", "endpoint": "https://gateway.example"},
                      "agent_foundry": {"id": PROJECT["id"].rsplit("/projects/", 1)[0]},
                      "model_version": "2024-07-18", "deployer_principal_id": "operator", "runner": {"principal_id": "runner"}}
        alpha = {**PROJECT, "principal_id": "alpha-mi", "chargeback_project_key": "alpha"}
        beta = {**alpha, "id": PROJECT["id"].replace("team-alpha", "team-beta"), "name": "team-beta",
                "endpoint": PROJECT["endpoint"].replace("team-alpha", "team-beta"), "principal_id": "beta-mi"}
        a = prepare_team(control, deployment, alpha)
        b = prepare_team(control, deployment, beta)
        self.assertNotEqual(a["model_connection_id"].rsplit("/", 1)[1], b["model_connection_id"].rsplit("/", 1)[1])
        properties = control.put.call_args.args[1]["properties"]
        self.assertEqual(properties["authType"], "ProjectManagedIdentity")
        self.assertEqual(properties["credentials"], {})
        control.runtime_key.assert_not_called()

    def test_agent_tool_arguments_handle_nested_body(self):
        schema = {"type": "object", "required": ["body"], "properties": {
            "body": {"type": "object", "required": ["input"], "properties": {
                "input": {"type": "string"}, "store": {"type": "boolean"}, "stream": {"type": "boolean"},
            }},
            "api-version": {"type": "string", "enum": ["2025-11-15-preview"]},
        }}
        self.assertEqual(agent_arguments(schema, "hello"), {
            "body": {"input": "hello", "store": False, "stream": False},
            "api-version": "2025-11-15-preview",
        })

    def test_unknown_required_tool_argument_rejected(self):
        with self.assertRaises(ValueError):
            agent_arguments({"required": ["secret"], "properties": {"secret": {"type": "string"}}}, "hello")

    def test_native_gateway_stripped_query_default_uses_pinned_agent_api(self):
        schema = {"required": ["api-version", "input"], "properties": {
            "api-version": {"type": "string"}, "input": {"type": "string"},
        }}
        self.assertEqual(agent_arguments(schema, "hello"), {"api-version": "v1", "input": "hello"})

    def test_sse_rpc_uses_matching_response(self):
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "text/event-stream"
        response._content = b'data: {"jsonrpc":"2.0","method":"notification"}\n\ndata: {"jsonrpc":"2.0","id":7,"result":{"tools":[]}}\n\n'
        self.assertEqual(parse_rpc_response(response, 7), {"tools": []})

    def test_rpc_error_is_not_success(self):
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"jsonrpc":"2.0","id":1,"error":{"code":-32603,"message":"backend forbidden"}}'
        with self.assertRaisesRegex(RuntimeError, "backend forbidden"):
            parse_rpc_response(response, 1)

    def test_echoed_marker_is_not_proof_of_agent_success(self):
        with self.assertRaises(RuntimeError):
            completed_agent_output({"content": [{"type": "text", "text": "Request failed: EXPECTED_MARKER"}]})

    def test_completed_agent_response_is_verified_structurally(self):
        response = {"object": "response", "id": "resp_test", "status": "completed", "error": None,
                    "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "READY"}]}]}
        result = completed_agent_output({"content": [{"type": "text", "text": json.dumps(response)}]})
        self.assertEqual(result, {"response_id": "resp_test", "output_text": "READY"})


class GatewayRoutingTests(unittest.TestCase):
    def make_fixture(self, rejection=None):
        control = Mock(spec=ControlPlane)
        saved = {}

        def put(resource_id, body, **kwargs):
            if resource_id.endswith("/aliases/approved-chat") and len(body["properties"]["backendModels"]) > 1 and rejection:
                raise rejection
            saved[resource_id] = body
            return body

        def request(method, resource_id, **kwargs):
            return saved[resource_id]

        control.put.side_effect = put
        control.request.side_effect = request
        control.protect.side_effect = lambda value: value
        gateway_id = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.ApiManagement/aigateways/gw"
        deployment = {
            "gateway": {"id": gateway_id, "principal_id": "gateway-principal"}, "model_name": "gpt-4o-mini", "model_version": "2024-07-18",
            "runner": {"principal_id": "runner-principal"}, "deployer_principal_id": "operator-principal",
            "application_insights_id": "/insights",
            "otlp": {"rule_id": "/dcr", "metrics_endpoint": "https://metrics.example/v1/metrics",
                     "logs_endpoint": "https://logs.example/v1/logs", "traces_endpoint": "https://traces.example/v1/traces"},
            "models": {region: {
                "id": f"/accounts/{region}", "model_endpoint": f"https://{region}.example/",
                "deployment_id": f"/accounts/{region}/deployments/model",
            } for region in ("primary", "secondary")},
        }
        return control, deployment

    def test_native_pool_preserves_both_equal_weight_targets(self):
        control, deployment = self.make_fixture()
        receipt = configure_models(control, deployment)
        self.assertEqual(receipt["routing_mode"], "load_balance")
        self.assertEqual([item["weight"] for item in receipt["backends"]], [1, 1])
        self.assertFalse(receipt["payload_capture"])
        operator_grant = [call for call in control.grant.call_args_list if call.args[0] == "operator-principal"]
        self.assertEqual(operator_grant[0].kwargs["principal_type"], "User")

    def test_federated_ci_operator_uses_service_principal_role_assignment(self):
        control, deployment = self.make_fixture()
        deployment["deployer_principal_type"] = "ServicePrincipal"
        configure_models(control, deployment)
        operator_grant = [call for call in control.grant.call_args_list if call.args[0] == "operator-principal"]
        self.assertEqual(operator_grant[0].kwargs["principal_type"], "ServicePrincipal")

    def test_supported_single_backend_fallback_is_explicit(self):
        control, deployment = self.make_fixture(AzureRequestError(400, "NotSupported", "Only one backend is supported"))
        receipt = configure_models(control, deployment)
        self.assertEqual(receipt["routing_mode"], "single_backend_fallback")
        self.assertEqual(len(receipt["backends"]), 1)
        self.assertIn("primary", receipt["backends"][0]["resourceId"])
        self.assertIsNotNone(receipt["fallback_reason"])

    def test_permission_failure_does_not_silently_fall_back(self):
        control, deployment = self.make_fixture(AzureRequestError(403, "Forbidden", "Caller has no access"))
        with self.assertRaises(AzureRequestError):
            configure_models(control, deployment)


class ShowbackTests(unittest.TestCase):
    def test_inactive_identity_query_has_an_explicit_historical_lookback(self):
        control = Mock(spec=ControlPlane)
        control.credential = Mock()
        control.credential.get_token.return_value.token = "unit-token"
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"status":"success","data":{"result":[]}}'
        deployment = {"otlp": {"query_endpoint": "https://metrics.example", "metrics_workspace_id": "/workspace"}}
        with patch("accelerator.usage.requests.get", return_value=response) as get:
            result = exported_metrics(control, deployment)
        query = get.call_args.kwargs["params"]["query"]
        self.assertEqual(query, 'last_over_time({__name__="azure.ai_gateway.client.token.usage"}[4h])')
        self.assertEqual(result["lookback_hours"], 4)

    def test_absent_project_measurements_are_unavailable_not_zero(self):
        project = {**PROJECT, "principal_id": "project-mi", "chargeback_project_key": "stable"}
        result = project_showback({"rows": []}, {"tenant_id": "tenant"}, [project])
        self.assertIsNone(result["projects"][0]["total_tokens"])
        self.assertEqual(result["projects"][0]["data_status"], "unavailable")

    def test_native_counts_use_identity_not_project_header_or_nested_token_details(self):
        project = {**PROJECT, "principal_id": "project-mi", "chargeback_project_key": "stable"}
        labels = {
            "azure.ai_gateway.identity.claims.oid": "project-mi",
            "azure.ai_gateway.identity.claims.tid": "tenant",
            "azure.ai_gateway.api_key_id": "unknown",
            "gen_ai.request.model": "approved-chat",
            "X-Chargeback-Project-Key": "forged-other-project",
        }
        rows = [{"value": value, "dimensions": {**labels, "gen_ai.token.type": kind}} for kind, value in (
            ("prompt_tokens", 12), ("completion_tokens", 3), ("total_tokens", 15),
            ("completion_tokens_details.reasoning_tokens", 2),
        )]
        result = project_showback({"rows": rows}, {"tenant_id": "tenant"}, [project])
        self.assertEqual(result["projects"][0]["total_tokens"], 15)
        self.assertEqual(result["projects"][0]["chargeback_project_key"], "stable")

    def test_key_authenticated_claims_are_not_assigned(self):
        project = {**PROJECT, "principal_id": "project-mi", "chargeback_project_key": "stable"}
        row = {"value": 5, "dimensions": {
            "azure.ai_gateway.identity.claims.oid": "project-mi", "azure.ai_gateway.identity.claims.tid": "tenant",
            "azure.ai_gateway.api_key_id": "some-key", "gen_ai.request.model": "approved-chat", "gen_ai.token.type": "total_tokens",
        }}
        result = project_showback({"rows": [row]}, {"tenant_id": "tenant"}, [project])
        self.assertIsNone(result["projects"][0]["total_tokens"])
        self.assertEqual(result["projects"][0]["data_status"], "unavailable")
        self.assertEqual(len(result["unassigned_core_series"]), 1)


if __name__ == "__main__":
    unittest.main()
