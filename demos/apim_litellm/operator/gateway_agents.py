"""Publish the declarative A2A test agent without replacing existing hosted agents."""

from __future__ import annotations

import argparse
import time

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    A2AProtocolConfiguration,
    AgentCard,
    AgentCardSkill,
    AgentEndpointConfig,
    FixedRatioVersionSelectionRule,
    PromptAgentDefinition,
    MCPTool,
    ProtocolConfiguration,
    ResponsesProtocolConfiguration,
    VersionSelector,
)
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import AzureCliCredential, AzureDeveloperCliCredential

from accelerator.control import ControlPlane, save_receipt


def deploy(project_endpoint: str, project_id: str, tenant_id: str, model: str, *, use_azd: bool,
           private_proxy_namespace: str | None = None, mcp_url: str | None = None,
           mcp_audience: str | None = None) -> dict:
    identity = (AzureDeveloperCliCredential if use_azd else AzureCliCredential)(
        tenant_id=tenant_id, process_timeout=180,
    )
    name = "gateway-declarative"
    tools = []
    if mcp_url:
        if not mcp_audience:
            raise ValueError("An explicit MCP audience is required for project managed-identity authentication")
        connection = project_id + "/connections/private-demo-mcp"
        ControlPlane(identity).put(connection, {"properties": {
            "category": "RemoteTool", "authType": "ProjectManagedIdentity",
            "target": mcp_url, "audience": mcp_audience,
            "isSharedToAll": False, "credentials": {}, "metadata": {"ApiType": "Azure"},
        }}, version="2025-10-01-preview")
        tools = [MCPTool(
            server_label="private_blob", server_url=mcp_url, project_connection_id=connection,
            allowed_tools=["whoami", "read_blob"], require_approval="never",
        )]
    definition = PromptAgentDefinition(
        model=model,
        instructions=("Use the approved private MCP tool when the user asks for Blob contents or identity evidence. "
                      "Otherwise return exactly the requested marker without extra text."
                      if tools else
                      "For gateway acceptance tests return exactly the requested marker, without extra text. Do not call tools."),
        **({"tools": tools} if tools else {}),
    )
    options = {}
    if private_proxy_namespace:
        from .private_foundry import sdk_transport
        options["transport"] = sdk_transport(project_endpoint, private_proxy_namespace)
    with AIProjectClient(endpoint=project_endpoint, credential=identity, allow_preview=True, **options) as client:
        try:
            client.agents.get(agent_name=name)
        except ResourceNotFoundError:
            versions = []
        else:
            versions = list(client.agents.list_versions(agent_name=name))
        current = next((version for version in versions if version.definition.as_dict() == definition.as_dict()), None)
        if current is None or current.definition.as_dict() != definition.as_dict():
            current = client.agents.create_version(
                agent_name=name, definition=definition, metadata={"scenario": "apim-litellm-identity"},
            )
        deadline = time.monotonic() + 600
        while str(current.status).lower() in {"creating", "agentversionstatus.creating"}:
            if time.monotonic() >= deadline:
                raise TimeoutError("Declarative agent did not activate.")
            time.sleep(5)
            current = client.agents.get_version(agent_name=name, agent_version=current.version)
        if str(current.status).lower() in {"failed", "agentversionstatus.failed"}:
            raise RuntimeError("Declarative agent provisioning failed.")
        client.agents.update_details(
            agent_name=name,
            agent_card=AgentCard(
                version="1.0.0",
                description="Declarative agent for native A2A gateway acceptance.",
                skills=[AgentCardSkill(
                    id="declarative", name="Declarative marker",
                    description="Return the explicitly requested acceptance marker.",
                    tags=["acceptance"],
                )],
            ),
            agent_endpoint=AgentEndpointConfig(
                version_selector=VersionSelector(version_selection_rules=[
                    FixedRatioVersionSelectionRule(agent_version=current.version, traffic_percentage=100),
                ]),
                protocol_configuration=ProtocolConfiguration(
                    responses=ResponsesProtocolConfiguration(), a2a=A2AProtocolConfiguration(),
                ),
            ),
        )
    return {
        "name": name, "version": current.version, "project_id": project_id,
        "project_endpoint": project_endpoint,
        "a2a_endpoint": f"{project_endpoint}/agents/{name}/endpoint/protocols/a2a",
        "authentication": "Entra; service or delegated according to selected gateway scenario",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-endpoint", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--use-azd", action="store_true")
    parser.add_argument("--user-object-id", action="append", default=[])
    parser.add_argument("--output", required=True)
    parser.add_argument("--private-proxy-namespace", choices=["gateway", "gateway-app-alpha", "gateway-user-obo"])
    parser.add_argument("--mcp-url")
    parser.add_argument("--mcp-audience")
    args = parser.parse_args()
    value = deploy(args.project_endpoint, args.project_id, args.tenant_id, args.model, use_azd=args.use_azd,
                   private_proxy_namespace=args.private_proxy_namespace,
                   mcp_url=args.mcp_url, mcp_audience=args.mcp_audience)
    if args.user_object_id:
        identity = (AzureDeveloperCliCredential if args.use_azd else AzureCliCredential)(
            tenant_id=args.tenant_id, process_timeout=180,
        )
        control = ControlPlane(identity)
        for user in args.user_object_id:
            control.grant(
                user, args.project_id, "eed3b665-ab3a-47b6-8f48-c9382fb1dad6", principal_type="User",
            )
    save_receipt(args.output, value)
    print(f"Declarative A2A agent {value['name']}:{value['version']} is published.")


if __name__ == "__main__":
    main()
