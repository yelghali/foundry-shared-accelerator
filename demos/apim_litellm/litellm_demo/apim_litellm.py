"""Release helpers and live acceptance for the separate APIM/LiteLLM scenario."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from azure.identity import AzureDeveloperCliCredential

from litellm_demo.control import credential, load_json, save_receipt
from litellm_demo.mcp import MCPClient, MCPProtocolError, parse_rpc_response
from .gateway_keys import NAMESPACES, read_key


def create_development_certificate(host: str, directory: Path) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    directory.mkdir(parents=True, exist_ok=True)
    key_path, cert_path = directory / "tls.key", directory / "tls.crt"
    if key_path.exists() != cert_path.exists():
        raise ValueError("Both TLS files must exist, or neither; refusing to overwrite an incomplete key pair.")
    if key_path.exists():
        certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
        names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
        if host not in names:
            raise ValueError(f"The existing TLS certificate does not cover {host}; explicitly rotate the TLS pair.")
        if certificate.not_valid_after_utc <= datetime.now(timezone.utc) + timedelta(days=1):
            raise ValueError("Development certificate is expiring; explicitly rotate the TLS pair.")
        stored_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        public_format = (serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        if stored_key.public_key().public_bytes(*public_format) != certificate.public_key().public_bytes(*public_format):
            raise ValueError("The stored TLS private key does not match the certificate.")
        return
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ))
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))


def mcp_authorization_environment(path: Path, namespace: str) -> dict[str, str]:
    """Keep approved hosted callers separate from the gateway's own identity."""
    policy = load_json(path)
    if policy.get("namespace") not in {NAMESPACES["shared-service"], NAMESPACES["dedicated-application"]}:
        raise ValueError("Hosted service caller policy must target a service-identity MCP namespace")
    if policy.get("namespace") != namespace:
        print(f"MCP caller policy targets {policy.get('namespace')}; no additional callers for {namespace}.")
        return {}
    result = {}
    for field, variable in (
        ("client_ids", "MCP_FOUNDRY_CLIENT_IDS"),
        ("principal_ids", "MCP_FOUNDRY_PRINCIPAL_IDS"),
    ):
        values = policy.get(field)
        if not isinstance(values, list) or not values or not all(isinstance(value, str) for value in values):
            raise ValueError(f"MCP caller policy requires a nonempty {field} list")
        for value in values:
            uuid.UUID(value)
        result[variable] = ",".join(values)
    print(f"Using approved hosted MCP caller policy for namespace {namespace}.")
    return result


def prepare(args: argparse.Namespace) -> None:
    if getattr(args, "private_demo", False) and not getattr(args, "enable_agent_responses", False):
        raise ValueError("--private-demo requires --enable-agent-responses")
    deployment = load_json(args.deployment)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    identities = deployment["workload_identities"]
    scenario = args.scenario
    namespace = NAMESPACES[scenario]
    profile_file = Path(args.scenario_contract) if args.scenario_contract else Path(args.deployment).with_name("terraform-outputs.json")
    outputs = load_json(profile_file) if profile_file.exists() else {}
    profiles = outputs.get("identity_scenarios", {}).get("value", {})
    if scenario != "shared-service" and scenario not in profiles:
        raise ValueError("Deploy identity-scenario infrastructure and export terraform-outputs.json first.")
    gateway_identity = profiles.get(scenario, identities["litellm"])
    audience = deployment.get("mcp_service_token_audience") or deployment["apim"]["litellm_token_audience"]
    env = {
        **os.environ,
        "TENANT_ID": deployment["tenant_id"],
        "AUTHENTICATION_SCENARIO": scenario,
        "GATEWAY_NAMESPACE": namespace,
        "LITELLM_CLIENT_ID": gateway_identity["client_id"],
        "LITELLM_PRINCIPAL_ID": gateway_identity["principal_id"],
        "FASTMCP_CLIENT_ID": identities["fastmcp"]["client_id"],
        "BLOB_SEED_CLIENT_ID": identities["blob-seed"]["client_id"],
        "MCP_TOKEN_AUDIENCE": audience,
        "MCP_TOKEN_SCOPE": audience.rstrip("/") + "/.default",
        "BLOB_ACCOUNT_URL": deployment["blob"]["endpoint"],
        "BLOB_CONTAINER": deployment["blob"]["container"],
        "FOUNDRY_A2A_URL": args.a2a_endpoint.rstrip("/"),
        "FOUNDRY_A2A_CARD_URL": args.a2a_endpoint.rstrip("/") + "/agentCard/v1.0",
        "FOUNDRY_DECLARATIVE_A2A_URL": args.declarative_endpoint or (
            args.a2a_endpoint.rstrip("/").rsplit("/agents/", 1)[0]
            + "/agents/gateway-declarative/endpoint/protocols/a2a"
        ),
        "AZURE_API_BASE": args.model_endpoint,
        "AZURE_MODEL": "azure/" + args.model_deployment,
        "LITELLM_IMAGE": deployment["acr"]["login_server"] + "/apim-litellm:" + args.image_tag,
        "FASTMCP_IMAGE": deployment["acr"]["login_server"] + "/fastmcp:" + args.image_tag,
        "EXPOSURE": args.exposure,
        "PUBLIC_DNS_LABEL": args.dns_label,
        "PROXY_BASE_URL": deployment["apim"]["gateway_url"].rstrip("/") + (
            "/ai" if scenario == "shared-service" else "/ai-" + scenario
        ),
    }
    policy_path = Path(getattr(args, "mcp_authorization", None) or
                       Path(args.deployment).with_name("hosted-mcp-authorization.json"))
    if policy_path.is_file():
        env.update(mcp_authorization_environment(policy_path, namespace))
    elif getattr(args, "private_demo", False) and scenario == "shared-service" and not all(
        env.get(name, "").strip() for name in ("MCP_FOUNDRY_CLIENT_IDS", "MCP_FOUNDRY_PRINCIPAL_IDS")
    ):
        raise ValueError("Private hosted MCP requires its approved caller policy (--mcp-authorization) or explicit caller allowlists")
    env["FOUNDRY_DECLARATIVE_A2A_CARD_URL"] = env["FOUNDRY_DECLARATIVE_A2A_URL"].rstrip("/") + "/agentCard/v1.0"
    if getattr(args, "enable_agent_responses", False):
        project = (getattr(args, "agent_responses_project_endpoint", None)
                   or args.a2a_endpoint.rstrip("/").rsplit("/agents/", 1)[0]).rstrip("/")
        env.update({
            "ENABLE_AGENT_RESPONSES": "true",
            "FOUNDRY_PROMPT_RESPONSES_BASE": project + "/agents/gateway-declarative/endpoint/protocols/openai",
            "FOUNDRY_HOSTED_RESPONSES_BASE": (
                project + "/agents/gateway-hosted-dual/endpoint/protocols/openai"
            ),
        })
    if scenario == "user-obo":
        if not args.identity:
            raise ValueError("user-obo requires the Entra setup receipt; no service-identity fallback is allowed.")
        identity = load_json(args.identity)
        if identity["tenant_id"] != deployment["tenant_id"]:
            raise ValueError("The OBO identity receipt belongs to a different tenant.")
        allowed_users = identity.get("permitted_user_object_ids", [])
        if not allowed_users:
            raise ValueError("An explicit permitted-user allowlist is required for the OBO sample.")
        env.update({
            "MCP_AUTH_MODE": "user-obo",
            "OBO_TENANT_ID": identity["tenant_id"],
            "OBO_CLIENT_ID": identity["gateway"]["app_id"],
            "OBO_INPUT_AUDIENCE": identity["gateway"]["app_id"],
            "OBO_REQUIRED_SCOPE": "access_as_user",
            "OBO_ALLOWED_CLIENT_IDS": ",".join([identity["test_client"]["app_id"], "04b07795-8ddb-461a-bbee-02f9e1bf7b46"]),
            "OBO_ALLOWED_PRINCIPAL_IDS": ",".join(allowed_users),
            "MCP_TOKEN_AUDIENCE": identity["mcp"]["app_id"],
            "MCP_ALLOWED_CLIENT_IDS": identity["gateway"]["app_id"],
            "MCP_ALLOWED_PRINCIPAL_IDS": ",".join(allowed_users),
            "MCP_REQUIRED_SCOPE": "tools.read",
            "MCP_TOKEN_SCOPE": identity["mcp"]["audience"] + "/.default",
            "OBO_CLIENT_SECRET_FILE": "/mnt/secrets-store/obo-client-secret",
            "OBO_KEY_VAULT_NAME": identity["key_vault_name"],
        })
    renderer = Path(__file__).resolve().parents[1] / "gateway" / "render.py"
    rendered = subprocess.run(
        [sys.executable, str(renderer)], env=env, check=False, capture_output=True, text=True
    )
    if rendered.returncode:
        raise RuntimeError(f"Workload manifest generation failed: {rendered.stderr[-2000:]}")
    (output / "workloads.json").write_text(rendered.stdout, encoding="utf-8")
    if args.exposure == "public":
        host = f"{args.dns_label}.{deployment['location']}.cloudapp.azure.com"
        create_development_certificate(host, output)


def canonical_artifacts(task: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {**artifact, "parts": [
            {"mediaType": "text/plain", **part} if "text" in part else part
            for part in artifact.get("parts", [])
        ]}
        for artifact in task.get("artifacts", [])
    ]

def completed_a2a_text(result: dict[str, Any]) -> str:
    task = result.get("task")
    if isinstance(task, dict):
        if task.get("status", {}).get("state") not in {"completed", "TASK_STATE_COMPLETED"}:
            raise RuntimeError("Declarative A2A task did not complete successfully.")
        return "".join(
            part["text"] for artifact in task.get("artifacts", []) for part in artifact.get("parts", [])
            if isinstance(part.get("text"), str)
        )
    message = result.get("message")
    if not isinstance(message, dict) or message.get("role") not in {"agent", "ROLE_AGENT"}:
        raise RuntimeError("Expected a native A2A agent message or completed task.")
    return "".join(part["text"] for part in message.get("parts", []) if isinstance(part.get("text"), str))


def completed_response_text(result: dict[str, Any]) -> str:
    if result.get("status") != "completed" or result.get("error") is not None:
        raise RuntimeError("The agent Responses request did not complete successfully.")
    messages = [
        item for item in result.get("output", [])
        if item.get("type") == "message" and item.get("role") == "assistant"
    ]
    if not messages:
        raise RuntimeError("The agent Responses result contained no assistant message.")
    return "".join(
        part["text"] for item in messages
        for part in item.get("content", []) if part.get("type") == "output_text"
    )


def verify(
    deployment: dict[str, Any], *, use_azd: bool = False, scenario: str = "shared-service",
    identity_receipt: dict[str, Any] | None = None, virtual_key: str | None = None,
    validate_permissions: bool = False, declarative: bool = False, user_token_cache: str | None = None,
    expected_backend_principal_id: str | None = None,
    reject_key_from: str | None = None,
    private_demo: bool = False,
) -> dict[str, Any]:
    path = "/ai" if scenario == "shared-service" else "/ai-" + scenario
    base = deployment["apim"]["gateway_url"].rstrip("/") + path
    if use_azd:
        identity = AzureDeveloperCliCredential(tenant_id=deployment.get("tenant_id"), process_timeout=180)
    else:
        identity = credential()
    scope = deployment["apim"]["caller_audience"].rstrip("/") + "/.default"
    if scenario == "user-obo":
        if not identity_receipt:
            raise ValueError("user-obo acceptance requires an Entra identity receipt.")
        user_consent = (identity_receipt.get("consent_mode") == "user"
                        and identity_receipt.get("user_consent_configured") is True)
        if not identity_receipt.get("admin_consent_requested") and not user_consent:
            raise ValueError("Tenant admin consent is not recorded; run Entra provisioning with --grant-admin-consent first.")
        if user_consent and not user_token_cache:
            raise ValueError("User-consent acceptance requires an actual user login and --user-token-cache.")
        scope = identity_receipt["gateway"]["scope"]
    if user_token_cache:
        if scenario != "user-obo" or identity_receipt is None:
            raise ValueError("A delegated user cache is supported only in the user-obo scenario.")
        from .entra_setup import cached_user_token
        token = cached_user_token(identity_receipt, user_token_cache)
    else:
        token = identity.get_token(scope).token
    key = virtual_key or read_key(scenario)
    headers = {"Authorization": "Bearer " + token, "x-litellm-api-key": "Bearer " + key}
    hosted_alias = "hosted"
    checks: dict[str, Any] = {}
    for path in ["/v1/chat/completions", "/blob/mcp", "/a2a/hosted"]:
        response = requests.post(base + path, json={}, timeout=60)
        if response.status_code not in {401, 403}:
            raise RuntimeError(f"Anonymous {path} was not denied: {response.status_code}.")
    checks["anonymous_denied"] = True
    if reject_key_from:
        if reject_key_from == scenario:
            raise ValueError("Cross-application isolation must use another scenario's key.")
        rejected = requests.get(
            base + "/v1/models",
            headers={**headers, "x-litellm-api-key": "Bearer " + read_key(reject_key_from)},
            timeout=60,
        )
        if rejected.status_code not in {401, 403}:
            raise RuntimeError("Another gateway's application key crossed the namespace/database boundary.")
        checks["foreign_application_key_denied"] = reject_key_from
    if validate_permissions:
        for description, request_headers in [
            ("missing_virtual_key", {"Authorization": "Bearer " + token}),
            ("invalid_virtual_key", {**headers, "x-litellm-api-key": "Bearer sk-invalid-acceptance"}),
        ]:
            denied = requests.get(base + "/v1/models", headers=request_headers, timeout=60)
            if denied.status_code not in {401, 403}:
                raise RuntimeError(f"{description} unexpectedly returned {denied.status_code}.")
            checks[description + "_denied"] = True
        denied = requests.post(base + "/key/generate", headers=headers, json={}, timeout=60)
        if denied.status_code != 403:
            raise RuntimeError("APIM exposed the private key-management API.")
        restricted_headers = {**headers, "x-litellm-api-key": "Bearer " + read_key(scenario, "models-only")}
        for backend_path in ["/blob/mcp", "/a2a/" + hosted_alias]:
            denied_payload = (
                {"jsonrpc": "2.0", "id": 50, "method": "tools/list", "params": {}}
                if backend_path == "/blob/mcp" else
                {"jsonrpc": "2.0", "id": 50, "method": "SendMessage", "params": {
                    "message": {"messageId": str(uuid.uuid4()), "role": "ROLE_USER",
                                "parts": [{"text": "This request must be denied."}]},
                    "configuration": {"returnImmediately": False},
                }}
            )
            denied = requests.post(
                base + backend_path, headers=restricted_headers,
                json=denied_payload, timeout=90,
            )
            if denied.status_code not in {401, 403}:
                if backend_path != "/blob/mcp" or denied.status_code != 200:
                    raise RuntimeError(f"Models-only key unexpectedly reached {backend_path}: {denied.status_code}.")
                try:
                    parse_rpc_response(denied, 50)
                except MCPProtocolError as error:
                    if error.error.get("code") != -32600 or error.error.get("message") != (
                        "The key is not allowed to access the requested MCP servers: blob"
                    ):
                        raise
                else:
                    raise RuntimeError("Models-only key unexpectedly received MCP access.")
        checks["models_only_key_backend_denials"] = True
    response = requests.post(
        base + "/v1/chat/completions", headers=headers,
        json={"model": "enterprise-chat", "messages": [{"role": "user", "content": "Reply exactly GATEWAY_OK"}],
              "temperature": 0, "max_tokens": 20},
        timeout=180,
    )
    response.raise_for_status()
    model_result = response.json()
    if model_result["choices"][0]["message"]["content"].strip() != "GATEWAY_OK":
        raise RuntimeError("Model acceptance did not return the exact marker.")
    checks["model"] = {"marker": "GATEWAY_OK", "usage": model_result["usage"]}
    denied = requests.post(
        base + "/v1/chat/completions", headers=headers,
        json={"model": "not-approved", "messages": [{"role": "user", "content": "hello"}]}, timeout=90,
    )
    if denied.status_code not in {401, 403}:
        raise RuntimeError(f"Unapproved model was not denied: {denied.status_code}.")
    checks["unapproved_model_denied"] = True
    client = MCPClient(base + "/blob/mcp", token)
    client.headers["x-litellm-api-key"] = "Bearer " + key
    try:
        client.initialize()
        tools = client.call("tools/list")["tools"]
        names = [tool["name"] for tool in tools]
        read_tools = [name for name in names if name.endswith("read_blob")]
        if len(read_tools) != 1:
            raise RuntimeError(f"Expected exactly one Blob reader, got {names}.")
        result = client.call("tools/call", {"name": read_tools[0], "arguments": {"name": "gateway-check.txt"}})
        if result.get("isError") or "MCP_WORKLOAD_IDENTITY_OK" not in json.dumps(result):
            raise RuntimeError("MCP did not read the acceptance blob successfully.")
        checks["mcp"] = {"tools": names, "blob_marker": "MCP_WORKLOAD_IDENTITY_OK"}
        whoami_tools = [name for name in names if name.endswith("whoami")]
        if scenario == "user-obo" and len(whoami_tools) != 1:
            raise RuntimeError("Delegated MCP must expose the identity-evidence tool.")
        if whoami_tools:
            identity_result = client.call("tools/call", {"name": whoami_tools[0], "arguments": {}})
            if identity_result.get("isError"):
                raise RuntimeError("MCP identity verification failed.")
            claims = identity_result.get("structuredContent")
            if not isinstance(claims, dict):
                texts = [part["text"] for part in identity_result.get("content", []) if part.get("type") == "text"]
                claims = json.loads("".join(texts))
            if scenario == "user-obo":
                if claims.get("oid") not in identity_receipt["permitted_user_object_ids"] or claims.get("authentication_mode") != "user-obo":
                    raise RuntimeError("MCP did not authorize the expected delegated user.")
            elif expected_backend_principal_id and claims.get("oid") != expected_backend_principal_id:
                raise RuntimeError("MCP received a different service identity than the selected scenario.")
            checks["mcp"]["backend_identity"] = claims
    finally:
        client.session.close()
    response = requests.post(
        base + "/v1/chat/completions", headers=headers,
        json={
            "model": "enterprise-chat",
            "messages": [{"role": "user", "content": (
                "Use the blob MCP read_blob tool to read gateway-check.txt and return its exact complete contents. "
                "Do not invent its contents."
            )}],
            "tools": [{
                "type": "mcp", "server_url": "litellm_proxy", "server_label": "blob",
                "require_approval": "never", "allowed_tools": read_tools,
            }],
            "temperature": 0, "max_tokens": 300,
        }, timeout=240,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"].get("content")
    if not isinstance(content, str) or "MCP_WORKLOAD_IDENTITY_OK" not in content:
        raise RuntimeError("Model-plus-MCP Playground acceptance did not return the actual Blob marker.")
    checks["mcp_playground"] = {"actual_blob_marker": True, "registered_tools": read_tools}
    rpc_headers = {**headers, "A2A-Version": "1.0"}
    request_id = 100

    def rpc(method: str, params: dict[str, Any], agent: str = "hosted") -> dict[str, Any]:
        nonlocal request_id
        request_id += 1
        url = base + "/a2a/" + agent
        print(f"POST {url} [A2A {method}]", file=sys.stderr, flush=True)
        response = requests.post(
            url, headers=rpc_headers,
            json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}, timeout=240,
        )
        return parse_rpc_response(response, request_id)

    sent = rpc("SendMessage", {
        "message": {"messageId": str(uuid.uuid4()), "role": "ROLE_USER", "parts": [{
            "text": "Reply exactly HOSTED_AGENT_OK",
        }]},
        "configuration": {"returnImmediately": False},
    }, hosted_alias)
    if not isinstance(sent.get("task"), dict):
        raise RuntimeError("Expected a native A2A 1.0 SendMessage task envelope, not a translated legacy response.")
    task = sent["task"]
    task_id = task["id"]
    deadline = time.monotonic() + 180
    while task.get("status", {}).get("state") in {"submitted", "working", "TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}:
        if time.monotonic() >= deadline:
            raise TimeoutError("A2A task did not complete.")
        time.sleep(3)
        result = rpc("GetTask", {"id": task_id}, hosted_alias)
        task = result.get("task", result)
    if task.get("status", {}).get("state") not in {"completed", "TASK_STATE_COMPLETED"}:
        raise RuntimeError(f"A2A task failed: {task.get('status')}.")
    texts = [part.get("text") for artifact in task.get("artifacts", []) for part in artifact.get("parts", [])]
    expected_artifact = "HOSTED_AGENT_OK"
    if texts != [expected_artifact]:
        raise RuntimeError(f"A2A returned unexpected artifacts: {texts}.")
    result = rpc("GetTask", {"id": task_id}, hosted_alias)
    saved = result.get("task", result)
    # LiteLLM's send serializer omits text/plain, while native GetTask retains it.
    if saved.get("id") != task_id or canonical_artifacts(saved) != canonical_artifacts(task):
        raise RuntimeError("A2A saved Task differs from the completed Task.")
    checks["a2a"] = {"task_id": task_id, "artifact": expected_artifact, "saved_task_retrieved": True}
    if private_demo:
        submitted = rpc("SendMessage", {
            "message": {"messageId": str(uuid.uuid4()), "role": "ROLE_USER", "parts": [{
                "text": "Reply exactly BACKGROUND_AGENT_OK",
            }]},
            "configuration": {"returnImmediately": True},
        }, hosted_alias)
        background_task = submitted.get("task")
        if not isinstance(background_task, dict) or not background_task.get("id"):
            raise RuntimeError("Background A2A submission returned no native Task")
        background_id = background_task["id"]
        initial_state = background_task.get("status", {}).get("state")
        deadline = time.monotonic() + 180
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("Background A2A Task did not complete")
            polled = rpc("GetTask", {"id": background_id}, hosted_alias)
            background_task = polled.get("task", polled)
            if background_task.get("id") != background_id:
                raise RuntimeError("Background GetTask returned the wrong Task")
            state = background_task.get("status", {}).get("state")
            if state in {"completed", "TASK_STATE_COMPLETED"}:
                if completed_a2a_text({"task": background_task}) != "BACKGROUND_AGENT_OK":
                    raise RuntimeError("Background A2A returned an unexpected artifact")
                break
            if state not in {"submitted", "working", "TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}:
                raise RuntimeError(f"Background A2A failed: {state}")
            time.sleep(3)
        checks["a2a_background"] = {
            "task_id": background_id, "return_immediately": True,
            "initial_state": initial_state, "final_state": state,
            "artifact": "BACKGROUND_AGENT_OK", "polled": True,
        }
    if declarative:
        result = rpc("SendMessage", {
            "message": {"messageId": str(uuid.uuid4()), "role": "ROLE_USER",
                        "parts": [{"text": "Reply exactly DECLARATIVE_OK"}]},
            "configuration": {"returnImmediately": False},
        }, "declarative")
        if completed_a2a_text(result).strip() != "DECLARATIVE_OK":
            raise RuntimeError("Declarative A2A agent omitted its acceptance marker.")
        checks["declarative_a2a"] = {"marker": "DECLARATIVE_OK", "native_send": True}
    if private_demo:
        results = {}
        for alias, prompt, marker in [
            ("prompt-agent-responses", "Reply exactly PRIVATE_RESPONSES_OK", "PRIVATE_RESPONSES_OK"),
            ("hosted-agent-responses", "Reply exactly PRIVATE_RESPONSES_OK", "PRIVATE_RESPONSES_OK"),
            ("hosted-agent-responses", "/mcp-read gateway-check.txt", "MCP_WORKLOAD_IDENTITY_OK"),
            ("hosted-agent-responses", "/network-check", "INTERNET_EGRESS_BLOCKED"),
        ]:
            url = base + "/v1/responses"
            print(f"POST {url} [Responses: {alias}]", file=sys.stderr, flush=True)
            response = requests.post(
                url, headers=headers, json={"model": alias, "input": prompt}, timeout=240,
            )
            response.raise_for_status()
            payload = response.json()
            text = completed_response_text(payload)
            if text.strip() != marker:
                raise RuntimeError(f"{alias} did not return its required private-demo marker.")
            suffix = "-mcp" if prompt.startswith("/mcp-read") else "-egress" if prompt == "/network-check" else ""
            results[alias + suffix] = marker
        checks["agent_responses"] = results
        if validate_permissions:
            for alias in ("prompt-agent-responses", "hosted-agent-responses"):
                denied = requests.post(
                    base + "/v1/responses", headers=restricted_headers,
                    json={"model": alias, "input": "This request must be denied."}, timeout=90,
                )
                if denied.status_code not in {401, 403}:
                    raise RuntimeError("The models-only key unexpectedly reached an agent Responses alias.")
            denied = requests.post(
                base + "/v1/responses", headers=headers,
                json={
                    "model": "hosted-agent-responses", "input": "This request must be denied.",
                    "agent_reference": {"type": "agent_reference", "name": "another-agent"},
                }, timeout=90,
            )
            if denied.status_code != 403:
                raise RuntimeError("A caller-supplied agent reference was not denied by the registered-alias policy.")
            checks["agent_responses_key_and_override_denials"] = True
    return {"verified_at": datetime.now(timezone.utc).isoformat(), "scenario": scenario,
            "endpoint": base, "checks": checks}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preparation = commands.add_parser("prepare")
    preparation.add_argument("--enable-agent-responses", action="store_true")
    preparation.add_argument("--agent-responses-project-endpoint")
    preparation.add_argument("--private-demo", action="store_true")
    preparation.add_argument("--mcp-authorization", help="Approved namespace-scoped hosted MCP caller policy JSON.")
    preparation.add_argument("--deployment", required=True)
    preparation.add_argument("--a2a-endpoint", required=True)
    preparation.add_argument("--model-endpoint", required=True)
    preparation.add_argument("--model-deployment", default="gpt-4o-mini")
    preparation.add_argument("--image-tag", required=True)
    preparation.add_argument("--dns-label", required=True)
    preparation.add_argument("--exposure", choices=["public", "private"], default="public")
    preparation.add_argument("--output", required=True)
    preparation.add_argument("--scenario", choices=list(NAMESPACES), default="shared-service")
    preparation.add_argument("--scenario-contract")
    preparation.add_argument("--identity")
    preparation.add_argument("--declarative-endpoint")
    verification = commands.add_parser("verify")
    verification.add_argument("--deployment", required=True)
    verification.add_argument("--output", required=True)
    verification.add_argument("--use-azd", action="store_true", help="Use an existing azd login instead of Azure CLI.")
    verification.add_argument("--scenario", choices=list(NAMESPACES), default="shared-service")
    verification.add_argument("--identity")
    verification.add_argument("--permissions", action="store_true")
    verification.add_argument("--declarative", action="store_true")
    verification.add_argument("--user-token-cache", help="OS-encrypted cache created by entra_setup login.")
    verification.add_argument("--kubeconfig", help="Operator kubeconfig; defaults beside the deployment receipt.")
    verification.add_argument("--reject-key-from", choices=list(NAMESPACES))
    verification.add_argument("--private-demo", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args)
    else:
        attempt = {"scenario": args.scenario, "started_at": datetime.now(timezone.utc).isoformat()}
        save_receipt(args.output, {**attempt, "status": "running"})
        passed = False
        previous_kubeconfig = os.environ.get("KUBECONFIG")
        try:
            kubeconfig = Path(args.kubeconfig) if args.kubeconfig else Path(args.deployment).with_name("kubeconfig")
            if not kubeconfig.is_file():
                raise ValueError("Acceptance needs the operator kubeconfig; pass --kubeconfig or deploy workloads first.")
            os.environ["KUBECONFIG"] = str(kubeconfig.resolve())
            output_file = Path(args.deployment).with_name("terraform-outputs.json")
            profiles = load_json(output_file).get("identity_scenarios", {}).get("value", {}) if output_file.exists() else {}
            expected_principal = profiles.get(args.scenario, {}).get("principal_id")
            result = verify(load_json(args.deployment), use_azd=args.use_azd, scenario=args.scenario,
                            identity_receipt=load_json(args.identity) if args.identity else None,
                            validate_permissions=args.permissions, declarative=args.declarative,
                            user_token_cache=args.user_token_cache, expected_backend_principal_id=expected_principal,
                            reject_key_from=args.reject_key_from, private_demo=args.private_demo)
            result["status"] = "passed"
            save_receipt(args.output, result)
            passed = True
            print(json.dumps(result, indent=2))
        finally:
            if previous_kubeconfig is None:
                os.environ.pop("KUBECONFIG", None)
            else:
                os.environ["KUBECONFIG"] = previous_kubeconfig
            if not passed:
                save_receipt(args.output, {**attempt, "status": "failed",
                                          "message": "Acceptance did not complete; see the explicit command error."})


if __name__ == "__main__":
    main()
