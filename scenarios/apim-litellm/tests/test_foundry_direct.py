import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException


TARGET = "https://foundry.example/agents/echo/endpoint/protocols/a2a"


@pytest.fixture
def runtime(monkeypatch):
    module_path = Path(__file__).resolve().parents[1] / "foundry_a2a.py"
    spec = importlib.util.spec_from_file_location("foundry_direct_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    endpoints = SimpleNamespace(_forward_jsonrpc=AsyncMock(), _resolve_backend_auth=AsyncMock())
    endpoints._forward_jsonrpc._foundry_direct = False
    monkeypatch.setitem(sys.modules, "litellm.proxy.agent_endpoints", SimpleNamespace(a2a_endpoints=endpoints))
    monkeypatch.setenv("FOUNDRY_A2A_URL", TARGET)
    monkeypatch.delenv("AUTHENTICATION_SCENARIO", raising=False)
    return module, endpoints


def test_native_task_fix_is_scoped_and_preserves_input_and_other_agents(runtime):
    module, endpoints = runtime
    original = endpoints._forward_jsonrpc
    resolve = endpoints._resolve_backend_auth
    module.install()
    installed = endpoints._forward_jsonrpc
    module.install()
    assert endpoints._forward_jsonrpc is installed
    body = {"jsonrpc": "2.0", "id": 5, "method": "tasks/get", "params": {"id": "task-1"}}
    headers = {"A2A-Version": "0.3", "Authorization": "Bearer backend"}
    asyncio.run(installed(TARGET, body, extra_headers=headers))
    original.assert_awaited_once_with(
        TARGET, {**body, "method": "GetTask"},
        extra_headers={"Authorization": "Bearer backend", "a2a-version": "1.0"},
    )
    assert body["method"] == "tasks/get"
    assert headers["A2A-Version"] == "0.3"
    assert endpoints._resolve_backend_auth is resolve
    original.reset_mock()
    asyncio.run(installed("https://other.example/a2a", body, extra_headers=headers))
    original.assert_awaited_once_with("https://other.example/a2a", body, extra_headers=headers)


def test_direct_user_obo_supplies_exchanged_token_without_registry_mutation_or_fallback(runtime, monkeypatch):
    module, endpoints = runtime
    monkeypatch.setenv("AUTHENTICATION_SCENARIO", "user-obo")
    exchange = SimpleNamespace(get_token=AsyncMock(return_value="exchanged-foundry-user"))
    monkeypatch.setitem(sys.modules, "obo", SimpleNamespace(OnBehalfOf=lambda client: exchange))
    monkeypatch.setitem(
        sys.modules, "litellm.proxy.agent_endpoints.databricks_oauth",
        SimpleNamespace(AgentBackendAuth=SimpleNamespace),
    )
    original = endpoints._resolve_backend_auth
    module.install()
    params = {"agent_card_path": "agentCard/v1.0", "foundry_user_obo": True}
    result = asyncio.run(endpoints._resolve_backend_auth(
        params, None, {"authorization": "Bearer gateway-user"},
    ))
    args = exchange.get_token.await_args
    assert args.args == ("Bearer gateway-user", "https://ai.azure.com/.default")
    assert "client" in args.kwargs
    assert result.header == {"Authorization": "Bearer exchanged-foundry-user"}
    assert result.litellm_params["azure_ad_token"] == "exchanged-foundry-user"
    assert "azure_ad_token" not in params
    original.assert_not_awaited()
    exchange.get_token.side_effect = HTTPException(401, "A delegated user bearer is required")
    with pytest.raises(HTTPException) as error:
        asyncio.run(endpoints._resolve_backend_auth(params, None))
    assert error.value.status_code == 401
    original.assert_not_awaited()
