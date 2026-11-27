"""Offline behavioral coverage for durable native-key gateway profiles."""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
from copy import deepcopy

import pytest
import yaml
from fastapi import HTTPException
from starlette.requests import Request


ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def request_setup(monkeypatch):
    async def enrich(data, request, *args, **kwargs):
        return data

    setup = SimpleNamespace(add_litellm_data_to_request=enrich)
    monkeypatch.setitem(sys.modules, "litellm.proxy.litellm_pre_call_utils", setup)
    monkeypatch.setitem(sys.modules, "litellm.proxy.common_request_processing",
                        SimpleNamespace(add_litellm_data_to_request=enrich))
    return setup


def capture_request(data, body):
    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode()}

    request = Request({"type": "http", "path": "/v1/chat/completions", "headers": []}, receive)
    setup = sys.modules["litellm.proxy.litellm_pre_call_utils"]
    return asyncio.run(setup.add_litellm_data_to_request(data, request))


@pytest.fixture
def environment(monkeypatch):
    for name in (
        "TENANT_ID", "LITELLM_CLIENT_ID", "FASTMCP_CLIENT_ID", "APIM_TOKEN_AUDIENCE",
        "APIM_PRINCIPAL_ID", "AZURE_API_BASE", "FOUNDRY_A2A_URL", "FOUNDRY_A2A_CARD_URL",
        "MCP_TOKEN_SCOPE", "MCP_TOKEN_AUDIENCE", "LITELLM_PRINCIPAL_ID",
        "BLOB_ACCOUNT_URL", "BLOB_CONTAINER", "LITELLM_IMAGE", "FASTMCP_IMAGE",
        "BLOB_SEED_CLIENT_ID", "OBO_CLIENT_ID", "OBO_INPUT_AUDIENCE",
        "OBO_ALLOWED_CLIENT_IDS", "OBO_ALLOWED_PRINCIPAL_IDS",
    ):
        monkeypatch.setenv(name, "configured")
    for name in ("AUTHENTICATION_SCENARIO", "GATEWAY_NAMESPACE", "GATEWAY_INSTANCE",
                 "GATEWAY_SERVICE_ACCOUNT", "OBO_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EXPOSURE", "private")
    monkeypatch.setenv("FOUNDRY_A2A_URL", "https://foundry.example/agents/echo/endpoint/protocols/a2a")


@pytest.mark.parametrize("missing", ["DATABASE_URL", "LITELLM_MASTER_KEY"])
def test_missing_durable_secret_never_executes(monkeypatch, missing):
    launch = load("launch")
    monkeypatch.setenv("DATABASE_URL", "postgresql://db/litellm")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-persistent")
    monkeypatch.delenv(missing)
    calls = []
    monkeypatch.setattr(launch.os, "execvp", lambda *args: calls.append(args))
    with pytest.raises(RuntimeError, match=missing):
        launch.main()
    assert not calls


def test_obo_secret_file_required_then_loaded(monkeypatch):
    launch = load("launch")
    monkeypatch.setenv("DATABASE_URL", "postgresql://db/litellm")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-persistent")
    monkeypatch.setenv("AUTHENTICATION_SCENARIO", "user-obo")
    monkeypatch.delenv("OBO_CLIENT_SECRET", raising=False)
    # Keep test files within the scenario, rather than external temporary paths.
    secret_file = ROOT / f".test-obo-secret-{uuid4().hex}"
    monkeypatch.setenv("OBO_CLIENT_SECRET_FILE", str(secret_file))
    calls = []
    monkeypatch.setattr(launch.os, "execvp", lambda *args: calls.append(args))
    try:
        with pytest.raises(RuntimeError, match="unavailable"):
            launch.main()
        assert not calls
        secret_file.write_text("test-only-secret\n", encoding="utf-8")
        launch.main()
        assert launch.os.environ["OBO_CLIENT_SECRET"] == "test-only-secret"
        assert calls
        assert "test-only-secret" not in repr(calls)
    finally:
        secret_file.unlink(missing_ok=True)


@pytest.mark.parametrize("scenario,namespace,instance", [
    ("shared-service", "gateway", "litellm"),
    ("dedicated-application", "gateway-app-alpha", "litellm-app-alpha"),
    ("dedicated-application", "gateway-app-alpha", "litellm"),
    ("user-obo", "gateway-user-obo", "litellm"),
])
def test_profile_rendering(environment, monkeypatch, scenario, namespace, instance):
    monkeypatch.setenv("AUTHENTICATION_SCENARIO", scenario)
    monkeypatch.setenv("GATEWAY_NAMESPACE", namespace)
    if instance != "litellm":
        monkeypatch.setenv("GATEWAY_INSTANCE", instance)
    items = load("render").render()["items"]
    resources = {(i["kind"], i["metadata"]["name"]): i for i in items}
    for i in items:
        if i["kind"] != "Namespace":
            assert i["metadata"]["namespace"] == namespace
    pod = resources["Deployment", instance]["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "litellm"
    assert resources["Service", instance]["spec"]["selector"] == {"app": instance}
    assert pod["volumes"][0]["configMap"]["name"] == f"{instance}-config"
    gateway, *adapters = pod["containers"]
    env = {i["name"]: i for i in gateway["env"]}
    assert env["DATABASE_URL"]["valueFrom"]["secretKeyRef"] == {
        "name": "litellm-auth", "key": "database-url",
    }
    assert env["LITELLM_MASTER_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "litellm-auth", "key": "master-key",
    }
    assert not adapters
    assert env["FASTMCP_URL"]["value"] == f"http://fastmcp.{namespace}.svc.cluster.local:8000/mcp"
    if scenario != "user-obo":
        assert "mcp_workload:install" in env["LITELLM_WORKER_STARTUP_HOOKS"]["value"]
    config = yaml.safe_load(resources["ConfigMap", f"{instance}-config"]["data"]["config.yaml"])
    assert not config["mcp_servers"]["blob"]["allow_all_keys"]
    assert config["mcp_servers"]["blob"]["url"] == f"http://fastmcp.{namespace}.svc.cluster.local:8000/mcp"
    agent = config["agents"][0]
    assert agent["agent_name"] == "hosted"
    assert agent["agent_card_params"]["url"] == "https://foundry.example/agents/echo/endpoint/protocols/a2a"
    assert agent["litellm_params"]["agent_card_path"] == "agentCard/v1.0"
    assert agent["static_headers"] == {"a2a-version": "1.0"}
    if scenario == "user-obo":
        assert agent["extra_headers"] == ["Authorization"]
        assert agent["litellm_params"] == {"agent_card_path": "agentCard/v1.0", "foundry_user_obo": True}
        mcp = config["mcp_servers"]["blob"]
        assert not adapters
        assert mcp["url"] == f"http://fastmcp.{namespace}.svc.cluster.local:8000/mcp"
        assert mcp["auth_type"] == "oauth2_token_exchange"
        # YAML fields are top-level, unlike the REST credentials payload.
        assert mcp["client_secret"] == "os.environ/OBO_CLIENT_SECRET"
        assert mcp["token_exchange_profile"] == "entra_obo"
        assert any(m["name"] == "obo-secret" for m in gateway["volumeMounts"])
    else:
        assert agent["litellm_params"]["azure_ad_token"] == "oidc/azure/api://AzureADTokenExchange"
        assert "client_secret" not in agent["litellm_params"]
        assert not any(v["name"] == "obo-secret" for v in pod["volumes"])


@pytest.mark.parametrize("name,value", [
    ("AUTHENTICATION_SCENARIO", "typo"),
    ("GATEWAY_INSTANCE", "bad/name"),
    ("GATEWAY_NAMESPACE", "../gateway"),
    ("FOUNDRY_A2A_URL", "http://foundry.example/a2a"),
    ("FOUNDRY_A2A_URL", "https://user:password@foundry.example/a2a"),
])
def test_invalid_profile_inputs_fail_closed(environment, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        load("render").render()


@pytest.mark.parametrize("scenario", ["shared-service", "user-obo"])
def test_agent_responses_aliases_are_opt_in_and_fixed_to_foundry(environment, monkeypatch, scenario):
    monkeypatch.setenv("AUTHENTICATION_SCENARIO", scenario)
    if scenario == "user-obo":
        monkeypatch.setenv("GATEWAY_NAMESPACE", "gateway-user-obo")
    monkeypatch.setenv("FOUNDRY_DECLARATIVE_A2A_URL", "https://demo.services.ai.azure.com/declarative/a2a")
    monkeypatch.setenv("ENABLE_AGENT_RESPONSES", "true")
    monkeypatch.setenv("FOUNDRY_PROMPT_RESPONSES_BASE", "https://demo.services.ai.azure.com/api/projects/demo")
    monkeypatch.setenv("FOUNDRY_HOSTED_RESPONSES_BASE", "https://demo.services.ai.azure.com/api/projects/demo/agents/hosted/endpoint/protocols/openai")
    resources = load("render").render()["items"]
    config = yaml.safe_load(next(r for r in resources if r["kind"] == "ConfigMap")["data"]["config.yaml"])
    models = {entry["model_name"]: entry["litellm_params"] for entry in config["model_list"]}
    assert models["prompt-agent-responses"]["model"] == "openai/gpt-4o-mini"
    assert models["hosted-agent-responses"]["model"] == "openai/gateway-hosted-dual"
    assert [agent["agent_name"] for agent in config["agents"]] == ["hosted", "declarative"]
    hosted = config["agents"][0]
    assert hosted["agent_card_params"]["url"] == (
        "https://demo.services.ai.azure.com/api/projects/demo/agents/hosted/endpoint/protocols/a2a"
    )
    if scenario == "user-obo":
        assert hosted["extra_headers"] == ["Authorization"]
        assert hosted["litellm_params"] == {"agent_card_path": "agentCard/v1.0", "foundry_user_obo": True}
    else:
        assert hosted["litellm_params"]["azure_ad_token"] == "oidc/azure/api://AzureADTokenExchange"
    monkeypatch.setenv("FOUNDRY_HOSTED_RESPONSES_BASE", "http://caller.example/")
    with pytest.raises(ValueError, match="Foundry HTTPS"):
        load("render").render()


def test_private_mcp_preserves_agent_source_for_network_policy_and_health_checks(environment, monkeypatch):
    monkeypatch.setenv("FOUNDRY_AGENT_SUBNET_CIDR", "10.240.18.0/24")
    resources = load("render").render()["items"]
    service = next(r for r in resources if r["kind"] == "Service"
                   and r["metadata"]["name"] == "fastmcp-foundry-internal")
    assert service["metadata"]["annotations"]["service.beta.kubernetes.io/azure-load-balancer-internal"] == "true"
    assert service["spec"]["externalTrafficPolicy"] == "Local"
    assert service["spec"]["loadBalancerSourceRanges"] == ["10.240.18.0/24"]
    policy = next(r for r in resources if r["kind"] == "NetworkPolicy")
    assert policy["spec"]["ingress"][-1]["from"] == [{"ipBlock": {"cidr": "10.240.18.0/24"}}]


def test_private_mcp_cannot_accept_public_source_range(environment, monkeypatch):
    monkeypatch.setenv("FOUNDRY_AGENT_SUBNET_CIDR", "0.0.0.0/0")
    with pytest.raises(ValueError, match="private delegated IPv4 subnet"):
        load("render").render()


@pytest.mark.parametrize("instance", ["litellm", "litellm-app-alpha"])
def test_dedicated_profile_cannot_reuse_shared_database_namespace(environment, monkeypatch, instance):
    monkeypatch.setenv("AUTHENTICATION_SCENARIO", "dedicated-application")
    monkeypatch.setenv("GATEWAY_INSTANCE", instance)
    with pytest.raises(ValueError, match="isolated"):
        load("render").render()


@pytest.mark.parametrize("override", [
    {"api_base": "https://attacker.invalid"}, {"api_key": "external-key"},
    {"litellm_params": {"api_base": "https://attacker.invalid"}},
    {"extra_headers": {"Authorization": "external-token"}},
    {"extra_body": {"api_base": "https://attacker.invalid"}},
    {"tools": [{"type": "mcp", "server_url": "https://attacker.invalid"}]},
    {"metadata": {"nested": {"api_base": "https://attacker.invalid"}}},
    {"model": "azure/unregistered"},
    {"extra_query": {"api-version": "caller-selected"}},
])
def test_registered_backend_policy_rejects_overrides(monkeypatch, override):
    monkeypatch.setitem(sys.modules, "litellm.integrations.custom_logger",
                        SimpleNamespace(CustomLogger=object))
    policy = load("request_policy").policy
    data = {"proxy_server_request": {"body": {"model": "enterprise-chat", **override}}}
    capture_request(data, {"model": "enterprise-chat", **override})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(policy.async_pre_call_hook(None, None, data, "completion"))
    assert exc.value.status_code == 403


@pytest.mark.parametrize("field", ["agent_reference", "agent", "agent_id", "agent_name"])
@pytest.mark.parametrize("nested", [False, True])
def test_agent_response_alias_cannot_select_another_agent(monkeypatch, field, nested):
    monkeypatch.setitem(sys.modules, "litellm.integrations.custom_logger",
                        SimpleNamespace(CustomLogger=object))
    policy = load("request_policy").policy
    data = {}
    override = {"extra_body": {field: "another-agent"}} if nested else {field: "another-agent"}
    capture_request(data, {"model": "hosted-agent-responses", "input": "hello", **override})
    with pytest.raises(HTTPException) as error:
        asyncio.run(policy.async_pre_call_hook(None, None, data, "aresponses"))
    assert error.value.status_code == 403


def test_responses_obo_receives_original_header_only_from_nonlogged_framework_capture(monkeypatch):
    monkeypatch.setitem(sys.modules, "litellm.integrations.custom_logger",
                        SimpleNamespace(CustomLogger=object))
    authenticate = AsyncMock(side_effect=lambda data, body, **kwargs: data)
    monkeypatch.setitem(sys.modules, "agent_responses", SimpleNamespace(authenticate=authenticate))
    policy = load("request_policy").policy
    body = {"model": "hosted-agent-responses", "input": "hello"}

    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode()}

    request = Request({
        "type": "http", "path": "/v1/responses",
        "headers": [(b"authorization", b"Bearer captured-user")],
    }, receive)
    data = {"proxy_server_request": {"headers": {}}}
    setup = sys.modules["litellm.proxy.litellm_pre_call_utils"]
    asyncio.run(setup.add_litellm_data_to_request(data, request))
    asyncio.run(policy.async_pre_call_hook(None, None, data, "aresponses"))
    assert authenticate.await_args.kwargs["authorization"] == "Bearer captured-user"
    assert "captured-user" not in json.dumps(data["proxy_server_request"])


def test_registered_backend_policy_keeps_authorization_and_router_parameters(monkeypatch):
    monkeypatch.setitem(sys.modules, "litellm.integrations.custom_logger",
                        SimpleNamespace(CustomLogger=object))
    monkeypatch.setenv("MODEL_ALIAS", "enterprise-chat")
    policy = load("request_policy").policy
    data = {
        "api_base": "https://administrator-configured.invalid",
        "proxy_server_request": {
            "headers": {"authorization": "Bearer user-jwt", "x-litellm-api-key": "sk-native"},
            "body": {
                "model": "enterprise-chat",
                "metadata": {"headers": {"authorization": "Bearer user-jwt"},
                             "user_api_key_metadata": {"api_base": "https://administrator-configured.invalid"},
                             "model_info": {"id": "native-deployment-id"}},
            },
        },
    }
    capture_request(data, {"model": "enterprise-chat", "messages": [{"role": "user", "content": "hello"}]})
    assert asyncio.run(policy.async_pre_call_hook(None, None, data, "completion")) is data
    assert data["proxy_server_request"]["headers"]["authorization"] == "Bearer user-jwt"


@pytest.mark.parametrize("target", [
    "litellm_proxy", "litellm_proxy/mcp/blob", "litellm_proxy/mcp/blob-id", "litellm_proxy/blob-id",
])
def test_model_playground_can_use_registered_mcp_tools(monkeypatch, target):
    monkeypatch.setitem(sys.modules, "litellm.integrations.custom_logger", SimpleNamespace(CustomLogger=object))
    monkeypatch.setitem(sys.modules, "litellm.proxy.mcp_server.mcp_server_manager", SimpleNamespace(
        global_mcp_server_manager=SimpleNamespace(
            get_mcp_server_by_name=lambda name: SimpleNamespace(server_id="blob-id") if name == "blob" else None,
        ),
    ))
    module = load("request_policy")
    body = {"model": "enterprise-chat", "messages": [{"role": "user", "content": "Read the approved Blob"}],
            "tools": [{"type": "mcp", "server_url": target, "server_label": "blob",
                       "require_approval": "never", "allowed_tools": ["blob-read_blob"], "headers": {}}]}
    data = {}
    capture_request(data, body)
    assert asyncio.run(module.policy.async_pre_call_hook(None, None, data, "acompletion")) is data


@pytest.mark.parametrize("tool", [
    {"type": "mcp", "server_url": "https://external.invalid/mcp"},
    {"type": "mcp", "server_url": "http://127.0.0.1:8000/mcp"},
    {"type": "mcp", "server_url": "litellm_proxy/mcp/unregistered"},
    {"type": "mcp", "server_url": "litellm_proxy/mcp/blob-id?api_key=external"},
    {"type": "mcp", "server_url": "litellm_proxy", "headers": {"Authorization": "external"}},
    {"type": "mcp", "server_url": "litellm_proxy", "api_base": "https://external.invalid"},
    {"type": "mcp", "server_url": "litellm_proxy", "allowed_tools": [{"api_key": "external"}]},
])
def test_registered_mcp_support_does_not_allow_backend_or_credential_overrides(monkeypatch, tool):
    monkeypatch.setitem(sys.modules, "litellm.integrations.custom_logger", SimpleNamespace(CustomLogger=object))
    monkeypatch.setitem(sys.modules, "litellm.proxy.mcp_server.mcp_server_manager", SimpleNamespace(
        global_mcp_server_manager=SimpleNamespace(
            get_mcp_server_by_name=lambda name: SimpleNamespace(server_id="blob-id") if name == "blob" else None,
        ),
    ))
    module = load("request_policy")
    data = {}
    capture_request(data, {"model": "enterprise-chat", "tools": [tool]})
    with pytest.raises(HTTPException) as error:
        asyncio.run(module.policy.async_pre_call_hook(None, None, data, "acompletion"))
    assert error.value.status_code == 403


def test_native_mcp_yaml_credentials_resolve_without_mutating_published_config(monkeypatch):
    monkeypatch.setitem(sys.modules, "litellm.integrations.custom_logger",
                        SimpleNamespace(CustomLogger=object))
    native_loader = AsyncMock()
    manager = SimpleNamespace(load_servers_from_config=native_loader)
    monkeypatch.setitem(sys.modules, "litellm.proxy.mcp_server.mcp_server_manager",
                        SimpleNamespace(global_mcp_server_manager=manager))
    monkeypatch.setenv("OBO_CLIENT_ID", "client-id")
    monkeypatch.setenv("OBO_CLIENT_SECRET", "test-secret")
    policy = load("request_policy")
    policy.install_mcp_env_resolution()
    wrapped = manager.load_servers_from_config
    policy.install_mcp_env_resolution()
    assert manager.load_servers_from_config is wrapped
    config = {"blob": {"client_id": "os.environ/OBO_CLIENT_ID",
                       "client_secret": "os.environ/OBO_CLIENT_SECRET"}}
    asyncio.run(manager.load_servers_from_config(config))
    native_loader.assert_awaited_once_with(
        {"blob": {"client_id": "client-id", "client_secret": "test-secret"}}, None,
    )
    assert config["blob"]["client_secret"] == "os.environ/OBO_CLIENT_SECRET"
    monkeypatch.delenv("OBO_CLIENT_SECRET")
    with pytest.raises(RuntimeError, match="required"):
        asyncio.run(manager.load_servers_from_config(config))
    assert native_loader.await_count == 1


def test_policy_rejects_credentials_redacted_by_native_request_snapshot(monkeypatch):
    monkeypatch.setitem(sys.modules, "litellm.integrations.custom_logger",
                        SimpleNamespace(CustomLogger=object))
    policy = load("request_policy").policy
    data = {"proxy_server_request": {
        "body": {"model": "enterprise-chat"}, "credential_fields": ("api_key",),
    }}
    capture_request(data, {"model": "enterprise-chat", "api_key": "caller-supplied-secret"})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(policy.async_pre_call_hook(None, None, data, "acompletion"))
    assert exc.value.status_code == 403


@pytest.mark.parametrize("call_type", ["asend_message", "list_mcp_tools", "call_mcp_tool"])
def test_policy_does_not_reject_registered_protocol_synthetic_hook_data(monkeypatch, call_type):
    monkeypatch.setitem(sys.modules, "litellm.integrations.custom_logger",
                        SimpleNamespace(CustomLogger=object))
    policy = load("request_policy").policy
    data = {"proxy_server_request": {"body": {
        "model": "a2a_agent/Echo", "custom_llm_provider": "a2a_agent",
        "metadata": {"model_info": {"id": "registered-agent-id"}},
    }}}
    assert asyncio.run(policy.async_pre_call_hook(None, None, data, call_type)) is data


def test_policy_requires_framework_capture_instead_of_trusting_spoofed_snapshot(monkeypatch):
    monkeypatch.setitem(sys.modules, "litellm.integrations.custom_logger",
                        SimpleNamespace(CustomLogger=object))
    policy = load("request_policy").policy
    data = {"proxy_server_request": {"body": {"model": "enterprise-chat"}}}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(policy.async_pre_call_hook(None, None, data, "acompletion"))
    assert exc.value.status_code == 400


def test_capture_reads_original_bytes_not_mutated_cached_request_json(monkeypatch, request_setup):
    monkeypatch.setitem(sys.modules, "litellm.integrations.custom_logger",
                        SimpleNamespace(CustomLogger=object))
    policy_module = load("request_policy")
    wrapped = request_setup.add_litellm_data_to_request
    policy_module.install_request_capture()
    assert request_setup.add_litellm_data_to_request is wrapped
    assert sys.modules["litellm.proxy.common_request_processing"].add_litellm_data_to_request is wrapped
    async def receive():
        return {"type": "http.request", "body": b'{"model":"enterprise-chat"}'}

    request = Request({"type": "http", "path": "/v1/chat/completions", "headers": []}, receive)
    request._json = {"model": "enterprise-chat", "metadata": {"headers": {}, "api_base": "native-injected"}}
    data = {"proxy_server_request": {"body": request._json}}
    asyncio.run(wrapped(data, request))
    assert asyncio.run(policy_module.policy.async_pre_call_hook(None, None, data, "acompletion")) is data
    copied = deepcopy(data)
    assert asyncio.run(policy_module.policy.async_pre_call_hook(None, None, copied, "acompletion")) is copied
    assert "_scenario_request" not in data["proxy_server_request"]


def test_capture_passes_synthetic_mcp_request_without_receive_unchanged(monkeypatch, request_setup):
    monkeypatch.setitem(sys.modules, "litellm.integrations.custom_logger",
                        SimpleNamespace(CustomLogger=object))
    data = {"model": "synthetic-mcp-model"}
    request = Request({"type": "http", "path": "/blob/mcp"})
    original = AsyncMock(return_value=data)
    original._scenario_raw_capture = False
    request_setup.add_litellm_data_to_request = original
    load("request_policy")
    assert request_setup.add_litellm_data_to_request is not original
    result = asyncio.run(request_setup.add_litellm_data_to_request(
        data, request, "native-auth", proxy_config="native-config",
    ))
    original.assert_awaited_once_with(data, request, "native-auth", proxy_config="native-config")
    assert result is data
    assert data == {"model": "synthetic-mcp-model"}


def test_startup_hook_then_dynamic_callback_import_shares_capture_owner(monkeypatch, request_setup):
    monkeypatch.setitem(sys.modules, "litellm.integrations.custom_logger",
                        SimpleNamespace(CustomLogger=object))
    monkeypatch.setitem(sys.modules, "litellm.proxy.mcp_server.mcp_server_manager",
                        SimpleNamespace(global_mcp_server_manager=SimpleNamespace(
                            load_servers_from_config=AsyncMock())))
    monkeypatch.setenv("MODEL_ALIAS", "enterprise-chat")
    startup_module = load("request_policy")
    startup_module.install_mcp_env_resolution()
    wrapped = request_setup.add_litellm_data_to_request
    callback_module = load("request_policy")
    assert callback_module is not startup_module
    assert request_setup.add_litellm_data_to_request is wrapped
    data = capture_request({}, {"model": "enterprise-chat"})
    assert asyncio.run(callback_module.policy.async_pre_call_hook(
        None, None, data, "acompletion",
    )) is data
    malicious = capture_request({}, {
        "model": "enterprise-chat", "api_base": "https://attacker.invalid",
    })
    with pytest.raises(HTTPException) as exc:
        asyncio.run(callback_module.policy.async_pre_call_hook(None, None, malicious, "acompletion"))
    assert exc.value.status_code == 403
