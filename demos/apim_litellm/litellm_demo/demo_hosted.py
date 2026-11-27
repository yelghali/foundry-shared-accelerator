"""Publish the standalone image-hosted dual-protocol agent to an existing project."""

import argparse
import json
import time
from urllib.parse import urlsplit

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    AgentCard, AgentCardSkill, AgentEndpointConfig, A2AProtocolConfiguration,
    ContainerConfiguration, FixedRatioVersionSelectionRule, HostedAgentDefinition,
    ProtocolConfiguration, ProtocolVersionRecord, ResponsesProtocolConfiguration, VersionSelector,
)
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import AzureCliCredential, AzureDeveloperCliCredential

from litellm_demo.control import ControlPlane, save_receipt
from .private_foundry import sdk_transport


def deploy(args):
    identity = (AzureDeveloperCliCredential if args.use_azd else AzureCliCredential)(
        tenant_id=args.tenant_id, process_timeout=180,
    )
    options = {}
    if args.private_proxy_namespace:
        options["transport"] = sdk_transport(args.project_endpoint, args.private_proxy_namespace)
    definition = HostedAgentDefinition(
        cpu="0.5", memory="1Gi",
        container_configuration=ContainerConfiguration(image=args.image),
        protocol_versions=[
            ProtocolVersionRecord(protocol="a2a", version="1.0.0"),
            ProtocolVersionRecord(protocol="responses", version="2.0.0"),
        ],
        environment_variables={
            "MODEL_ENDPOINT": args.model_endpoint.rstrip("/"),
            "MODEL_DEPLOYMENT": args.model_deployment,
            "PRIVATE_MCP_URL": args.mcp_url or "",
            "MCP_TOKEN_SCOPE": args.mcp_scope or "",
            "OTEL_INSTRUMENTATION_A2A_SDK_ENABLED": "false",
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "false",
        },
    )
    name = "gateway-hosted-dual"
    with AIProjectClient(endpoint=args.project_endpoint, credential=identity, allow_preview=True, **options) as client:
        try:
            versions = list(client.agents.list_versions(agent_name=name))
        except ResourceNotFoundError:
            versions = []
        current = next((version for version in versions if version.definition.as_dict() == definition.as_dict()), None)
        if current is None:
            current = client.agents.create_version(agent_name=name, definition=definition)
        deadline = time.monotonic() + 1200
        while str(current.status).lower() in {"creating", "agentversionstatus.creating"}:
            if time.monotonic() > deadline:
                raise TimeoutError("Hosted version is still provisioning; inspect before retrying.")
            time.sleep(10)
            current = client.agents.get_version(agent_name=name, agent_version=current.version)
            print(f"{name}:{current.version} status: {current.status}", flush=True)
        if str(current.status).lower() in {"failed", "agentversionstatus.failed"}:
            raise RuntimeError("Hosted version failed; no success receipt written.")
        client.agents.update_details(
            agent_name=name,
            agent_card=AgentCard(
                version="1.0.0", description="Model-backed private demo agent with A2A and Responses",
                skills=[AgentCardSkill(id="chat", name="Chat", description="Model and explicit MCP read", tags=["chat"])],
            ),
            agent_endpoint=AgentEndpointConfig(
                version_selector=VersionSelector(version_selection_rules=[
                    FixedRatioVersionSelectionRule(agent_version=current.version, traffic_percentage=100),
                ]),
                protocol_configuration=ProtocolConfiguration(
                    a2a=A2AProtocolConfiguration(), responses=ResponsesProtocolConfiguration(),
                ),
            ),
        )
        agent = client.agents.get(agent_name=name)
        runtime = agent.instance_identity
        if args.grant_model_access:
            if runtime is None:
                raise RuntimeError("The hosted runtime identity is absent; model RBAC was not granted.")
            ControlPlane(identity).grant(
                runtime.principal_id, args.model_account_id,
                "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd", principal_type="ServicePrincipal",
            )
        return {
            "name": name, "version": current.version, "project_endpoint": args.project_endpoint,
            "a2a_endpoint": f"{args.project_endpoint}/agents/{name}/endpoint/protocols/a2a",
            "responses_endpoint": f"{args.project_endpoint}/agents/{name}/endpoint/protocols/openai/responses",
            "runtime_principal_id": runtime.principal_id if runtime else None,
            "runtime_client_id": runtime.client_id if runtime else None,
            "invocation_tested": False,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("project-endpoint", "tenant-id", "image", "model-endpoint", "output"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--model-deployment", default="gpt-4o-mini")
    parser.add_argument("--mcp-url")
    parser.add_argument("--mcp-scope")
    parser.add_argument("--model-account-id")
    parser.add_argument("--grant-model-access", action="store_true")
    parser.add_argument("--use-azd", action="store_true")
    parser.add_argument("--private-proxy-namespace", choices=["gateway", "gateway-app-alpha", "gateway-user-obo"])
    parser.add_argument("--apply", action="store_true", help="Without this flag, print the intended deployment only.")
    args = parser.parse_args()
    if args.grant_model_access and not args.model_account_id:
        raise ValueError("--grant-model-access requires the explicit --model-account-id")
    endpoint = urlsplit(args.project_endpoint)
    if (endpoint.scheme != "https" or not endpoint.hostname
            or not endpoint.hostname.endswith(".services.ai.azure.com")
            or not endpoint.path.startswith("/api/projects/")
            or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
        raise ValueError("Provide the existing Foundry project HTTPS endpoint without credentials or query.")
    if not args.apply:
        print(json.dumps({
            "mode": "plan", "project_endpoint": args.project_endpoint, "image": args.image,
            "agent_name": "gateway-hosted-dual", "protocols": ["a2a", "responses"],
            "grant_model_access": args.grant_model_access, "writes_performed": False,
        }, indent=2))
        return
    result = deploy(args)
    save_receipt(args.output, result)
    print(f"Published {result['name']}:{result['version']}; invocation still requires acceptance.")


if __name__ == "__main__":
    main()
