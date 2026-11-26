import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from azure.core.exceptions import ClientAuthenticationError


@pytest.fixture
def module(monkeypatch):
    monkeypatch.setitem(sys.modules, "httpx2", httpx)
    monkeypatch.setitem(sys.modules, "identity", SimpleNamespace(required=lambda n: {
        "FASTMCP_URL": "http://fastmcp.gateway.svc.cluster.local:8000/mcp",
        "GATEWAY_NAMESPACE": "gateway", "MCP_TOKEN_SCOPE": "api://mcp/.default",
    }[n]))
    spec = importlib.util.spec_from_file_location("mcp_workload_test", Path(__file__).parents[1] / "mcp_workload.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "version", lambda _: "1.110.2")
    monkeypatch.setattr(mod.atexit, "register", lambda _: None)
    return mod


def test_workload_auth_renews_per_request_and_rejects_other_destinations(module, monkeypatch):
    identity = Mock()
    identity.get_token.side_effect = [SimpleNamespace(token="first"), SimpleNamespace(token="renewed")]
    monkeypatch.setattr(module, "WorkloadIdentityCredential", lambda: identity)
    auth = module.WorkloadAuth("http://fastmcp.gateway.svc.cluster.local:8000/mcp", "api://mcp/.default")

    async def run():
        for expected in ("first", "renewed"):
            request = httpx.Request("POST", auth.target)
            async for signed in auth.async_auth_flow(request):
                assert signed.headers["Authorization"] == "Bearer " + expected
        with pytest.raises(ValueError, match="destination mismatch"):
            async for _ in auth.async_auth_flow(httpx.Request("POST", "https://other.example/mcp")):
                pass
        identity.get_token.side_effect = ClientAuthenticationError("rejected")
        with pytest.raises(ClientAuthenticationError):
            async for _ in auth.async_auth_flow(httpx.Request("POST", auth.target)):
                pass

    asyncio.run(run())
    assert identity.get_token.call_count == 3


def test_install_scopes_auth_to_fixed_mcp_client_and_leaves_c_native(module, monkeypatch):
    calls = []

    class Client:
        def __init__(self, server_url="", auth_value=None, resolved_auth=None, auth_type=None, auth_header_name=None):
            calls.append((server_url, auth_value, resolved_auth))

    monkeypatch.setitem(sys.modules, "litellm.mcp_client.client", SimpleNamespace(MCPClient=Client))
    auth = object()
    monkeypatch.setattr(module, "WorkloadAuth", lambda *args: auth)
    monkeypatch.setenv("AUTHENTICATION_SCENARIO", "shared-service")
    module.install()
    module.install()
    Client("http://fastmcp.gateway.svc.cluster.local:8000/mcp", auth_value="caller-override")
    Client(server_url="https://other.example/mcp", auth_value="unchanged")
    assert calls == [
        ("http://fastmcp.gateway.svc.cluster.local:8000/mcp", None, auth),
        ("https://other.example/mcp", "unchanged", None),
    ]
    monkeypatch.setenv("AUTHENTICATION_SCENARIO", "user-obo")
    monkeypatch.setattr(module, "version", lambda _: "unsupported")
    module.install()
    monkeypatch.setenv("AUTHENTICATION_SCENARIO", "shared-service")
    with pytest.raises(RuntimeError, match="validated LiteLLM"):
        module.install()
