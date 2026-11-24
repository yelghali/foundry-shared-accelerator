"""Offline delegated identity tests: real signatures, mocked Entra exchange only."""

import asyncio
import gzip
import importlib
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    for key, value in {
        "TENANT_ID": "tenant", "OBO_TENANT_ID": "tenant", "OBO_CLIENT_ID": "gateway",
        "OBO_CLIENT_SECRET": "test-secret", "OBO_TOKEN_AUDIENCE": "gateway",
        "OBO_ALLOWED_CLIENT_IDS": "consumer", "OBO_ALLOWED_PRINCIPAL_IDS": "alice,bob",
        "MCP_TOKEN_AUDIENCE": "mcp", "MCP_ALLOWED_CLIENT_IDS": "gateway",
        "MCP_ALLOWED_PRINCIPAL_IDS": "alice,bob", "MCP_AUTH_MODE": "user-obo",
    }.items():
        monkeypatch.setenv(key, value)
    identity = importlib.import_module("identity")
    identity.delegated_verifier.cache_clear()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwks = {"keys": [dict(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True), kid="test")]}
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda self: jwks)

    def token(**overrides):
        now = int(time.time())
        claims = {
            "iss": "https://login.microsoftonline.com/tenant/v2.0", "tid": "tenant",
            "oid": "alice", "azp": "consumer", "aud": "gateway",
            "scp": "access_as_user", "iat": now, "nbf": now - 1, "exp": now + 3600,
        }
        claims.update(overrides)
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"})

    yield SimpleNamespace(identity=identity, token=token,
                          obo=importlib.import_module("obo"),
                          mcp=importlib.import_module("fastmcp_server"))
    identity.delegated_verifier.cache_clear()


@pytest.mark.parametrize("override", [
    {"aud": "wrong"}, {"scp": "other"}, {"scp": "", "idtyp": "app"},
    {"azp": "untrusted"}, {"oid": "mallory"}, {"tid": "wrong"},
])
def test_mcp_rejects_non_authorized_users(runtime, override):
    claims = {"aud": "mcp", "azp": "gateway", "scp": "tools.read"}
    claims.update(override)
    assert asyncio.run(runtime.mcp.GatewayTokenVerifier().verify_token(runtime.token(**claims))) is None


def test_whoami_uses_verified_claims_only(runtime, monkeypatch):
    signed = runtime.token(aud="mcp", azp="gateway", scp="tools.read")
    access = asyncio.run(runtime.mcp.GatewayTokenVerifier().verify_token(signed))
    assert access is not None
    monkeypatch.setattr(runtime.mcp, "get_access_token", lambda: access)
    result = asyncio.run(runtime.mcp.whoami.fn())
    assert result == {"tid": "tenant", "oid": "alice", "appid": "gateway",
                      "scp": "tools.read", "authentication_mode": "user-obo"}
    assert signed not in str(result)


def test_service_mode_still_rejects_delegated(runtime):
    verifier = runtime.identity.EntraVerifier("mcp", {"gateway"}, {"alice"})
    with pytest.raises(ValueError, match="Delegated"):
        verifier.verify(runtime.token(aud="mcp", azp="gateway", scp="tools.read"))


def test_empty_delegated_scope_fails_closed(runtime, monkeypatch):
    monkeypatch.setenv("MCP_REQUIRED_SCOPE", "")
    with pytest.raises(ValueError, match="delegated scope"):
        runtime.identity.delegated_verifier("MCP")


def test_obo_input_audience_is_v2_application_id(runtime, monkeypatch):
    monkeypatch.setenv("OBO_INPUT_AUDIENCE", "gateway-guid")
    monkeypatch.setenv("OBO_TOKEN_AUDIENCE", "api://legacy-uri")
    verifier = runtime.identity.delegated_verifier("OBO")
    assert verifier.verify(runtime.token(aud="gateway-guid"))["aud"] == "gateway-guid"
    with pytest.raises(jwt.InvalidAudienceError):
        verifier.verify(runtime.token(aud="api://legacy-uri"))


def test_exchange_cache_isolated_and_refreshed(runtime):
    calls = []

    def exchange(request):
        calls.append(request)
        return httpx.Response(200, json={"access_token": f"exchanged-{len(calls)}", "expires_in": 3600})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(exchange)) as client:
            obo = runtime.obo.OnBehalfOf(client)
            a, b = runtime.token(), runtime.token(oid="bob")
            assert await obo.get_token(f"Bearer {a}", "foundry/.default") == "exchanged-1"
            assert await obo.get_token(f"Bearer {a}", "foundry/.default") == "exchanged-1"
            assert await obo.get_token(f"Bearer {b}", "foundry/.default") == "exchanged-2"
            assert await obo.get_token(f"Bearer {a}", "other/.default") == "exchanged-3"
            obo.cache = {k: (v[0], time.time() + 30) for k, v in obo.cache.items()}
            assert await obo.get_token(f"Bearer {a}", "foundry/.default") == "exchanged-4"
    asyncio.run(exercise())
    assert len(calls) == 4
    assert b"requested_token_use=on_behalf_of" in calls[0].content


@pytest.mark.parametrize("failure", ["credential", "endpoint", "subject"])
def test_exchange_failures_never_fall_back(runtime, monkeypatch, failure):
    client = SimpleNamespace(post=AsyncMock(return_value=httpx.Response(400, json={"error": "invalid_grant"})))
    obo = runtime.obo.OnBehalfOf(client)
    if failure == "credential":
        monkeypatch.setattr(obo, "secret", lambda: (_ for _ in ()).throw(OSError("missing")))
    assertion = "sk-virtual-key" if failure == "subject" else runtime.token()
    with pytest.raises(HTTPException) as error:
        asyncio.run(obo.get_token(f"Bearer {assertion}", "foundry/.default"))
    assert error.value.status_code == (401 if failure == "subject" else 502)
    assert not obo.cache
    if failure == "subject":
        client.post.assert_not_awaited()


@pytest.mark.parametrize("route,variable", [
    ("/a2a", "FOUNDRY_A2A_URL"),
    ("/a2a/hosted", "FOUNDRY_A2A_URL"),
    ("/a2a/declarative", "FOUNDRY_DECLARATIVE_A2A_URL"),
])
def test_adapter_forwards_exchanged_user_not_workload(runtime, monkeypatch, route, variable):
    adapter = importlib.import_module("adapter")
    monkeypatch.setenv("BACKEND_AUTH_MODE", "user-obo")
    monkeypatch.setenv(variable, "https://foundry.example/fixed")
    received = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"ok":true}'

    def upstream(request):
        received.append(request)
        return httpx.Response(200, stream=Body())

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            adapter.app.state.client = client
            adapter.app.state.credential = SimpleNamespace(get_token=AsyncMock())
            adapter.app.state.obo = SimpleNamespace(get_token=AsyncMock(return_value="exchanged-user"))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=adapter.app),
                                         base_url="http://adapter") as caller:
                response = await caller.post(route, json={"method": "tasks/get"},
                                             headers={"Authorization": "Bearer real-subject"})
            assert response.status_code == 200
            adapter.app.state.obo.get_token.assert_awaited_once_with(
                "Bearer real-subject", "https://ai.azure.com/.default")
            adapter.app.state.credential.get_token.assert_not_awaited()
    asyncio.run(exercise())
    assert received[0].headers["authorization"] == "Bearer exchanged-user"
    assert received[0].url == "https://foundry.example/fixed"
    assert b"GetTask" in received[0].content


@pytest.mark.parametrize("card_request", [False, True])
def test_adapter_gzip_json_decoding_and_card_rewrite(runtime, monkeypatch, card_request):
    adapter = importlib.import_module("adapter")
    monkeypatch.setenv("BACKEND_AUTH_MODE", "user-obo")
    variable = "FOUNDRY_DECLARATIVE_A2A_CARD_URL" if card_request else "FOUNDRY_DECLARATIVE_A2A_URL"
    monkeypatch.setenv(variable, "https://foundry.example/fixed")
    payload = (
        {"protocolVersion": "1.0", "url": "https://foundry.example/public",
         "supportedInterfaces": [{"url": "https://foundry.example/public",
                                  "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}]}
        if card_request else {"jsonrpc": "2.0", "id": "request-1", "result": {"text": "Foundry response"}}
    )
    compressed = gzip.compress(json.dumps(payload).encode())
    received = []

    class CompressedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield compressed[:10]
            yield compressed[10:]

    def upstream(request):
        received.append(request)
        return httpx.Response(200, stream=CompressedBody(), headers={
            "content-type": "application/json", "content-encoding": "gzip",
            "authorization": "Bearer upstream-secret", "x-api-key": "upstream-key",
        })

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            adapter.app.state.client = client
            adapter.app.state.credential = SimpleNamespace(get_token=AsyncMock())
            adapter.app.state.obo = SimpleNamespace(get_token=AsyncMock(return_value="exchanged-user"))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=adapter.app),
                                         base_url="http://adapter") as caller:
                headers = {"Authorization": "Bearer real-subject",
                           "x-litellm-api-key": "sk-consumer-key", "api-key": "consumer-api-key"}
                if card_request:
                    response = await caller.get(
                        "/a2a/declarative/.well-known/agent-card.json", headers=headers)
                else:
                    response = await caller.post(
                        "/a2a/declarative", headers=headers,
                        json={"jsonrpc": "2.0", "id": "request-1", "method": "message/send"})
            assert response.status_code == 200
            if card_request:
                assert "content-encoding" not in response.headers
                rewritten = "http://127.0.0.1:4001/a2a/declarative"
                expected = {**payload, "url": rewritten,
                            "supportedInterfaces": [{**payload["supportedInterfaces"][0], "url": rewritten}]}
                assert response.json() == expected
            else:
                assert response.headers["content-encoding"] == "gzip"
                assert response.json() == payload
            for secret in ("real-subject", "sk-consumer-key", "consumer-api-key",
                           "exchanged-user", "upstream-secret", "upstream-key"):
                assert secret not in response.text
                assert secret not in str(response.headers)
            adapter.app.state.credential.get_token.assert_not_awaited()
    asyncio.run(exercise())
    assert received[0].headers["authorization"] == "Bearer exchanged-user"
    assert "x-litellm-api-key" not in received[0].headers
    assert "api-key" not in received[0].headers


@pytest.mark.parametrize("route,variable,rewritten", [
    ("/a2a/.well-known/agent-card.json", "FOUNDRY_A2A_CARD_URL", "/a2a"),
    ("/a2a/declarative/.well-known/agent-card.json",
     "FOUNDRY_DECLARATIVE_A2A_CARD_URL", "/a2a/declarative"),
])
def test_adapter_card_discovery_uses_user_obo(runtime, monkeypatch, route, variable, rewritten):
    adapter = importlib.import_module("adapter")
    monkeypatch.setenv("BACKEND_AUTH_MODE", "user-obo")
    monkeypatch.setenv(variable, "https://foundry.example/fixed/card")
    received = []

    def upstream(request):
        received.append(request)
        return httpx.Response(200, json={
            "protocolVersion": "1.0", "url": "https://foundry.example/public",
            "supportedInterfaces": [{"url": "https://foundry.example/public",
                                     "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
        })

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            adapter.app.state.client = client
            adapter.app.state.credential = SimpleNamespace(get_token=AsyncMock())
            adapter.app.state.obo = SimpleNamespace(get_token=AsyncMock(return_value="exchanged-user"))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=adapter.app),
                                         base_url="http://adapter") as caller:
                response = await caller.get(route, headers={"Authorization": "Bearer real-subject"})
            adapter.app.state.obo.get_token.assert_awaited_once_with(
                "Bearer real-subject", "https://ai.azure.com/.default")
            adapter.app.state.credential.get_token.assert_not_awaited()
            assert response.status_code == 200
            card = response.json()
            assert card["url"] == f"http://127.0.0.1:4001{rewritten}"
            assert card["supportedInterfaces"][0]["url"] == card["url"]
            assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
    asyncio.run(exercise())
    assert received[0].headers["authorization"] == "Bearer exchanged-user"
