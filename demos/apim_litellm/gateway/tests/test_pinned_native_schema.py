"""Run against the actual pinned gateway distribution, never a hand-written schema."""

from importlib.metadata import PackageNotFoundError, version

import pytest


def pinned_runtime():
    try:
        installed = version("litellm")
    except PackageNotFoundError:
        pytest.skip("The native gateway schema check requires LiteLLM v1.110.2")
    if installed != "1.110.2":
        pytest.skip(f"Native schema check requires v1.110.2, not {installed}")


def test_native_key_generation_schema_supports_explicit_route_and_object_grants():
    pinned_runtime()
    from litellm.proxy._types import GenerateKeyRequest

    application = GenerateKeyRequest.model_validate({
        "key_alias": "application",
        "key_type": "default",
        "models": ["enterprise-chat"],
        "allowed_routes": ["/chat/completions", "mcp_routes", "agent_routes"],
        "object_permission": {
            "mcp_servers": ["registered-mcp-server-id"],
            "agents": ["registered-hosted-agent-id", "registered-declarative-agent-id"],
        },
    })
    assert application.object_permission.mcp_servers == ["registered-mcp-server-id"]
    assert len(application.object_permission.agents) == 2
    models_only = GenerateKeyRequest.model_validate({
        "key_alias": "models-only",
        "key_type": "default",
        "models": ["enterprise-chat"],
        "allowed_routes": ["/chat/completions"],
        "object_permission": {"mcp_servers": [], "agents": []},
    })
    assert models_only.object_permission.mcp_servers == []
    assert models_only.object_permission.agents == []


def test_native_mcp_model_accepts_flat_entra_obo_configuration():
    pinned_runtime()
    from litellm.types.mcp_server.mcp_server_manager import MCPServer

    server = MCPServer.model_validate({
        "server_id": "blob-id", "name": "blob", "server_name": "blob",
        "url": "http://fastmcp.gateway-user-obo.svc.cluster.local:8000/mcp",
        "transport": "http", "auth_type": "oauth2_token_exchange",
        "token_exchange_profile": "entra_obo",
        "token_exchange_endpoint": "https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
        "client_id": "gateway-client-id", "client_secret": "test-not-a-real-secret",
        "scopes": ["api://mcp-client-id/.default"],
        "allow_all_keys": False,
    })
    assert server.token_exchange_profile == "entra_obo"
    assert server.client_secret == "test-not-a-real-secret"
    assert not server.allow_all_keys
