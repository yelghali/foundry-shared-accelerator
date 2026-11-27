"""Offline delegated identity tests: real signatures, mocked Entra exchange only."""

import asyncio
import importlib
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
