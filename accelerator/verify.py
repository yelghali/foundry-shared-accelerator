"""Read back actual Azure state and write a nonsecret retained-environment inventory."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .control import ControlPlane, credential, load_json, save_receipt


def verify_resources(
    control: ControlPlane, deployment: dict[str, Any], projects: list[dict[str, Any]],
) -> dict[str, Any]:
    accounts: list[dict[str, Any]] = []
    for label, expected in [("agents", deployment["agent_foundry"]), *deployment["models"].items()]:
        actual = control.request("GET", expected["id"], version="2026-05-01")
        properties = actual["properties"]
        if properties.get("provisioningState") != "Succeeded":
            raise RuntimeError(f"Foundry account {expected['name']} is not ready.")
        if properties.get("publicNetworkAccess") != "Disabled" or properties.get("disableLocalAuth") is not True:
            raise RuntimeError("Foundry public or local-key authentication is not disabled.")
        injections = properties.get("networkInjections") or []
        if label == "agents" and not any(item.get("scenario") == "agent" for item in injections):
            raise RuntimeError("Shared agent Foundry lacks its required agent network integration.")
        if label != "agents" and injections:
            raise RuntimeError("A model-only account unexpectedly has agent network integration.")
        connections = properties.get("privateEndpointConnections", [])
        approved = [item for item in connections if item["properties"]["privateLinkServiceConnectionState"]["status"] == "Approved"]
        if not approved:
            raise RuntimeError("Foundry account lacks an approved private endpoint.")
        accounts.append({
            "purpose": label, "id": actual["id"], "name": expected["name"],
            "location": actual["location"], "public_access": properties["publicNetworkAccess"],
            "local_auth_disabled": properties["disableLocalAuth"], "approved_private_endpoints": len(approved),
            "agent_network_injections": len(injections),
        })
    gateways: list[dict[str, Any]] = []
    candidates = [deployment["gateway"], *[project["gateway"] for project in projects]]
    for expected in {item["id"].lower(): item for item in candidates}.values():
        actual = control.request("GET", expected["id"])
        sku = control.request("GET", expected["id"], version="2026-05-01").get("sku", {}).get("name")
        if actual["type"].lower() != "microsoft.apimanagement/aigateways" or sku != expected["sku"]:
            raise RuntimeError("Actual resource is not the requested dedicated gateway product/SKU.")
        if actual["properties"]["provisioningState"] != "Succeeded":
            raise RuntimeError("Gateway is not ready.")
        keys = control.request("GET", expected["id"] + "/apiKeys").get("value", [])
        builtin = [item for item in keys if item.get("name") == "master"
                   and item.get("properties", {}).get("displayName") == "Built-in all-access subscription"]
        active = [item for item in keys if item.get("properties", {}).get("state") == "active" and item not in builtin]
        if active:
            raise RuntimeError("Unexpected active application runtime keys exist in the Entra-only test environment.")
        gateways.append({
            "id": actual["id"], "name": expected["name"], "type": actual["type"],
            "sku": sku, "endpoint": expected["endpoint"], "active_application_keys": len(active),
            "administrative_builtin_keys": len(builtin),
            "backend_subnet_id": actual["properties"]["backend"]["subnet"]["id"],
        })
    alias_id = deployment["gateway"]["id"] + "/workspaces/default/aliases/approved-chat"
    alias = control.request("GET", alias_id)["properties"]
    if len(alias["backendModels"]) != len(deployment["models"]):
        raise RuntimeError("Native alias membership does not match the enabled regional model resources.")
    return {
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "subscription_id": deployment["subscription_id"], "resource_group": deployment["resource_group"],
        "accounts": accounts, "gateways": gateways, "model_alias": alias,
        "projects": [{
            "id": project["id"], "name": project["name"], "gateway_id": project["gateway"]["id"],
            "model_reference": project["model_reference"],
            "agents": project.get("agents", {project["agent"]["name"]: {"agent": project["agent"]}}),
            "toolbox": project.get("toolbox"),
        } for project in projects],
        "resources_retained": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--projects", nargs="+", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--usage")
    parser.add_argument("--markdown")
    args = parser.parse_args()
    value = verify_resources(ControlPlane(credential()), load_json(args.deployment), [load_json(path) for path in args.projects])
    save_receipt(args.output, value)
    if args.markdown:
        if not args.usage:
            parser.error("--markdown requires the verified --usage report.")
        usage = load_json(args.usage)
        lines = [
            "# Retained Azure deployment results", "",
            f"Verified: {value['verified_at']}", "",
            f"Subscription: `{value['subscription_id']}`",
            f"Resource group: `{value['resource_group']}`",
            f"[Open Azure resource group](https://portal.azure.com/#resource/subscriptions/{value['subscription_id']}/resourceGroups/{value['resource_group']}/overview)",
            "", "All resources are retained. No destroy was run. Billable infrastructure remains provisioned.",
            "", "## Foundry resources", "",
            "| Purpose | Name | Region | Public access | Approved PEs | Agent injection |",
            "|---|---|---|---|---:|---:|",
        ]
        lines.extend(f"| {item['purpose']} | {item['name']} | {item['location']} | {item['public_access']} | {item['approved_private_endpoints']} | {item['agent_network_injections']} |" for item in value["accounts"])
        lines += ["", "## Dedicated gateways", "", "| Name | Product/SKU | Endpoint | Active app keys |", "|---|---|---|---:|"]
        lines.extend(f"| {item['name']} | aigateways / {item['sku']} | {item['endpoint']} | {item['active_application_keys']} |" for item in value["gateways"])
        lines += ["", "Azure's administrative built-in master keys remain undistributed; application traffic uses Entra and managed identity.",
                  "", "## Project/agent bindings", ""]
        for project in value["projects"]:
            lines += [f"### {project['name']}", "", f"Project: `{project['id']}`", f"Model: `{project['model_reference']}`", ""]
            for name, binding in project["agents"].items():
                agent = binding["agent"]
                lines += [f"- Agent `{name}`, selected version `{agent['version']}`, ID `{agent['id']}`.",
                          f"  MCP: {binding.get('mcp_endpoint', 'See project receipt')}"]
            if project.get("toolbox"):
                lines.append(f"- Curated Toolbox `{project['toolbox']['name']}`, version `{project['toolbox']['version']}`.")
            lines.append("")
        lines += ["## Native project token showback", "",
                  "Cumulative native snapshot for the verified Entra-only test population, not a monthly invoice.",
                  "", "| Project | Input tokens | Output tokens | Total tokens | Quality |", "|---|---:|---:|---:|---|"]
        for project in usage["native_project_showback"]["projects"]:
            lines.append(f"| {project['project_name']} | {project['input_tokens']} | {project['output_tokens']} | {project['total_tokens']} | {project['data_status']} |")
        lines += ["", "## Regional model evidence", "", "| Backend | Region | Actual TokenTransaction |", "|---|---|---:|"]
        for backend, item in usage["backend_account_metrics"].items():
            lines.append(f"| {backend} | {item['location']} | {item['counters'].get('TokenTransaction')} |")
        lines += ["", "Both backends served inference through the native equal-weight alias.",
                  "", "## Reproduce", "", "From the accelerator directory in PowerShell:", "", "```powershell",
                  ".\\scripts\\Test-Live.ps1",
                  "python .\\samples\\consume_agent.py --project .local\\teams\\team-gamma.json --question \"Reply with exactly READY\" --expected READY",
                  "python .\\samples\\consume_agent.py --project .local\\teams\\team-gamma.json --agent-name welcome --question \"Reply with exactly READY\" --expected READY",
                  "python .\\samples\\consume_agent.py --project .local\\teams\\team-alpha.json --toolbox --question \"Reply with exactly READY\" --expected READY",
                  "```", "", "## Boundaries", "",
                  "- Dedicated `Microsoft.ApiManagement/aigateways`, not conventional APIM or Foundry-managed gateway integration.",
                  "- Agent consumption is native MCP wrapping a private Responses API, not a generic raw HTTP/SSE proxy.",
                  "- Hosted-agent templates and the onboarding form are future work.",
                  "- Monetary chargeback needs effective rates, period deltas, authentication-population validation and invoice reconciliation.",
                  "- The no-public-IP VM, NAT, private endpoints, monitoring and gateways remain billable.",
                  "- Keep the git-ignored Terraform state and `.local` receipts; do not adopt these live resources from a fresh empty state.",
                  ""]
        target = Path(args.markdown)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines), encoding="utf-8")
    print(f"Actual private Foundry and dedicated gateway state verified: {args.output}. No resources deleted.")


if __name__ == "__main__":
    main()
