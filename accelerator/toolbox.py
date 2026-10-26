"""Version a project-owned curated toolbox using its managed-identity connection."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import MCPToolboxTool
from azure.core.credentials import TokenCredential
from azure.core.exceptions import ResourceNotFoundError

from .agent import validate_project


def deploy_toolbox(project: dict[str, Any], token_credential: TokenCredential) -> dict[str, Any]:
    validate_project(project)
    definition = {"url": project["agent_mcp_endpoint"], "connection_id": project["tool_connection_id"]}
    fingerprint = hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()
    name = "curated-tools"
    with AIProjectClient(endpoint=project["endpoint"], credential=token_credential, allow_preview=True) as client:
        try:
            versions = list(client.toolboxes.list_versions(name=name))
        except ResourceNotFoundError:
            versions = []
        matching = [item for item in versions if item.metadata and item.metadata.get("definition_sha256") == fingerprint]
        if matching:
            version = matching[0]
        else:
            if versions:
                raise ValueError("Existing toolbox definition differs; an explicit reviewed toolbox release is required.")
            version = client.toolboxes.create_version(
                name=name,
                tools=[MCPToolboxTool(
                    name="team-agent", server_label="team-agent",
                    server_url=project["agent_mcp_endpoint"],
                    project_connection_id=project["tool_connection_id"],
                    allowed_tools=[project.get("agent_tool_name", f"{project['name']}_ask_agent")], require_approval="never",
                )],
                metadata={"definition_sha256": fingerprint},
                description="Approved team agent tool through the dedicated AI Gateway.",
            )
        client.toolboxes.update(name=name, default_version=version.version)
        return {
            "project_id": project["id"], "name": name, "version": version.version,
            "definition_sha256": fingerprint,
            "endpoint": project["endpoint"] + f"/toolboxes/{name}/mcp?api-version=v1",
        }
