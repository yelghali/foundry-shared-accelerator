"""Focused, offline checks for the APIM/LiteLLM development profile."""

import asyncio
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import jwt
import pytest
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request


SCENARIO = Path(__file__).resolve().parents[1]


class ObjectPermissionContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    object_permission_id: str = Field(min_length=1)
    mcp_servers: list[str]
    agents: list[str]


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(SCENARIO))
    monkeypatch.setitem(sys.modules, "litellm.proxy._types", SimpleNamespace(
        UserAPIKeyAuth=SimpleNamespace,
        LiteLLM_ObjectPermissionTable=ObjectPermissionContract,
    ))
    monkeypatch.setitem(sys.modules, "litellm.proxy.mcp_server.mcp_server_manager", SimpleNamespace(
        global_mcp_server_manager=SimpleNamespace(
            get_mcp_server_by_name=lambda name: SimpleNamespace(server_id="blob-id") if name == "blob" else None
        )))
    monkeypatch.setitem(sys.modules, "litellm.proxy.agent_endpoints.agent_registry", SimpleNamespace(
        global_agent_registry=SimpleNamespace(
            get_agent_by_name=lambda name: SimpleNamespace(agent_id="echo-id") if name == "echo" else None
        )))
    names = ("identity", "custom_auth", "adapter", "render", "fastmcp_server")
    previous = {name: sys.modules.pop(name, None) for name in names}
    try:
        loaded = SimpleNamespace(**{name: importlib.import_module(name) for name in names})
        yield loaded
    finally:
        for name in names:
            sys.modules.pop(name, None)
            if previous[name] is not None:
                sys.modules[name] = previous[name]


def request(path, method="POST", body=None, token="token"):
    data = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": data}

    headers = [] if token is None else [(b"authorization", f"Bearer {token}".encode())]
    return Request({"type": "http", "method": method, "path": path,
                    "headers": headers, "query_string": b""}, receive)


@pytest.mark.parametrize("exposure", ["public", "private"])
def test_render_tls_and_private_mcp(modules, monkeypatch, exposure):
    for name in (
        "TENANT_ID", "LITELLM_CLIENT_ID", "FASTMCP_CLIENT_ID", "APIM_TOKEN_AUDIENCE",
        "APIM_PRINCIPAL_ID", "AZURE_API_BASE", "FOUNDRY_A2A_URL", "FOUNDRY_A2A_CARD_URL",
        "MCP_TOKEN_SCOPE", "MCP_TOKEN_AUDIENCE", "LITELLM_PRINCIPAL_ID",
        "BLOB_ACCOUNT_URL", "BLOB_CONTAINER", "LITELLM_IMAGE", "FASTMCP_IMAGE",
        "BLOB_SEED_CLIENT_ID",
    ):
        monkeypatch.setenv(name, "configured")
    monkeypatch.setenv("EXPOSURE", exposure)
    monkeypatch.setenv("FOUNDRY_A2A_URL", "https://foundry.example/agents/echo/endpoint/protocols/a2a")
    monkeypatch.setenv("TLS_SECRET_NAME", "gateway-tls")
    monkeypatch.setenv("PUBLIC_DNS_LABEL", "fsa-lit1120")
    monkeypatch.setenv("PROXY_BASE_URL", "https://gateway.example/ai")
    monkeypatch.setenv("LOAD_BALANCER_SOURCE_RANGES", "203.0.113.1/32,198.51.100.0/24")
    items = modules.render.render()["items"]
    resources = {(item["kind"], item["metadata"]["name"]): item for item in items}
    service = resources["Service", "litellm"]
    assert service["spec"]["ports"] == [{"port": 443, "targetPort": 4000}]
    assert service["metadata"]["annotations"]["service.beta.kubernetes.io/azure-load-balancer-internal"] == str(exposure == "private").lower()
    assert service["spec"]["loadBalancerSourceRanges"] == ["203.0.113.1/32", "198.51.100.0/24"]
    assert service["metadata"]["annotations"].get("service.beta.kubernetes.io/azure-dns-label-name") == (
        "fsa-lit1120" if exposure == "public" else None
    )
    if exposure == "public":
        internal = resources["Service", "litellm-internal"]
        assert internal["spec"]["type"] == "LoadBalancer"
        assert internal["spec"]["selector"] == {"app": "litellm"}
        assert internal["spec"]["ports"] == [{"port": 443, "targetPort": 4000}]
        assert internal["metadata"]["annotations"]["service.beta.kubernetes.io/azure-load-balancer-internal"] == "true"
        assert "service.beta.kubernetes.io/azure-dns-label-name" not in internal["metadata"]["annotations"]
    else:
        assert ("Service", "litellm-internal") not in resources
    assert resources["Service", "fastmcp"]["spec"].get("type", "ClusterIP") == "ClusterIP"
    spec = resources["Deployment", "litellm"]["spec"]["template"]["spec"]
    gateway, = spec["containers"]
    assert gateway["ports"] == [{"containerPort": 4000}]
    assert {"name": "PROXY_BASE_URL", "value": "https://gateway.example/ai"} in gateway["env"]
    assert {"name": "KEEPALIVE_TIMEOUT", "value": "300"} in gateway["env"]
    mcp_container = resources["Deployment", "fastmcp"]["spec"]["template"]["spec"]["containers"][0]
    assert not any(item["name"] == "KEEPALIVE_TIMEOUT" for item in mcp_container["env"])
    assert gateway["args"][-4:] == [
        "--ssl_keyfile_path", "/app/tls/tls.key", "--ssl_certfile_path", "/app/tls/tls.crt",
    ]
    assert gateway["readinessProbe"]["httpGet"]["scheme"] == "HTTPS"
    assert {"name": "tls", "secret": {"secretName": "gateway-tls"}} in spec["volumes"]
    assert spec["serviceAccountName"] == "litellm"
    for name in ("litellm", "fastmcp"):
        assert resources["Deployment", name]["spec"]["template"]["spec"]["enableServiceLinks"] is False
    job = next(item for item in items if item["kind"] == "Job")
    assert job["metadata"]["name"].startswith("blob-seed-")
    assert job["spec"]["ttlSecondsAfterFinished"] == 300
    pod = job["spec"]["template"]
    assert pod["metadata"]["labels"]["azure.workload.identity/use"] == "true"
    assert pod["metadata"]["labels"]["app"] == "blob-seed"
    assert pod["spec"]["serviceAccountName"] == "blob-seed"
    assert pod["spec"]["enableServiceLinks"] is False
    assert pod["spec"]["containers"][0]["image"] == "configured"
    assert resources["ServiceAccount", "fastmcp"]["metadata"]["annotations"]["azure.workload.identity/client-id"] == "configured"
    assert job["metadata"]["name"] == next(
        item["metadata"]["name"] for item in modules.render.render()["items"] if item["kind"] == "Job"
    )
    for key in ("FASTMCP_IMAGE", "BLOB_ACCOUNT_URL", "BLOB_CONTAINER"):
        monkeypatch.setenv(key, "changed")
        changed_job = next(item for item in modules.render.render()["items"] if item["kind"] == "Job")
        assert changed_job["metadata"]["name"] != job["metadata"]["name"]
        monkeypatch.setenv(key, "configured")
    assert resources["NetworkPolicy", "fastmcp-only-from-litellm"]
    original_template = resources["Deployment", "litellm"]["spec"]["template"]
    changed_source = resources["ConfigMap", "litellm-config"]["data"]["config.yaml"] + "\n# rollout change\n"
    with monkeypatch.context() as patch:
        read_config = MagicMock(return_value=changed_source)
        patch.setattr(modules.render.Path, "read_text", read_config)
        changed_items = modules.render.render()["items"]
    read_config.assert_called_once_with(encoding="utf-8")
    changed_resources = {(item["kind"], item["metadata"]["name"]): item for item in changed_items}
    changed_template = changed_resources["Deployment", "litellm"]["spec"]["template"]
    assert changed_template["metadata"]["annotations"]["checksum/config"] != (
        original_template["metadata"]["annotations"]["checksum/config"]
    )
    assert changed_template["spec"] == original_template["spec"]
    assert changed_resources["Deployment", "fastmcp"] == resources["Deployment", "fastmcp"]


def load_source(name):
    spec = importlib.util.spec_from_file_location(name, SCENARIO / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_launcher_retains_durable_native_key(monkeypatch, capsys):
    launch = load_source("launch")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-durable-key")
    monkeypatch.setenv("DATABASE_URL", "postgresql://litellm:password@litellm-postgres/litellm")
    monkeypatch.setattr(launch.sys, "argv", ["launch.py", "--config", "/app/scenario/config.yaml"])
    calls = []
    monkeypatch.setattr(launch.os, "execvp", lambda executable, args: calls.append(
        (executable, args, launch.os.environ["LITELLM_MASTER_KEY"])))
    launch.main()
    launch.main()
    assert calls[0][0:2] == ("litellm", ["litellm", "--config", "/app/scenario/config.yaml"])
    assert calls[0][2] == "sk-durable-key"
    assert calls[0][2] == calls[1][2]
    assert calls[0][2] not in repr(calls[0][1])
    assert capsys.readouterr() == ("", "")
    config = (SCENARIO / "config.yaml").read_text(encoding="utf-8")
    assert "custom_auth:" not in config
    assert "litellm_key_header_name: x-litellm-api-key" in config
    for name in ("config.yaml", "launch.py", "render.py"):
        assert "dangerously_permit_weak_or_unset_master_key" not in (
            SCENARIO / name
        ).read_text(encoding="utf-8")


def test_blob_seed_only_writes_verification_blob(monkeypatch):
    credential = MagicMock()
    client = MagicMock()
    identity = SimpleNamespace(WorkloadIdentityCredential=credential)
    storage = SimpleNamespace(BlobServiceClient=client)
    monkeypatch.setitem(sys.modules, "azure.identity", identity)
    monkeypatch.setitem(sys.modules, "azure.storage.blob", storage)
    monkeypatch.setenv("BLOB_ACCOUNT_URL", "https://private.blob.core.windows.net")
    monkeypatch.setenv("BLOB_CONTAINER", "dedicated")
    load_source("blob_seed").main()
    client.assert_called_once_with(
        "https://private.blob.core.windows.net", credential=credential.return_value.__enter__.return_value
    )
    service = client.return_value.__enter__.return_value
    service.get_blob_client.assert_called_once_with(container="dedicated", blob="gateway-check.txt")
    service.get_blob_client.return_value.upload_blob.assert_called_once_with(
        b"MCP_WORKLOAD_IDENTITY_OK", overwrite=True
    )


def test_render_requires_explicit_exposure(modules, monkeypatch):
    monkeypatch.delenv("EXPOSURE", raising=False)
    with pytest.raises(ValueError, match="EXPOSURE"):
        modules.render.render()
    monkeypatch.setenv("EXPOSURE", "internet")
    with pytest.raises(ValueError, match="EXPOSURE"):
        modules.render.render()


@pytest.mark.parametrize("failure", [jwt.InvalidTokenError("secret-token"), ValueError("secret-token")])
def test_auth_rejects_bad_tokens_without_logging_secrets(modules, monkeypatch, caplog, failure):
    monkeypatch.setattr(modules.custom_auth, "verifier", lambda _: SimpleNamespace(
        verify_async=AsyncMock(side_effect=failure)))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(modules.custom_auth.user_api_key_auth(request("/v1/models"), "ignored"))
    assert exc.value.status_code == 401
    assert "identity rejected" in caplog.text
    assert "secret-token" not in caplog.text


@pytest.mark.parametrize("path,method,body,status", [
    ("/v1/models", "GET", None, None),
    ("/v1/chat/completions", "POST", {"model": "enterprise-chat"}, None),
    ("/v1/chat/completions", "POST", {"model": "other"}, 403),
    ("/v1/chat/completions", "POST", {"model": "enterprise-chat", "api_key": "override"}, 403),
    ("/v1/responses", "POST", {"model": "enterprise-chat", "tools": [
        {"type": "mcp", "server_url": "https://external.example/mcp"}]}, 403),
    ("/v1/responses", "POST", {"model": "enterprise-chat", "extra_body": {"tools": [
        {"type": "mcp", "server_url": "https://external.example/mcp"}]}}, 403),
    ("/v1/responses", "POST", {"model": "enterprise-chat", "extra_body": {"api_base": "https://external"}}, 403),
    ("/v1/chat/completions", "POST", {"model": "enterprise-chat", "tools": [
        {"type": "function", "function": {"name": "local", "parameters": {"type": "object"}}}]}, None),
    ("/key/generate", "POST", {}, 403),
    ("/blob/mcp", "DELETE", None, None),
    ("/other/mcp", "POST", None, 403),
    ("/a2a/echo", "POST", {}, None),
])
def test_auth_allowlist(modules, monkeypatch, path, method, body, status):
    for key, value in {"MODEL_ALIAS": "enterprise-chat", "A2A_AGENT_NAME": "echo",
                       "MCP_SERVER_NAME": "blob"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(modules.custom_auth, "verifier", lambda _: SimpleNamespace(
        verify_async=AsyncMock(return_value={"oid": "apim"})))
    call = modules.custom_auth.user_api_key_auth(request(path, method, body), "ignored")
    if status:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(call)
        assert exc.value.status_code == status
    else:
        result = asyncio.run(call)
        assert result.user_id == "apim"
        assert result.user_role == "internal_user"
        assert result.object_permission.mcp_servers == (["blob-id"] if path == "/blob/mcp" else [])
        assert result.object_permission.agents == (["echo-id"] if path == "/a2a/echo" else [])


@pytest.mark.parametrize("path,original,root,status", [
    ("/mcp/blob", "/blob/mcp", "", None),
    ("/mcp/blob/", "/blob/mcp/", "", None),
    ("/ai/mcp/blob", "/ai/blob/mcp", "/ai", None),
    ("/mcp/blob", "/other/mcp", "", 403),
    ("/mcp/other", "/blob/mcp", "", 403),
    ("/mcp/blob", "/blob,other/mcp", "", 403),
    ("/mcp/blob", None, "", 403),
    ("/mcp", None, "", 403),
    ("/mcp", "/blob/mcp", "", 403),
    ("/mcp/blob", "/toolset/blob/mcp", "", 403),
])
def test_auth_named_mcp_internal_path(modules, monkeypatch, path, original, root, status):
    for key, value in {"MODEL_ALIAS": "enterprise-chat", "A2A_AGENT_NAME": "echo",
                       "MCP_SERVER_NAME": "blob"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(modules.custom_auth, "verifier", lambda _: SimpleNamespace(
        verify_async=AsyncMock(return_value={"oid": "apim"})))
    req = request(path)
    req.scope["root_path"] = root
    if original is not None:
        req.scope["_original_path"] = original
    # Routing decisions must not trust a client-supplied "original path" header.
    req.scope["headers"].append((b"x-original-path", b"/blob/mcp"))
    call = modules.custom_auth.user_api_key_auth(req, "ignored")
    if status:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(call)
        assert exc.value.status_code == status
    else:
        result = asyncio.run(call)
        assert result.object_permission.mcp_servers == ["blob-id"]
        assert result.object_permission.agents == []


def test_auth_missing_registry_objects_fail_closed(modules, monkeypatch):
    with pytest.raises(HTTPException) as exc:
        modules.custom_auth.object_permissions("unknown", "echo", mcp_route=True, agent_route=False)
    assert exc.value.status_code == 503
    with pytest.raises(HTTPException) as exc:
        modules.custom_auth.object_permissions("blob", "unknown", mcp_route=False, agent_route=True)
    assert exc.value.status_code == 503


def test_auth_missing_and_unexpected_failure(modules, monkeypatch):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(modules.custom_auth.user_api_key_auth(request("/", token=None), "ignored"))
    assert exc.value.status_code == 401
    monkeypatch.setattr(modules.custom_auth, "verifier", lambda _: SimpleNamespace(
        verify_async=AsyncMock(side_effect=RuntimeError("programming error"))))
    with pytest.raises(RuntimeError):
        asyncio.run(modules.custom_auth.user_api_key_auth(request("/"), "ignored"))


@pytest.mark.parametrize("change", [
    {"tid": "other"}, {"iss": "https://evil.invalid"}, {"oid": "other"},
    {"appid": "other"}, {"scp": "user_impersonation"},
])
def test_verifier_fails_closed(modules, monkeypatch, change):
    monkeypatch.setenv("TENANT_ID", "tenant")
    verifier = modules.identity.EntraVerifier("aud", {"client"}, {"principal"})
    claims = {"tid": "tenant", "iss": "https://sts.windows.net/tenant/",
              "oid": "principal", "appid": "client"}
    claims.update(change)
    monkeypatch.setattr(verifier.jwks, "get_signing_key_from_jwt", lambda _: SimpleNamespace(key="key"))
    monkeypatch.setattr(modules.identity.jwt, "decode", lambda *a, **kw: claims)
    with pytest.raises(ValueError):
        verifier.verify("token")


@pytest.mark.parametrize("result", [None, jwt.InvalidTokenError("secret"), ValueError("secret")])
def test_mcp_auth_rejects_none_and_jwt_errors(modules, monkeypatch, caplog, result):
    verify = AsyncMock(side_effect=result) if isinstance(result, Exception) else AsyncMock(return_value=result)
    monkeypatch.setattr(modules.fastmcp_server, "verifier", lambda _: SimpleNamespace(verify_async=verify))
    assert asyncio.run(modules.fastmcp_server.GatewayTokenVerifier().verify_token("secret")) is None
    assert "identity rejected" in caplog.text
    assert "secret" not in caplog.text


def test_auth_permission_ids_separate_route_grants(modules, monkeypatch):
    for key, value in {"MODEL_ALIAS": "enterprise-chat", "A2A_AGENT_NAME": "echo",
                       "MCP_SERVER_NAME": "blob"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(modules.custom_auth, "verifier", lambda _: SimpleNamespace(
        verify_async=AsyncMock(return_value={"oid": "apim"})))
    routes = [
        ("/v1/chat/completions", {"model": "enterprise-chat"}, [], []),
        ("/blob/mcp", {}, ["blob-id"], []),
        ("/a2a/echo", {}, [], ["echo-id"]),
    ]
    ids = []
    for path, body, mcp_ids, agent_ids in routes:
        results = [
            asyncio.run(modules.custom_auth.user_api_key_auth(request(path, body=body), "ignored"))
            for _ in range(2)
        ]
        permission = results[0].object_permission
        assert permission.object_permission_id == results[1].object_permission.object_permission_id
        assert permission.mcp_servers == mcp_ids
        assert permission.agents == agent_ids
        assert results[0].user_id == "apim"
        assert results[0].user_role == "internal_user"
        ids.append(permission.object_permission_id)
    assert len(set(ids)) == len(routes)


@pytest.mark.parametrize("method,path", [
    ("POST", "/a2a"), ("GET", "/mcp"), ("POST", "/mcp"), ("DELETE", "/mcp"),
    ("GET", "/.well-known/agent-card.json"),
    ("GET", "/a2a/.well-known/agent-card.json"),
    ("GET", "/a2a/.well-known/agent.json"),
])
def test_adapter_preserves_protocol_and_rewrites_all_card_interfaces(modules, monkeypatch, method, path):
    monkeypatch.setenv("FOUNDRY_A2A_URL", "https://foundry.example/invoke?api-version=1")
    monkeypatch.setenv("FOUNDRY_A2A_CARD_URL", "https://foundry.example/card")
    monkeypatch.setenv("FASTMCP_URL", "http://fastmcp.gateway.svc.cluster.local:8000/mcp")
    monkeypatch.setenv("MCP_TOKEN_SCOPE", "scope")
    observed = []
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": "saved-task"},
    }).encode() if path == "/a2a" else b"payload"
    card = {"name": "upstream", "url": "https://upstream", "skills": [{"id": "echo"}],
            "additionalInterfaces": [{"url": "https://upstream/old", "transport": "JSONRPC"}],
            "supportedInterfaces": [{"url": "https://upstream/new", "protocolBinding": "JSONRPC",
                                     "protocolVersion": "1.0"}]}

    class CardStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield json.dumps(card).encode()

    async def upstream(req):
        observed.append(req)
        return httpx.Response(200, stream=CardStream(), headers={
            "mcp-session-id": "session", "content-type": "application/json",
        })

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            app = modules.adapter.app
            app.state.client = client
            app.state.credential = SimpleNamespace(get_token=AsyncMock(return_value=SimpleNamespace(token="renewed")))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://adapter") as downstream:
                response = await downstream.request(method, path, content=body, headers={
                    "Authorization": "Bearer user-secret", "MCP-Protocol-Version": "2025-03-26",
                    "MCP-Session-Id": "session", "A2A-Version": "1.0"})
            assert response.status_code == 200
            if "/.well-known/" in path:
                result = response.json()
                assert result["skills"] == card["skills"]
                for field in ("additionalInterfaces", "supportedInterfaces"):
                    assert result[field][0]["url"] == "http://127.0.0.1:4001/a2a"
                assert result["supportedInterfaces"][0]["protocolVersion"] == "1.0"
            else:
                assert response.headers["mcp-session-id"] == "session"
                assert response.json() == card
    asyncio.run(run())
    forwarded = observed[0]
    assert forwarded.method == method
    assert forwarded.content == body
    assert forwarded.headers["authorization"] == "Bearer renewed"
    assert forwarded.headers["a2a-version"] == "1.0"
    assert forwarded.headers["mcp-protocol-version"] == "2025-03-26"


@pytest.mark.parametrize("path", ["/a2a", "/mcp"])
@pytest.mark.parametrize("incoming_version", [None, "0.3"])
def test_adapter_native_task_version_does_not_affect_mcp(modules, monkeypatch, path, incoming_version):
    monkeypatch.setenv("FOUNDRY_A2A_URL", "https://foundry.example/invoke")
    monkeypatch.setenv("FASTMCP_URL", "http://fastmcp.gateway.svc.cluster.local:8000/mcp")
    monkeypatch.setenv("MCP_TOKEN_SCOPE", "scope")
    observed = []
    payload = {"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": "saved-task"}}
    if path == "/mcp":
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

    async def upstream(req):
        observed.append(req)
        return httpx.Response(204)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            app = modules.adapter.app
            app.state.client = client
            app.state.credential = SimpleNamespace(
                get_token=AsyncMock(return_value=SimpleNamespace(token="renewed"))
            )
            headers = {} if incoming_version is None else {"A2A-Version": incoming_version}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://adapter"
            ) as downstream:
                response = await downstream.post(path, json=payload, headers=headers)
            assert response.status_code == 204
    asyncio.run(run())
    forwarded = observed[0]
    assert json.loads(forwarded.content) == payload
    expected = ["1.0"] if path == "/a2a" else ([] if incoming_version is None else [incoming_version])
    assert forwarded.headers.get_list("a2a-version") == expected


@pytest.mark.parametrize("path", ["/a2a", "/mcp"])
@pytest.mark.parametrize("method", ["tasks/get", "GetTask"])
def test_adapter_only_converts_legacy_get_task_on_foundry_route(modules, monkeypatch, path, method):
    monkeypatch.setenv("FOUNDRY_A2A_URL", "https://foundry.example/invoke")
    monkeypatch.setenv("FASTMCP_URL", "http://fastmcp.gateway.svc.cluster.local:8000/mcp")
    monkeypatch.setenv("MCP_TOKEN_SCOPE", "scope")
    payload = {
        "jsonrpc": "2.0", "id": "rpc-correlation", "method": method,
        "params": {"id": "saved-task", "historyLength": 2},
        "extension": {"request": "preserve"},
    }
    body = json.dumps(payload, indent=2).encode()
    observed = []

    async def upstream(req):
        observed.append(req)
        return httpx.Response(204)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            app = modules.adapter.app
            app.state.client = client
            app.state.credential = SimpleNamespace(
                get_token=AsyncMock(return_value=SimpleNamespace(token="renewed"))
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://adapter"
            ) as downstream:
                response = await downstream.post(path, content=body, headers={"content-type": "application/json"})
            assert response.status_code == 204
    asyncio.run(run())
    forwarded = observed[0]
    expected = {**payload, "method": "GetTask"} if path == "/a2a" else payload
    assert json.loads(forwarded.content) == expected
    if path == "/mcp" or method == "GetTask":
        assert forwarded.content == body


@pytest.mark.parametrize("body", [b"{", b"\xff"])
def test_adapter_invalid_a2a_payload_fails_without_forwarding(modules, monkeypatch, body):
    monkeypatch.setenv("FOUNDRY_A2A_URL", "https://foundry.example/invoke")
    client = SimpleNamespace(build_request=MagicMock(), send=AsyncMock())
    app = modules.adapter.app
    app.state.client = client
    app.state.credential = SimpleNamespace(
        get_token=AsyncMock(return_value=SimpleNamespace(token="renewed"))
    )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://adapter"
        ) as downstream:
            response = await downstream.post("/a2a", content=body)
        assert response.status_code == 400
    asyncio.run(run())
    client.build_request.assert_not_called()
    client.send.assert_not_awaited()
