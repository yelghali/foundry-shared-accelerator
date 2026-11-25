import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException


@pytest.fixture
def runtime(monkeypatch):
    identity = SimpleNamespace(get_token=AsyncMock(return_value=SimpleNamespace(token="fresh-workload")))
    identity.__aenter__ = AsyncMock(return_value=identity)
    identity.__aexit__ = AsyncMock()

    class Credential:
        async def __aenter__(self):
            return identity

        async def __aexit__(self, *args):
            return None

    monkeypatch.setitem(sys.modules, "identity", SimpleNamespace(
        credential=Credential, required=lambda name: __import__("os").environ[name],
    ))
    spec = importlib.util.spec_from_file_location(
        "agent_responses_test", Path(__file__).resolve().parents[1] / "agent_responses.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("FOUNDRY_HOSTED_RESPONSES_BASE", "https://demo.services.ai.azure.com/hosted/openai")
    monkeypatch.setenv("FOUNDRY_PROMPT_RESPONSES_BASE", "https://demo.services.ai.azure.com/api/projects/demo")
    monkeypatch.setenv("AUTHENTICATION_SCENARIO", "shared-service")
    return module, identity


def test_hosted_responses_gets_fresh_identity_token_and_fixed_endpoint(runtime):
    module, identity = runtime
    data = {}
    result = asyncio.run(module.authenticate(data, {"model": "hosted-agent-responses"}))
    assert result is data
    assert data["api_key"] == "fresh-workload"
    assert data["api_base"] == "https://demo.services.ai.azure.com/hosted/openai"
    assert "extra_query" not in data
    identity.get_token.assert_awaited_once_with("https://ai.azure.com/.default")


def test_prompt_responses_uses_endpoint_api_version_and_unknown_alias_denied(runtime):
    module, _ = runtime
    data = asyncio.run(module.authenticate({}, {"model": "prompt-agent-responses"}))
    assert "extra_query" not in data
    with pytest.raises(HTTPException) as error:
        asyncio.run(module.authenticate({}, {"model": "caller-defined-agent"}))
    assert error.value.status_code == 403


def test_agent_responses_obo_does_not_fall_back_to_workload(runtime, monkeypatch):
    module, identity = runtime
    monkeypatch.setenv("AUTHENTICATION_SCENARIO", "user-obo")
    exchange = SimpleNamespace(get_token=AsyncMock(side_effect=HTTPException(401, "User required")))
    monkeypatch.setitem(sys.modules, "obo", SimpleNamespace(OnBehalfOf=lambda client: exchange))
    with pytest.raises(HTTPException) as error:
        asyncio.run(module.authenticate({}, {"model": "hosted-agent-responses"}))
    assert error.value.status_code == 401
    identity.get_token.assert_not_awaited()


def test_agent_responses_uses_only_framework_captured_user_assertion(runtime, monkeypatch):
    module, identity = runtime
    monkeypatch.setenv("AUTHENTICATION_SCENARIO", "user-obo")
    exchange = SimpleNamespace(get_token=AsyncMock(return_value="delegated-foundry"))
    monkeypatch.setitem(sys.modules, "obo", SimpleNamespace(OnBehalfOf=lambda client: exchange))
    data = {"proxy_server_request": {"headers": {"authorization": "Bearer untrusted-copy"}}}
    result = asyncio.run(module.authenticate(
        data, {"model": "hosted-agent-responses"}, authorization="Bearer captured-user",
    ))
    assert exchange.get_token.await_args.args == ("Bearer captured-user", "https://ai.azure.com/.default")
    assert result["api_key"] == "delegated-foundry"
    identity.get_token.assert_not_awaited()


def test_preview_version_hook_only_changes_registered_foundry_targets(runtime, monkeypatch):
    module, _ = runtime

    class Config:
        def get_complete_url(self, api_base, litellm_params):
            return api_base.rstrip("/") + "/responses"

    monkeypatch.setitem(
        sys.modules, "litellm.llms.openai.responses.transformation",
        SimpleNamespace(OpenAIResponsesAPIConfig=Config),
    )
    module.install()
    installed = Config.get_complete_url
    module.install()
    assert installed is Config.get_complete_url
    assert Config().get_complete_url(
        "https://demo.services.ai.azure.com/hosted/openai", {},
    ) == "https://demo.services.ai.azure.com/hosted/openai/responses?api-version=2025-11-15-preview"
    assert Config().get_complete_url("https://other.example/v1", {}) == "https://other.example/v1/responses"
