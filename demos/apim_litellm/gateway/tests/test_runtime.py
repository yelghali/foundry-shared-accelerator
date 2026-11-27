"""Focused, offline checks for the APIM/LiteLLM development profile."""

import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest


SCENARIO = Path(__file__).resolve().parents[1]


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(SCENARIO))
    names = ("identity", "render", "fastmcp_server")
    previous = {name: sys.modules.pop(name, None) for name in names}
    try:
        loaded = SimpleNamespace(**{name: importlib.import_module(name) for name in names})
        yield loaded
    finally:
        for name in names:
            sys.modules.pop(name, None)
            if previous[name] is not None:
                sys.modules[name] = previous[name]


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
