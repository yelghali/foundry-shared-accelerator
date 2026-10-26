"""Deploy a prompt-agent version into an explicitly validated project."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from typing import Any
from urllib.parse import urlsplit

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    AgentEndpointConfig,
    FixedRatioVersionSelectionRule,
    PromptAgentDefinition,
    ProtocolConfiguration,
    ResponsesProtocolConfiguration,
    VersionSelector,
)
from azure.core.credentials import TokenCredential
from azure.core.exceptions import ResourceNotFoundError

from .control import credential, load_json, save_receipt


def validate_project(project: dict[str, Any]) -> None:
    match = re.fullmatch(
        r"/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft\.CognitiveServices/accounts/([^/]+)/projects/([^/]+)",
        project["id"], flags=re.IGNORECASE,
    )
    if not match:
        raise ValueError("The target must be a Foundry project ARM resource ID.")
    endpoint = urlsplit(project["endpoint"])
    if (
        endpoint.scheme != "https"
        or endpoint.hostname != f"{match[1]}.services.ai.azure.com".lower()
        or endpoint.path != f"/api/projects/{match[2]}"
        or endpoint.query
        or endpoint.fragment
        or endpoint.username
        or project["name"] != match[2]
    ):
        raise ValueError("Project resource ID, project name and endpoint do not match.")


def deploy_agent(
    project: dict[str, Any],
    token_credential: TokenCredential,
    *,
    name: str,
    mode: str,
    expected_agent_id: str | None = None,
    template: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_project(project)
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,39}", name):
        raise ValueError("Agent name must be a 3-40 character lowercase slug.")
    if mode not in {"create", "new-version", "ensure"}:
        raise ValueError("Deploy mode must be create, new-version or ensure.")
    if mode == "create" and expected_agent_id:
        raise ValueError("Creating an agent cannot target an existing agent ID.")
    instructions = (
        f"You are the assistant for project {project['name']}. "
        "Follow the user's requested output format exactly. "
        "For smoke tests, return only the requested marker. Do not invent tools or data."
    )
    if template is not None:
        if set(template) - {"schema_version", "kind", "instructions"}:
            raise ValueError("Prompt template contains unsupported fields.")
        if template.get("schema_version") != 1 or template.get("kind") != "prompt":
            raise ValueError("Expected a version-1 prompt template.")
        value = template.get("instructions")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Prompt template instructions must be nonempty.")
        instructions = value.replace("${PROJECT_NAME}", project["name"])
    definition_data = {"model": project["model_reference"], "instructions": instructions}
    fingerprint = hashlib.sha256(json.dumps(definition_data, sort_keys=True).encode()).hexdigest()
    with AIProjectClient(endpoint=project["endpoint"], credential=token_credential, allow_preview=True) as client:
        try:
            existing = client.agents.get(agent_name=name)
        except ResourceNotFoundError:
            existing = None
        if mode == "new-version" and existing is None:
            raise ValueError("The selected agent does not exist; refusing implicit creation.")
        if mode == "create" and existing is not None:
            raise ValueError("Agent already exists; use new-version or explicit ensure.")
        if expected_agent_id and (existing is None or existing.id != expected_agent_id):
            raise ValueError("Existing agent ID does not match the selected project/name.")
        reused = False
        if mode == "ensure" and existing is not None:
            versions = list(client.agents.list_versions(agent_name=name))
            matching = [item for item in versions if item.metadata and item.metadata.get("definition_sha256") == fingerprint]
            if not matching:
                raise ValueError("Agent exists with a different definition; use explicit new-version.")
            version = matching[0]
            reused = True
        else:
            version = client.agents.create_version(
                agent_name=name,
                definition=PromptAgentDefinition(**definition_data),
                metadata={"definition_sha256": fingerprint, "project_key": project["chargeback_project_key"]},
            )
        deadline = time.monotonic() + 900
        while str(version.status).lower() in {"creating", "agentversionstatus.creating"}:
            if time.monotonic() > deadline:
                raise TimeoutError("Agent version did not become ready.")
            time.sleep(5)
            version = client.agents.get_version(agent_name=name, agent_version=version.version)
        if str(version.status).lower() in {"failed", "agentversionstatus.failed"}:
            raise RuntimeError("Agent version failed to provision.")
        client.agents.update_details(
            agent_name=name,
            agent_endpoint=AgentEndpointConfig(
                version_selector=VersionSelector(version_selection_rules=[
                    FixedRatioVersionSelectionRule(agent_version=version.version, traffic_percentage=100),
                ]),
                protocol_configuration=ProtocolConfiguration(responses=ResponsesProtocolConfiguration()),
            ),
        )
        actual = client.agents.get(agent_name=name)
        identity = actual.instance_identity
        return {
            "project_id": project["id"],
            "project_endpoint": project["endpoint"],
            "name": actual.name,
            "id": actual.id,
            "version": version.version,
            "reused_unchanged_version": reused,
            "definition_sha256": fingerprint,
            "identity_principal_id": identity.principal_id if identity else None,
            "responses_endpoint": project["endpoint"] + f"/agents/{name}/endpoint/protocols/openai/responses",
            "model_reference": project["model_reference"],
            "chargeback_project_key": project["chargeback_project_key"],
        }


def verify_direct(project: dict[str, Any], agent: dict[str, Any], token_credential: TokenCredential, marker: str) -> dict[str, Any]:
    validate_project(project)
    with AIProjectClient(endpoint=project["endpoint"], credential=token_credential, allow_preview=True) as client:
        response = client.get_openai_client().responses.create(
            input=f"Reply with exactly this marker and nothing else: {marker}",
            store=False,
            max_output_tokens=80,
            extra_body={"agent_reference": {"name": agent["name"], "version": agent["version"], "type": "agent_reference"}},
        )
    if response.output_text.strip() != marker:
        raise RuntimeError("Agent response did not contain the required smoke-test marker.")
    return {
        "passed": True,
        "response_id": response.id,
        "marker_verified": marker,
        "usage": response.usage.model_dump() if response.usage else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--name", default=os.environ.get("ACCEL_AGENT_NAME", "assistant"))
    parser.add_argument("--mode", choices=["create", "new-version", "ensure"], default=os.environ.get("ACCEL_DEPLOY_MODE", "ensure"))
    parser.add_argument("--agent-id", default=os.environ.get("ACCEL_AGENT_ID"))
    parser.add_argument("--template-file", default=os.environ.get("ACCEL_TEMPLATE_FILE"))
    parser.add_argument("--output", required=True)
    parser.add_argument("--managed-identity", action="store_true")
    args = parser.parse_args()
    receipt = deploy_agent(
        load_json(args.project), credential(managed_identity=args.managed_identity),
        name=args.name, mode=args.mode, expected_agent_id=args.agent_id,
        template=load_json(args.template_file) if args.template_file else None,
    )
    save_receipt(args.output, receipt)
    print(f"Agent {receipt['name']} version {receipt['version']} selected; receipt: {args.output}.")


if __name__ == "__main__":
    main()
