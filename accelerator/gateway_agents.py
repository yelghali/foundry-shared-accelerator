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
    ProtocolConfiguration,
    ResponsesProtocolConfiguration,
    VersionSelector,
)
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import AzureCliCredential, AzureDeveloperCliCredential

from .control import ControlPlane, save_receipt


def deploy(project_endpoint: str, project_id: str, tenant_id: str, model: str, *, use_azd: bool) -> dict:
    identity = (AzureDeveloperCliCredential if use_azd else AzureCliCredential)(
        tenant_id=tenant_id, process_timeout=180,
    )
    name = "gateway-declarative"
    definition = PromptAgentDefinition(
        model=model,
        instructions="For gateway acceptance tests return exactly the requested marker, without extra text. Do not call tools.",
    )
    with AIProjectClient(endpoint=project_endpoint, credential=identity, allow_preview=True) as client:
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
    args = parser.parse_args()
    value = deploy(args.project_endpoint, args.project_id, args.tenant_id, args.model, use_azd=args.use_azd)
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
