"""Configure dedicated gateway assets through its public ARM API."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import requests
import re
import uuid
from typing import Any
from urllib.parse import urlsplit

from .control import (
    AGENT_CONSUMER, FOUNDRY_API, FOUNDRY_USER, GATEWAY_AUDIENCE, GATEWAY_MODELS_USER, GATEWAY_TOOLS_USER,
    AzureRequestError, ControlPlane, credential, load_json, save_receipt,
)
from .agent import validate_project


def configure_telemetry(control: ControlPlane, deployment: dict[str, Any], gateway: dict[str, Any]) -> str:
    otlp = deployment.get("otlp")
    if not isinstance(otlp, dict):
        raise ValueError("Apply the native-monitor Terraform module before configuring telemetry.")
    if gateway["id"].lower() != deployment["gateway"]["id"].lower():
        control.grant(gateway["principal_id"], otlp["rule_id"], "3913510d-42f4-4e42-8a64-420c390055eb")
    exporter_id = gateway["id"] + "/workspaces/default/telemetryExporters/native-otlp"
    control.put(exporter_id, {"properties": {
        "kind": "OpenTelemetry",
        "payloadCapture": False,
        "applicationInsights": {"resourceId": deployment["application_insights_id"]},
        "openTelemetry": {
            "metricsEndpoint": otlp["metrics_endpoint"],
            "logsEndpoint": otlp["logs_endpoint"],
            "tracesEndpoint": otlp["traces_endpoint"],
            "managedIdentity": {"resource": "https://monitor.azure.com"},
        },
        "telemetry": {"attributes": [
            {"source": {"kind": "claims", "value": claim}, "signals": ["metrics", "logs", "traces"]}
            for claim in ("tid", "oid")
        ]},
    }})
    actual = control.request("GET", exporter_id)["properties"]
    if actual.get("kind") != "OpenTelemetry" or actual.get("openTelemetry", {}).get("metricsEndpoint") != otlp["metrics_endpoint"]:
        raise RuntimeError("Gateway did not retain the required native OTLP exporter configuration.")
    return exporter_id


def configure_models(control: ControlPlane, deployment: dict[str, Any]) -> dict[str, Any]:
    workspace = deployment["gateway"]["id"] + "/workspaces/default"
    backends: list[dict[str, Any]] = []
    for region, account in sorted(deployment["models"].items()):
        provider_id = f"{workspace}/modelProviders/{region}"
        control.put(provider_id, {"properties": {
            "kind": "Foundry",
            "displayName": f"Platform models - {region}",
            "foundry": {
                "endpoint": account["model_endpoint"],
                "resourceIds": [account["id"]],
                "authentication": {
                    "kind": "ManagedIdentity",
                    "managedIdentity": {"resource": "https://cognitiveservices.azure.com/"},
                },
            },
        }})
        model_id = f"{provider_id}/models/{deployment['model_name']}"
        control.put(model_id, {"properties": {
            "displayName": f"{deployment['model_name']}-{region}",
            "deployment": {
                "resourceId": account["deployment_id"],
                "modelName": deployment["model_name"],
                "modelVersion": deployment["model_version"],
            },
            "supportedEndpoints": ["/openai/v1/chat/completions", "/openai/v1/responses"],
        }})
        backends.append({"resourceId": model_id, "priority": 0, "weight": 1})
    alias_id = workspace + "/aliases/approved-chat"
    alias_body = {"properties": {"aliasName": "approved-chat", "backendModels": backends, "selectionOrder": ["routing"]}}
    mode = "load_balance" if len(backends) > 1 else "single_backend"
    fallback_reason = None
    try:
        control.put(alias_id, alias_body)
    except AzureRequestError as error:
        unsupported = any(word in str(error).lower() for word in ("not supported", "single backend", "one backend", "at most one"))
        if len(backends) <= 1 or error.status not in {400, 422} or not unsupported:
            raise
        primary = next(item for item in backends if "/modelProviders/primary/" in item["resourceId"])
        alias_body["properties"]["backendModels"] = [primary]
        control.put(alias_id, alias_body)
        mode, fallback_reason = "single_backend_fallback", str(error)
        print("Native backend pooling was rejected; configured the explicitly allowed primary-only alternative.", flush=True)
    actual = control.request("GET", alias_id)["properties"]
    expected = {item["resourceId"].lower() for item in alias_body["properties"]["backendModels"]}
    observed = {item["resourceId"].lower() for item in actual.get("backendModels", [])}
    if observed != expected:
        raise RuntimeError("Gateway alias backend membership differs from the requested routing configuration.")
    control.grant(deployment["runner"]["principal_id"], alias_id, GATEWAY_MODELS_USER)
    control.grant(
        deployment["deployer_principal_id"], alias_id, GATEWAY_MODELS_USER,
        principal_type=deployment.get("deployer_principal_type", "User"),
    )
    exporter_id = configure_telemetry(control, deployment, deployment["gateway"])
    return {
        "gateway": deployment["gateway"],
        "alias": "approved-chat",
        "alias_id": alias_id,
        "routing_mode": mode,
        "backends": alias_body["properties"]["backendModels"],
        "fallback_reason": fallback_reason,
        "telemetry_exporter_id": exporter_id,
        "payload_capture": False,
    }


def prepare_team(control: ControlPlane, deployment: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    validate_project(project)
    if not project["id"].lower().startswith(deployment["agent_foundry"]["id"].lower() + "/projects/"):
        raise ValueError("Project is outside this accelerator's shared Foundry account.")
    alias_id = deployment["gateway"]["id"] + "/workspaces/default/aliases/approved-chat"
    terraform_owned = project.get("model_connection_id") is not None
    if not terraform_owned:
        control.grant(project["principal_id"], alias_id, GATEWAY_MODELS_USER)
    if project.get("sample_caller"):
        control.grant(project["sample_caller"]["principal_id"], alias_id, GATEWAY_MODELS_USER)
    connection_name = f"platform-models-{project['name']}"
    connection_id = project["id"] + f"/connections/{connection_name}"
    connection = {"properties": {
        "category": "ApiManagement",
        "authType": "ProjectManagedIdentity",
        "audience": GATEWAY_AUDIENCE,
        "target": deployment["gateway"]["endpoint"] + "/default/models/openai/v1",
        "isSharedToAll": False,
        "credentials": {},
        "metadata": {
            "audience": GATEWAY_AUDIENCE,
            "models": json.dumps([{"name": "approved-chat", "properties": {"model": {
                "name": "approved-chat", "format": "OpenAI", "version": deployment["model_version"],
            }}}]),
            "deploymentInPath": "false",
            "inferenceAPIVersion": "v1",
            "authConfig": "{}",
            "customHeaders": json.dumps({"X-Chargeback-Project-Key": project["chargeback_project_key"]}),
        },
    }}
    if terraform_owned:
        if project["model_connection_id"].lower() != connection_id.lower():
            raise ValueError("Terraform model connection ID does not match this project.")
        existing = control.request("GET", connection_id, version=FOUNDRY_API)["properties"]
        for key in ("category", "authType", "audience", "target"):
            if existing.get(key) != connection["properties"][key]:
                raise ValueError(f"Terraform model connection has an unexpected {key}.")
        if json.loads(existing.get("metadata", {}).get("models", "null")) != json.loads(connection["properties"]["metadata"]["models"]):
            raise ValueError("Terraform model connection has unexpected models.")
    else:
        control.put(connection_id, connection, version=FOUNDRY_API)
    gateway = project.get("gateway", deployment["gateway"])
    return {
        **project,
        "model_reference": f"{connection_name}/approved-chat",
        "model_connection_id": connection_id,
        "model_alias_id": alias_id,
        "gateway": gateway,
        "authentication": "entra_frontend_managed_identity_backend",
        "gateway_audience": GATEWAY_AUDIENCE,
        "deployer_principal_id": deployment["deployer_principal_id"],
        "deployer_principal_type": deployment.get("deployer_principal_type", "User"),
        "runner_principal_id": deployment["runner"]["principal_id"],
    }


def agent_openapi(project: dict[str, Any], agent: dict[str, Any]) -> dict[str, Any]:
    endpoint = urlsplit(project["endpoint"])
    path = endpoint.path + f"/agents/{agent['name']}/endpoint/protocols/openai/responses"
    return {
        "openapi": "3.0.3",
        "info": {"title": f"{project['name']} {agent['name']} approved agent", "version": "1.0.0"},
        "servers": [{"url": f"{endpoint.scheme}://{endpoint.netloc}"}],
        "paths": {path: {"post": {
            "operationId": "ask_agent",
            "summary": "Ask the approved team agent.",
            "parameters": [{"name": "api-version", "in": "query", "required": True,
                            "schema": {"type": "string", "enum": ["v1"], "default": "v1"}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["input"],
                "properties": {
                    "input": {"type": "string", "description": "The user's question."},
                    "store": {"type": "boolean", "default": False, "enum": [False]},
                    "stream": {"type": "boolean", "default": False, "enum": [False]},
                },
            }}}},
            "responses": {"200": {"description": "Agent response", "content": {"application/json": {
                "schema": {"type": "object", "additionalProperties": True},
            }}}},
        }}},
    }


def publish_tool_server(
    control: ControlPlane, deployment: dict[str, Any], project: dict[str, Any],
    server_name: str, properties: dict[str, Any],
) -> dict[str, str]:
    gateway = project["gateway"]
    server_id = gateway["id"] + f"/workspaces/default/toolServers/{server_name}"
    control.put(server_id, {"properties": {
        **properties, "accessState": "active", "subscriptionRequired": False,
    }})
    if project.get("sample_caller"):
        control.grant(project["sample_caller"]["principal_id"], server_id, GATEWAY_TOOLS_USER)
    control.grant(project["runner_principal_id"], server_id, GATEWAY_TOOLS_USER)
    control.grant(
        project["deployer_principal_id"], server_id, GATEWAY_TOOLS_USER,
        principal_type=project.get("deployer_principal_type", "User"),
    )
    exporter_id = configure_telemetry(control, deployment, gateway)
    endpoint = gateway["endpoint"] + f"/default/toolservers/{server_name}/mcp"
    anonymous = requests.post(
        endpoint, headers={"Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, timeout=90,
    )
    if anonymous.status_code not in {401, 403}:
        control.request(
            "PATCH", server_id, {"properties": {"accessState": "blocked"}},
            extra_headers={"If-Match": "*"},
        )
        raise RuntimeError("Native MCP did not reject an anonymous caller; the server has been blocked.")
    return {"id": server_id, "endpoint": endpoint, "exporter_id": exporter_id}


def agent_registration_names(project: dict[str, Any], agent: dict[str, Any]) -> tuple[str, str, str]:
    previous = project.get("agents", {}).get(agent["name"])
    if previous:
        server_name = previous["tool_server_id"].rsplit("/", 1)[1]
    elif project.get("agent", {}).get("name") == agent["name"] and project.get("agent_tool_server_id"):
        server_name = project["agent_tool_server_id"].rsplit("/", 1)[1]
    else:
        suffix = hashlib.sha256((project["id"] + "/" + agent["name"]).encode()).hexdigest()[:6]
        server_name = f"{project['name'][:20]}-{agent['name'][:20]}-{suffix}"
    existing_registration = previous is not None or (
        project.get("agent", {}).get("name") == agent["name"] and project.get("agent_tool_server_id")
    )
    endpoint_id = (previous or {}).get("endpoint_id", "agent") if existing_registration else server_name
    namespace = (previous or {}).get("namespace", project["name"]) if existing_registration else server_name
    return server_name, endpoint_id, namespace


def register_agent(
    control: ControlPlane, project: dict[str, Any], agent: dict[str, Any], *,
    deployment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_project(project)
    if agent["project_id"].lower() != project["id"].lower():
        raise ValueError("Refusing to register an agent from a different project.")
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,39}", agent["name"]):
        raise ValueError("Agent name must match the validated deployment slug contract.")
    if deployment is None:
        raise ValueError("Deployment telemetry metadata is required for registration.")
    gateway = project["gateway"]
    scope = project["id"] + f"/agents/{agent['name']}"
    control.grant(gateway["principal_id"], scope, AGENT_CONSUMER)
    specification = base64.b64encode(json.dumps(agent_openapi(project, agent)).encode()).decode()
    server_name, endpoint_id, namespace = agent_registration_names(project, agent)
    server = publish_tool_server(control, deployment, project, server_name, {
        "name": f"{project['name']} {agent['name']} agent",
        "type": "openApi",
        "endpoints": [{
            "id": endpoint_id,
            "kind": "openApi",
            "namespace": namespace,
            "openApi": {"specSource": {"type": "inline", "contentBase64": specification}},
            "credentials": {"type": "managedIdentity", "managedIdentity": {"resource": "https://ai.azure.com"}},
            "tools": ["ask_agent"],
            "required": True,
        }],
    })
    binding = {"agent": agent, "tool_server_id": server["id"], "mcp_endpoint": server["endpoint"],
               "endpoint_id": endpoint_id, "namespace": namespace, "tool_name": namespace + "_ask_agent"}
    default = {} if project.get("agent") and project["agent"]["name"] != agent["name"] else {
        "agent": agent, "agent_tool_server_id": server["id"], "agent_mcp_endpoint": server["endpoint"],
        "agent_tool_name": namespace + "_ask_agent",
    }
    return {
        **project,
        **default,
        "agents": {**project.get("agents", {}), agent["name"]: binding},
        "agent_exposure": "native_gateway_openapi_to_mcp",
        "gateway_registration_status": "configured_pending_runtime_verification",
        "telemetry_exporter_id": server["exporter_id"],
    }


def grant_agent_consumer(
    control: ControlPlane, project: dict[str, Any], agent_name: str, principal_id: str,
) -> dict[str, str]:
    validate_project(project)
    try:
        principal_id = str(uuid.UUID(principal_id))
    except ValueError as error:
        raise ValueError("Consumer principal must be an Entra service-principal object ID.") from error
    binding = project.get("agents", {}).get(agent_name)
    if not binding or binding["agent"]["project_id"].lower() != project["id"].lower():
        raise ValueError("Agent is not published for this project.")
    server_id = binding["tool_server_id"]
    expected = project["gateway"]["id"] + "/workspaces/default/toolServers/"
    if not server_id.lower().startswith(expected.lower()) or "/" in server_id[len(expected):]:
        raise ValueError("Agent gateway asset is outside the approved workspace.")
    server = control.request("GET", server_id)["properties"]
    if server.get("accessState") != "active" or server.get("type") != "openApi":
        raise ValueError("The selected agent gateway asset is not active.")
    control.grant(principal_id, server_id, GATEWAY_TOOLS_USER)
    return {"project_id": project["id"], "agent_name": agent_name,
            "consumer_principal_id": principal_id, "tool_server_id": server_id}


def prepare_tools(control: ControlPlane, project: dict[str, Any]) -> dict[str, Any]:
    validate_project(project)
    control.grant(project["principal_id"], project["agent_tool_server_id"], GATEWAY_TOOLS_USER)
    connection_id = project["id"] + f"/connections/agent-tools-{project['name']}"
    control.put(connection_id, {"properties": {
        "category": "RemoteTool", "authType": "ProjectManagedIdentity",
        "target": project["agent_mcp_endpoint"], "audience": GATEWAY_AUDIENCE,
        "isSharedToAll": False, "credentials": {}, "metadata": {"ApiType": "Azure"},
    }}, version=FOUNDRY_API)
    return {**project, "tool_connection_id": connection_id}


def register_toolbox(
    control: ControlPlane, deployment: dict[str, Any], project: dict[str, Any], toolbox: dict[str, Any],
) -> dict[str, Any]:
    validate_project(project)
    if toolbox["project_id"] != project["id"]:
        raise ValueError("Toolbox belongs to a different project.")
    control.grant(project["gateway"]["principal_id"], project["id"], FOUNDRY_USER)
    prior = project.get("toolbox_tool_server_id")
    suffix = hashlib.sha256(project["id"].encode()).hexdigest()[:6]
    server_name = prior.rsplit("/", 1)[1] if prior else f"{project['name'][:35]}-toolbox-{suffix}"
    endpoint_id = project.get("toolbox_endpoint_id", "toolbox") if prior else server_name
    namespace = project.get("toolbox_namespace", "toolbox") if prior else server_name
    server = publish_tool_server(control, deployment, project, server_name, {
        "name": f"{project['name']} curated toolbox", "type": "mcp",
        "endpoints": [{
            "id": endpoint_id, "kind": "mcp", "namespace": namespace,
            "mcp": {"url": toolbox["endpoint"], "transport": "streamableHttp"},
            "credentials": {"type": "managedIdentity", "managedIdentity": {"resource": "https://ai.azure.com"}},
            "required": True,
        }],
    })
    control.grant(project["principal_id"], server["id"], GATEWAY_TOOLS_USER)
    return {
        **project, "toolbox": toolbox, "toolbox_mcp_endpoint": server["endpoint"],
        "toolbox_tool_server_id": server["id"],
        "toolbox_endpoint_id": endpoint_id, "toolbox_namespace": namespace,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["configure-models", "prepare-team", "register-agent", "grant-agent-consumer", "prepare-tools", "register-toolbox"])
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--project")
    parser.add_argument("--agent")
    parser.add_argument("--agent-name")
    parser.add_argument("--consumer-principal-id")
    parser.add_argument("--toolbox")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    control = ControlPlane(credential())
    deployment = load_json(args.deployment)
    if args.command == "configure-models":
        receipt = configure_models(control, deployment)
    else:
        if not args.project:
            parser.error("--project is required for team operations.")
        project = load_json(args.project)
        validate_project(project)
        if not project["id"].lower().startswith(deployment["agent_foundry"]["id"].lower() + "/projects/"):
            parser.error("Project belongs to a different shared Foundry deployment.")
        if args.command == "prepare-team":
            receipt = prepare_team(control, deployment, project)
        elif args.command == "grant-agent-consumer":
            if not args.agent_name or not args.consumer_principal_id:
                parser.error("--agent-name and --consumer-principal-id are required for a consumer grant.")
            receipt = grant_agent_consumer(control, project, args.agent_name, args.consumer_principal_id)
        elif args.command == "prepare-tools":
            receipt = prepare_tools(control, project)
        elif args.command == "register-toolbox":
            if not args.toolbox:
                parser.error("--toolbox is required for toolbox registration.")
            receipt = register_toolbox(control, deployment, project, load_json(args.toolbox))
        else:
            if not args.agent:
                parser.error("--agent is required to register a deployed agent.")
            receipt = register_agent(control, project, load_json(args.agent), deployment=deployment)
    save_receipt(args.output, receipt)
    print(f"{args.command} completed; nonsecret receipt written to {args.output}.")


if __name__ == "__main__":
    main()
